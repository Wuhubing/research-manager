#!/usr/bin/env python3
"""科研管理器 — 本地服务。

- /api/transcribe : 浏览器录音 → OpenAI 语音转文字
- /api/prompt     : 口述 + 主线 + 最近记录 → 结构化 prompt（含范围边界，防止 agent 过度发挥）
- /api/digest     : agent 的输出 → 归纳（做了什么 / 是否越界 / 和主线关系 / 发现 / 下一步）
- 数据存在 data/projects/<id>.json，同时导出一份可读的 <id>.md 研究日志

只依赖 Python 标准库；LLM 调用走本机的 `claude -p`（用你的 Claude 订阅）。
"""
import atexit
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import lab

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("RM_DATA_DIR", ROOT / "data")) / "projects"
DATA.mkdir(parents=True, exist_ok=True)

PORT = int(os.environ.get("RM_PORT", "8765"))
CLAUDE_MODEL = os.environ.get("RM_CLAUDE_MODEL", "")  # 留空 = claude 默认模型


# ---------------------------------------------------------------- storage

def project_path(pid):
    if not re.fullmatch(r"[a-z0-9]{8,32}", pid):
        raise ValueError("bad project id")
    return DATA / f"{pid}.json"


def load_project(pid):
    return json.loads(project_path(pid).read_text("utf-8"))


def save_project(p):
    project_path(p["id"]).write_text(json.dumps(p, ensure_ascii=False, indent=2), "utf-8")
    (DATA / f"{p['id']}.md").write_text(render_markdown(p), "utf-8")


def list_projects():
    out = []
    for f in sorted(DATA.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True):
        p = json.loads(f.read_text("utf-8"))
        out.append({"id": p["id"], "name": p["name"], "updated": p.get("updated", 0)})
    return out


def new_project(name):
    p = {
        "id": uuid.uuid4().hex[:12],
        "name": name or "未命名项目",
        "created": time.time(),
        "updated": time.time(),
        "mainline": {"question": "", "hypotheses": "", "focus": "", "out_of_scope": ""},
        "entries": [],
    }
    save_project(p)
    return p


def fmt_time(t):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t))


RELATION_LABEL = {"mainline": "主线推进", "branch": "支线", "drift": "偏离主线"}


def render_markdown(p):
    m = p["mainline"]
    lines = [f"# {p['name']} — 研究日志", "", "## 主线", "",
             f"- **核心问题**：{m.get('question') or '（未填写）'}",
             f"- **当前假设**：{m.get('hypotheses') or '（未填写）'}",
             f"- **当前焦点**：{m.get('focus') or '（未填写）'}",
             f"- **不做的事**：{m.get('out_of_scope') or '（未填写）'}", "", "## 时间线", ""]
    for e in reversed(p["entries"]):
        if e["type"] == "prompt":
            lines += [f"### {fmt_time(e['time'])} · 下达任务", "", "口述原话：", "",
                      f"> {e['transcript']}", "", "```", e["prompt"], "```", ""]
        elif e["type"] == "digest":
            s = e["summary"]
            lines += [f"### {fmt_time(e['time'])} · {s.get('title', '归纳')} "
                      f"[{RELATION_LABEL.get(s.get('relation'), s.get('relation', ''))}]", "",
                      f"**做了什么**：{s.get('did', '')}", "",
                      f"**和主线的关系**：{s.get('relation_note', '')}", ""]
            for key, label in [("overreach", "超出要求的操作"), ("findings", "发现"),
                               ("open_questions", "待解决问题"), ("next_steps", "建议下一步")]:
                items = s.get(key) or []
                if items:
                    lines.append(f"**{label}**：")
                    lines += [f"- {x}" for x in items]
                    lines.append("")
            if e.get("note"):
                lines += [f"**我的备注**：{e['note']}", ""]
        elif e["type"] == "note":
            lines += [f"### {fmt_time(e['time'])} · 笔记", "", e["text"], ""]
    return "\n".join(lines)


# ---------------------------------------------------------------- LLM

def call_claude(system, user, timeout=240):
    cmd = ["claude", "-p", "--tools", "", "--no-session-persistence",
           "--output-format", "json", "--system-prompt", system]
    if CLAUDE_MODEL:
        cmd += ["--model", CLAUDE_MODEL]
    r = subprocess.run(cmd, input=user, capture_output=True, text=True,
                       timeout=timeout, cwd=tempfile.gettempdir())
    if r.returncode != 0:
        raise RuntimeError(f"claude 调用失败：{r.stderr.strip() or r.stdout.strip()[:500]}")
    data = json.loads(r.stdout)
    if data.get("is_error"):
        raise RuntimeError(f"claude 返回错误：{data.get('result')}")
    return data.get("result", "")


def extract_json(text):
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError(f"模型没有返回 JSON：{text[:300]}")
    return json.loads(text[start:end + 1])


def mainline_block(p):
    m = p["mainline"]
    return (f"项目：{p['name']}\n"
            f"核心问题：{m.get('question') or '（未填写）'}\n"
            f"当前假设：{m.get('hypotheses') or '（未填写）'}\n"
            f"当前焦点：{m.get('focus') or '（未填写）'}\n"
            f"明确不做的事：{m.get('out_of_scope') or '（未填写）'}")


def recent_block(p, n=6):
    items = []
    for e in p["entries"][:n]:
        if e["type"] == "digest":
            s = e["summary"]
            items.append(f"- [{fmt_time(e['time'])}] {s.get('title')}（{RELATION_LABEL.get(s.get('relation'), '')}）："
                         f"{s.get('did')}；发现：{'；'.join(s.get('findings') or []) or '无'}")
        elif e["type"] == "prompt":
            items.append(f"- [{fmt_time(e['time'])}] 下达任务：{e.get('title') or e['transcript'][:80]}")
        elif e["type"] == "note":
            items.append(f"- [{fmt_time(e['time'])}] 笔记：{e['text'][:200]}")
    return "\n".join(items) or "（还没有记录）"


PROMPT_SYSTEM = """你是科研助理，负责把研究者口语化、零散、可能有语音识别错误的口述，整理成一条交给编程/科研 agent（如 Claude Code）执行的高质量 prompt。

研究者的痛点：agent 经常“过度发挥”——做了没要求的事、改了不该改的文件、偷偷换了方法——导致研究者不知道发生了什么。所以你写的 prompt 必须：
1. 忠实于研究者的意图，不替他扩大任务范围；口述里没说的，不要加进任务。
2. 明确写出“范围边界”：只做什么、不要做什么（结合主线里“明确不做的事”），遇到需要越界的情况要先停下来问。
3. 写清楚交付物和完成标准。
4. 要求 agent 结束时按固定格式汇报：做了什么、改动了哪些文件、关键结果/数字、与要求不一致或自作主张的地方、遇到的问题。
5. 修正明显的语音识别错误（尤其是专业术语、英文词），但不确定的地方列入 assumptions。

prompt 用中文写（代码、术语、文件名保持原样），简洁，用小标题：## 目标 / ## 背景 / ## 具体任务 / ## 范围边界 / ## 交付物 / ## 完成后汇报格式。

只输出一个 JSON 对象，不要其他文字：
{"title": "≤15字的任务名", "prompt": "完整 prompt 文本", "assumptions": ["你做出的推断或纠正的识别错误"], "questions": ["如果口述有关键歧义，需要研究者确认的问题；没有则空数组"]}"""


DIGEST_SYSTEM = """你是科研助理，负责在 agent 每跑完一次任务后做归纳，帮研究者快速掌握“到底发生了什么”，并判断和研究主线的关系。

研究者会给你：研究主线、最近的研究记录、（可能有）当时下达给 agent 的 prompt、agent 的输出/日志/研究者的描述。

要求：
- 客观、具体，引用关键数字和文件名；不要夸大结果，不确定就说不确定。
- 如果有原始 prompt，逐条对比：agent 有没有做了没被要求的事、改了范围外的东西、换了方法或偷偷降低标准（例如跳过测试、改了评测、用了假数据）。这些写进 overreach。没有就空数组。
- relation 三选一：mainline（直接推进核心问题/当前焦点）、branch（相关但是支线）、drift（偏离主线，或碰了“明确不做的事”）。
- 如果这次结果意味着主线（假设/焦点）应该更新，在 mainline_update 里给出建议的新文字；否则留空字符串。

只输出一个 JSON 对象，不要其他文字：
{"title": "≤15字的标题", "did": "2-4句，agent 实际做了什么", "overreach": ["超出要求或可疑的操作"], "relation": "mainline|branch|drift", "relation_note": "1-2句，为什么这样判断，对主线意味着什么", "findings": ["新发现，每条一句，带数字"], "open_questions": ["悬而未决的问题"], "next_steps": ["建议的下一步，最多3条，要贴合主线"], "mainline_update": {"hypotheses": "", "focus": ""}}"""


def make_prompt(p, transcript, previous=None, feedback=None):
    user = f"## 研究主线\n{mainline_block(p)}\n\n## 最近的研究记录\n{recent_block(p)}\n\n"
    if previous:
        user += (f"## 上一版 prompt\n{previous}\n\n## 研究者对上一版的修改意见（口述）\n{feedback}\n\n"
                 "请根据修改意见改写 prompt，保留没被提到的部分。")
    else:
        user += f"## 研究者的口述（语音识别结果，可能有错字）\n{transcript}"
    return extract_json(call_claude(PROMPT_SYSTEM, user))


def make_digest(p, output, prompt_text=None):
    user = f"## 研究主线\n{mainline_block(p)}\n\n## 最近的研究记录\n{recent_block(p)}\n\n"
    if prompt_text:
        user += f"## 当时下达给 agent 的 prompt\n{prompt_text}\n\n"
    user += f"## agent 的输出 / 日志 / 研究者的描述\n{output[:60000]}"
    return extract_json(call_claude(DIGEST_SYSTEM, user))


# ---------------------------------------------------------------- 设置

def settings_view():
    """给设置页看的内容：key 只露最后 4 位。"""
    key = lab.openai_key()
    return {"openai_key_set": bool(key), "openai_key_hint": f"sk-…{key[-4:]}" if key else "",
            "openai_key_from_env": bool(os.environ.get("OPENAI_API_KEY")),
            "asr_model": lab.asr_model(), "asr_models": lab.ASR_MODELS,
            "context_window": str(lab.load_settings().get("context_window") or "auto"),
            "context_windows": lab.CONTEXT_WINDOWS}


# ---------------------------------------------------------------- 菜单栏录音（⌃P）

# 最近一次录音：识别出的原话（自动复制）
QUICK = {"state": "idle", "proc": None, "wav": None, "text": "", "error": ""}


def quick_record(action, req):
    if action == "start":
        if QUICK["state"] == "recording":
            return {"error": "已经在录音"}
        try:
            QUICK["proc"], QUICK["wav"] = lab.start_recording()
        except Exception as ex:
            QUICK.update(state="idle", text="", error=str(ex))
            return {"error": str(ex)}
        QUICK.update(state="recording", error="")
        return {"state": "recording"}
    if action == "stop":
        if QUICK["state"] != "recording":
            return {"error": "没有在录音"}
        QUICK["state"] = "transcribing"
        try:
            wav = lab.stop_recording(QUICK["proc"], QUICK["wav"])
            text = lab.transcribe(wav).strip()
            os.unlink(wav)
            if not text:
                raise RuntimeError("没听清，再说一次")
        except Exception as ex:
            QUICK.update(state="idle", error=str(ex))
            notify("录音失败", str(ex))
            return {"error": str(ex)}
        lab.copy_to_clipboard(text)
        QUICK.update(state="idle", text=text, error="")
        return {"state": "idle", "text": text}
    # copy：再复制一次最近的原话
    if not QUICK["text"]:
        return {"error": "还没有内容"}
    lab.copy_to_clipboard(QUICK["text"])
    return {"ok": True}


# ---------------------------------------------------------------- 语音

AUDIO_EXT = {"audio/webm": ".webm", "audio/mp4": ".mp4", "audio/mpeg": ".mp3", "audio/wav": ".wav",
             "audio/x-m4a": ".m4a", "audio/ogg": ".ogg"}


def transcribe(audio_bytes, content_type="", hint=""):
    ext = AUDIO_EXT.get(content_type.split(";")[0].strip(), ".webm")
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / f"audio{ext}"
        src.write_bytes(audio_bytes)
        return lab.transcribe(src, hint)


# ---------------------------------------------------------------- Claude Code 会话

# 左栏“我关注的会话”，存在用户目录，换浏览器也不丢
WATCH_FILE = Path.home() / ".research-manager" / "watch.json"


def load_watch():
    return json.loads(WATCH_FILE.read_text()) if WATCH_FILE.exists() else []


def save_watch(ids):
    WATCH_FILE.parent.mkdir(parents=True, exist_ok=True)
    WATCH_FILE.write_text(json.dumps([i for i in ids if re.fullmatch(r"[0-9a-f-]{36}", i)]))


# 交接：新会话接替旧会话的关系 {新会话 id: 旧会话 id}
LINKS_FILE = Path.home() / ".research-manager" / "links.json"


def load_links():
    return json.loads(LINKS_FILE.read_text()) if LINKS_FILE.exists() else {}


def adopt_new_session(cwd, old_id, started):
    """在 Terminal 里新开会话后，等它的记录文件出现，自动加进关注列表并记下接替关系。"""
    def watch():
        for _ in range(120):
            time.sleep(2)
            new = [f for f in lab.list_sessions(cwd) if f.stat().st_mtime > started and f.stem != old_id]
            if new:
                nid = new[0].stem
                links = load_links()
                links[nid] = old_id
                LINKS_FILE.write_text(json.dumps(links, indent=1))
                ids = load_watch()
                if nid not in ids:
                    pos = ids.index(old_id) + 1 if old_id in ids else len(ids)
                    save_watch(ids[:pos] + [nid] + ids[pos:])
                return
    threading.Thread(target=watch, daemon=True).start()


def session_detail(sid):
    path = lab.find_session(sid)
    cwd = lab.session_cwd(path)
    size = path.stat().st_size
    activity, _ = lab.condense(path, max(0, size - 2_000_000), budget=120_000)
    rdir = lab.git_root(cwd) / ".research"
    mainline = (rdir / "mainline.md").read_text() if (rdir / "mainline.md").exists() else lab.MAINLINE_TEMPLATE
    history = json.loads((rdir / "history.json").read_text()) if (rdir / "history.json").exists() else []
    for h in history:
        _, h["reply"], h["prompt"] = lab.parse_reply(h["reply"])
    prev = load_links().get(sid)
    try:
        prev_title = lab.session_title(lab.find_session(prev)) if prev else None
    except Exception:
        prev_title = None
    return {"id": sid, "cwd": cwd, "title": lab.session_title(path), "mtime": path.stat().st_mtime,
            "activity": activity, "mainline": mainline, "history": history[-30:],
            "context": lab.context_usage(path),
            "continues": {"id": prev, "title": prev_title} if prev else None,
            "handoff": lab.Lab(cwd, work=path).handoff_checkpoint() if rdir.exists() else None}


# ---------------------------------------------------------------- HTTP

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT / "static"), **kw)

    def log_message(self, fmt, *args):
        if args and "/api/" in str(args[0]):
            super().log_message(fmt, *args)

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        return self.rfile.read(int(self.headers.get("Content-Length", 0)))

    def do_GET(self):
        try:
            if self.path == "/api/projects":
                return self.send_json(list_projects())
            m = re.fullmatch(r"/api/projects/(\w+)", self.path)
            if m:
                return self.send_json(load_project(m.group(1)))
            m = re.fullmatch(r"/api/projects/(\w+)/log\.md", self.path)
            if m:
                body = render_markdown(load_project(m.group(1))).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            if self.path == "/api/sessions":
                return self.send_json(lab.all_sessions())
            if self.path == "/api/watch":
                return self.send_json(load_watch())
            if self.path == "/api/watched":  # 菜单栏用：关注的会话及其状态
                ids = load_watch()
                found = {}
                for i in ids:
                    try:
                        p = lab.find_session(i)
                        found[i] = {"id": i, "title": lab.session_title(p), "mtime": p.stat().st_mtime,
                                    "context": lab.context_usage(p)}
                    except Exception:
                        pass
                return self.send_json([found[i] for i in ids if i in found])
            m = re.fullmatch(r"/api/sessions/([0-9a-f-]+)", self.path)
            if m:
                return self.send_json(session_detail(m.group(1)))
            if self.path == "/api/status":
                return self.send_json({"openai": bool(lab.openai_key()),
                                       "claude": bool(shutil.which("claude"))})
            if self.path == "/api/settings":
                return self.send_json(settings_view())
            if self.path == "/api/quick_record/last":
                return self.send_json({k: QUICK[k] for k in ("state", "text", "error")})
        except Exception as ex:
            return self.send_json({"error": str(ex)}, 400)
        return super().do_GET()

    def do_POST(self):
        try:
            if self.path.startswith("/api/transcribe"):
                hint = ""
                m = re.search(r"project=(\w+)", self.path)
                if m:
                    p = load_project(m.group(1))
                    hint = p["name"] + "。" + p["mainline"].get("question", "")
                return self.send_json({"text": transcribe(self.body(), self.headers.get("Content-Type", ""), hint)})

            # 只接受 JSON：别的网页无法不经预检就跨域发 JSON，防止被偷偷改设置
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                return self.send_json({"error": "需要 application/json"}, 415)
            req = json.loads(self.body() or b"{}")
            if self.path == "/api/settings":
                if req.get("openai_key"):
                    key = req["openai_key"].strip()
                    if not re.fullmatch(r"sk-[A-Za-z0-9_\-]{20,}", key):
                        return self.send_json({"error": "key 格式不对，应该以 sk- 开头"}, 400)
                    lab.set_openai_key(key)
                if req.get("asr_model") in lab.ASR_MODELS:
                    lab.save_settings(asr_model=req["asr_model"])
                if req.get("context_window") in lab.CONTEXT_WINDOWS:
                    lab.save_settings(context_window=req["context_window"])
                return self.send_json(settings_view())
            m = re.fullmatch(r"/api/quick_record/(start|stop|copy)", self.path)
            if m:
                return self.send_json(quick_record(m.group(1), req))
            if self.path == "/api/watch":
                save_watch(req.get("ids", []))
                return self.send_json(load_watch())
            m = re.fullmatch(r"/api/sessions/([0-9a-f-]+)/(ask|mainline|handoff|newsession)", self.path)
            if m:
                path = lab.find_session(m.group(1))
                mgr = lab.Lab(lab.session_cwd(path), work=path)
                if m.group(2) == "mainline":
                    mgr.mainline_file.write_text(req["text"].strip() + "\n")
                    return self.send_json({"ok": True})
                if m.group(2) == "handoff":
                    return self.send_json(mgr.handoff(force=bool(req.get("force"))))
                if m.group(2) == "newsession":
                    # 只接受管家生成在 .research 里的交接文件，用它旁边的 .prompt.txt 作为新会话第一条消息
                    doc = Path(req["path"]).resolve()
                    pfile = doc.with_suffix(".prompt.txt")
                    if doc.parent != mgr.dir.resolve() or not doc.name.startswith("handoff-") or not pfile.exists():
                        return self.send_json({"error": "交接文件路径不对"}, 400)
                    started = time.time()
                    lab.open_terminal_session(mgr.project, pfile)
                    adopt_new_session(mgr.project, m.group(1), started)
                    return self.send_json({"ok": True})
                said = req.get("said", "").strip()
                reply = mgr.ask(said, want_prompt=bool(said) and req.get("want_prompt", True))
                mgr.log(said, reply)
                mainline, shown, prompt = lab.parse_reply(reply)
                return self.send_json({"reply": shown, "prompt": prompt, "mainline": mainline,
                                       "time": time.time(), "said": said})

            if self.path == "/api/projects":
                return self.send_json(new_project(req.get("name", "").strip()))

            m = re.fullmatch(r"/api/projects/(\w+)/(\w+)", self.path)
            if not m:
                return self.send_json({"error": "not found"}, 404)
            p, action = load_project(m.group(1)), m.group(2)

            if action == "mainline":
                p["mainline"].update({k: req.get(k, "") for k in p["mainline"]})
                if req.get("name"):
                    p["name"] = req["name"]
            elif action == "draft_prompt":
                return self.send_json(make_prompt(p, req.get("transcript", ""),
                                                  req.get("previous"), req.get("feedback")))
            elif action == "save_prompt":
                p["entries"].insert(0, {"id": uuid.uuid4().hex[:10], "type": "prompt", "time": time.time(),
                                        "title": req.get("title", ""), "transcript": req.get("transcript", ""),
                                        "prompt": req["prompt"]})
            elif action == "digest":
                prompt_text = None
                if req.get("prompt_id"):
                    prompt_text = next((e["prompt"] for e in p["entries"] if e["id"] == req["prompt_id"]), None)
                summary = make_digest(p, req["output"], prompt_text)
                p["entries"].insert(0, {"id": uuid.uuid4().hex[:10], "type": "digest", "time": time.time(),
                                        "prompt_id": req.get("prompt_id"), "output": req["output"],
                                        "summary": summary, "note": req.get("note", "")})
            elif action == "note":
                p["entries"].insert(0, {"id": uuid.uuid4().hex[:10], "type": "note",
                                        "time": time.time(), "text": req["text"]})
            elif action == "delete_entry":
                p["entries"] = [e for e in p["entries"] if e["id"] != req["id"]]
            elif action == "delete":
                project_path(p["id"]).unlink()
                (DATA / f"{p['id']}.md").unlink(missing_ok=True)
                return self.send_json({"ok": True})
            else:
                return self.send_json({"error": "unknown action"}, 404)

            p["updated"] = time.time()
            save_project(p)
            return self.send_json(p)
        except Exception as ex:
            return self.send_json({"error": str(ex)}, 500)


def stop_quick_recording(*_):
    """服务退出时停掉正在进行的录音，避免 ffmpeg 留在后台一直占着麦克风。"""
    p = QUICK.get("proc")
    if p and p.poll() is None:
        p.kill()
    if QUICK.get("wav"):
        Path(QUICK["wav"]).unlink(missing_ok=True)


def on_sigterm(*_):
    stop_quick_recording()
    raise SystemExit(0)


if __name__ == "__main__":
    lab.kill_stale_recordings()
    atexit.register(stop_quick_recording)
    signal.signal(signal.SIGTERM, on_sigterm)
    print(f"科研管理器运行在 http://localhost:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
