#!/usr/bin/env python3
"""lab — 终端里的科研管家。

在研究项目目录里运行 `lab`：
  - 回车开始录音，再回车结束 → OpenAI 转文字（gpt-4o-transcribe）
  - 管家（一个持续对话的 Claude CLI 会话）会：
      1. 读取你正在运行的 Claude Code 工作会话里“自上次以来”的新动态
      2. 必要时只读地查看项目文件 / git diff 核实
      3. 对照研究主线给出归纳：做了什么、有没有越界、和主线的关系、发现
      4. 按你说的话写下一条 prompt，自动复制到剪贴板
  - 一切记录在 <项目>/.research/ 下：mainline.md（主线）、log.md（日志）

管家是只读的：只能 Read/Grep/Glob 和 git status/diff/log/show、ls，不能改任何文件。
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

MIC = os.environ.get("LAB_MIC", ":0")  # ffmpeg avfoundation 音频设备，:0 = 内置麦克风
CLAUDE_MODEL = os.environ.get("LAB_CLAUDE_MODEL", "")

DIM, BOLD, RED, GREEN, YELLOW, CYAN, RESET = "\033[2m", "\033[1m", "\033[31m", "\033[32m", "\033[33m", "\033[36m", "\033[0m"

ALLOWED_TOOLS = ["Read", "Grep", "Glob", "Bash(git status:*)", "Bash(git diff:*)",
                 "Bash(git log:*)", "Bash(git show:*)", "Bash(ls:*)"]

SYSTEM = """你是研究者的“科研管家”，长期陪伴同一个研究项目。研究者通过语音和你说话（语音识别可能有错字，请按上下文纠正）。

研究者的核心痛点：他让另一个 Claude Code（下称“工作 agent”）干活，但 agent 经常过度发挥，他不知道到底发生了什么；他也不擅长把想法写成精确的 prompt。

每轮你会收到：研究主线、工作 agent 会话自上次以来的新动态（压缩过的记录）、研究者这次说的话。
你可以用只读工具（Read/Grep/Glob、git status/diff/log/show、ls）去项目目录核实细节，比如 agent 声称改了什么、实验结果文件里写了什么。只在有必要时查，别漫无目的地翻。

回复格式（中文，简洁，用研究者听得懂的话；没有内容的小节直接省略）：

## 发生了什么
工作 agent 这段时间实际做了什么（具体到文件、命令、数字）。没有新动态就说“工作会话没有新动态”。

## ⚠ 越界 / 可疑
agent 做了研究者没要求的事、改了范围外的东西、换方法、降低标准（跳过测试、改评测、假数据、偷偷改超参等）。没有就省略本节。

## 和主线的关系
一句话标注【主线推进】/【支线】/【偏离主线】，再说明对主线意味着什么，有哪些新发现。

## 建议下一步
最多 3 条。

## Prompt
如果研究者这次是想给 agent 派任务（或明显需要一条纠偏 prompt），写一条给工作 agent 的 prompt，放在 ```prompt 代码块里。prompt 必须：忠实于研究者意图、不擅自扩大范围；写清目标、具体任务、范围边界（只做什么/不要做什么/遇到什么情况先停下来问）、交付物；并要求 agent 完成后按“做了什么 / 改动的文件 / 关键结果 / 与要求不一致或自作主张之处 / 遇到的问题”汇报。如果研究者的话有关键歧义，先在 prompt 前列出需要他确认的问题。研究者只是聊想法或问情况时，不写 prompt。

如果主线（核心问题/当前假设/当前焦点/不做的事）需要建立或更新，在最后附上：
<mainline>
完整的新主线 markdown
</mainline>
主线为空时，请根据研究者的话主动起草一版。"""

MAINLINE_TEMPLATE = """# 研究主线

- **核心问题**：
- **当前假设**：
- **当前焦点**：
- **明确不做的事**：
"""


# ---------------------------------------------------------------- 工作会话

def sessions_dir(project):
    return Path.home() / ".claude/projects" / re.sub(r"[^A-Za-z0-9]", "-", str(project))


def session_title(path):
    title, first = None, None
    try:
        with open(path) as f:
            for line in f:
                d = json.loads(line)
                if d.get("type") == "custom-title":
                    title = d.get("customTitle") or d.get("title") or title
                elif first is None and d.get("type") == "user" and isinstance(d["message"].get("content"), str):
                    c = d["message"]["content"]
                    if not c.startswith("<"):
                        first = c
    except Exception:
        pass
    return (title or first or path.stem)[:50].replace("\n", " ")


def list_sessions(project, exclude=()):
    d = sessions_dir(project)
    files = [f for f in d.glob("*.jsonl") if f.stem not in exclude] if d.exists() else []
    return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)


def clip(s, n):
    s = str(s).strip()
    return s if len(s) <= n else s[:n] + "…"


def condense(path, offset, budget=40000):
    """把工作会话 jsonl 从 offset 开始压缩成可读的动态记录。返回 (文本, 新 offset)。"""
    out = []
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
        new_offset = offset + len(data)
    for raw in data.decode("utf-8", "ignore").splitlines():
        try:
            d = json.loads(raw)
        except Exception:
            continue
        t, msg = d.get("type"), d.get("message")
        if t not in ("user", "assistant") or not isinstance(msg, dict) or d.get("isMeta"):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for b in content or []:
            bt = b.get("type")
            if bt == "text":
                text = b.get("text", "")
                if t == "user":
                    if text.startswith("<") and not text.startswith("<bash-input"):
                        continue
                    out.append(f"\n👤 研究者：{clip(text, 1500)}")
                else:
                    out.append(f"🤖 agent：{clip(text, 1500)}")
            elif bt == "tool_use":
                inp = b.get("input", {})
                name = b.get("name", "")
                if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                    detail = inp.get("file_path", "")
                    if name == "Edit":
                        detail += f"\n      - {clip(inp.get('old_string', ''), 200)}\n      + {clip(inp.get('new_string', ''), 300)}"
                elif name == "Bash":
                    detail = clip(inp.get("command", ""), 300)
                elif name in ("Read", "Glob", "Grep"):
                    detail = inp.get("file_path") or inp.get("pattern") or ""
                else:
                    detail = clip(json.dumps(inp, ensure_ascii=False), 200)
                out.append(f"   🔧 {name}: {detail}")
            elif bt == "tool_result" and b.get("is_error"):
                c = b.get("content")
                if isinstance(c, list):
                    c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
                out.append(f"   ✗ 报错：{clip(c, 300)}")
    text = "\n".join(out)
    if len(text) > budget:
        text = "（更早的内容已省略）…\n" + text[-budget:]
    return text, new_offset


# ---------------------------------------------------------------- 录音 / 识别

def record():
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav = tmp.name
    p = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "avfoundation", "-i", MIC,
                          "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", wav],
                         stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    t0 = time.time()
    try:
        input(f"{RED}● 录音中…{RESET} 说完按回车结束 ")
    except (KeyboardInterrupt, EOFError):
        pass
    try:
        p.communicate(b"q", timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()
    if p.returncode not in (0, 255) or not Path(wav).exists() or Path(wav).stat().st_size < 2000:
        err = (p.stderr.read().decode() if p.stderr and not p.stderr.closed else "")
        raise RuntimeError("录音失败。请在「系统设置 → 隐私与安全性 → 麦克风」里允许你的终端 App。" + err[-300:])
    print(f"{DIM}  录了 {time.time() - t0:.0f}s，识别中…{RESET}")
    return wav


OPENAI_KEY_FILE = Path.home() / ".research-manager/openai_key"
SETTINGS_FILE = Path.home() / ".research-manager/settings.json"
ASR_MODELS = ["gpt-4o-transcribe", "gpt-4o-mini-transcribe"]
ASR_CONTEXT = "科研讨论，中文为主，夹杂英文术语。"


def openai_key():
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key and OPENAI_KEY_FILE.exists():
        key = OPENAI_KEY_FILE.read_text().strip()
    return key or None


def set_openai_key(key):
    """写入 key 文件，权限 600（只有自己能读）。"""
    OPENAI_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(OPENAI_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key.strip())
    os.chmod(OPENAI_KEY_FILE, 0o600)


def load_settings():
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except Exception:
        return {}


def save_settings(**changes):
    s = {**load_settings(), **changes}
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=1))
    return s


def asr_model():
    return os.environ.get("LAB_OPENAI_ASR_MODEL") or load_settings().get("asr_model") or ASR_MODELS[0]


def transcribe(path, hint=""):
    """用 OpenAI 把音频转成文字（wav / webm / mp4 / m4a / mp3 都行）。"""
    if not openai_key():
        raise RuntimeError(f"还没有配置 OpenAI API key：写进 {OPENAI_KEY_FILE}，或设置环境变量 OPENAI_API_KEY")
    boundary = uuid.uuid4().hex
    fields = {"model": asr_model(), "prompt": ASR_CONTEXT + hint[:300], "response_format": "json"}
    body = b"".join(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        for k, v in fields.items())
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{Path(path).name}"\r\n'
             f"Content-Type: application/octet-stream\r\n\r\n").encode() + Path(path).read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/audio/transcriptions", data=body, method="POST",
        headers={"Authorization": f"Bearer {openai_key()}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())["text"].strip()
    except urllib.error.HTTPError as ex:
        msg = ex.read().decode("utf-8", "ignore")
        try:
            msg = json.loads(msg)["error"]["message"]
        except Exception:
            pass
        raise RuntimeError(f"OpenAI {ex.code}：{msg[:200]}")


def transcribe_recording(wav, hint=""):
    try:
        return transcribe(wav, hint)
    finally:
        os.unlink(wav)


# ---------------------------------------------------------------- 管家

class Lab:
    def __init__(self, project, pick=False, work=None):
        self.project = Path(project).resolve()
        self.pinned = work is not None
        self.dir = self.project / ".research"
        self.dir.mkdir(exist_ok=True)
        self.state_file = self.dir / "state.json"
        self.state = json.loads(self.state_file.read_text()) if self.state_file.exists() else {}
        self.mainline_file = self.dir / "mainline.md"
        if not self.mainline_file.exists():
            self.mainline_file.write_text(MAINLINE_TEMPLATE)
        if work:
            self.work = Path(work)
        else:
            self.choose_session(pick)

    def save(self):
        self.state_file.write_text(json.dumps(self.state, ensure_ascii=False, indent=1))

    def choose_session(self, pick):
        files = list_sessions(self.project, exclude={self.state.get("manager_session")})
        if not files:
            self.work = None
            print(f"{YELLOW}这个目录下还没有 Claude Code 会话记录。在这里开一个 claude 会话干活后，管家就能看到它。{RESET}")
            return
        if pick:
            for i, f in enumerate(files[:9]):
                ago = (time.time() - f.stat().st_mtime) / 60
                print(f"  [{i + 1}] {session_title(f)}  {DIM}{ago:.0f} 分钟前{RESET}")
            s = input("选哪个工作会话？[1] ").strip() or "1"
            self.work = files[int(s) - 1]
        else:
            self.work = files[0]
        self.state["work_session"] = self.work.stem
        self.save()

    def mainline(self):
        text = self.mainline_file.read_text()
        return text if text.strip() != MAINLINE_TEMPLATE.strip() else "（主线还是空的）"

    def new_activity(self):
        if not self.work:
            return "（没有工作会话）"
        # 如果出现了更新的会话，自动跟过去
        latest = [] if self.pinned else list_sessions(self.project, exclude={self.state.get("manager_session")})
        if latest and latest[0] != self.work:
            self.work = latest[0]
            print(f"{DIM}  切换到最新的工作会话：{session_title(self.work)}{RESET}")
        offsets = self.state.setdefault("offsets", {})
        start = offsets.get(self.work.stem)
        if start is None:  # 第一次看这个会话：只看最后一段
            start = max(0, self.work.stat().st_size - 400_000)
        text, offsets[self.work.stem] = condense(self.work, start)
        return text or "（自上次以来没有新动态）"

    def ask(self, said):
        activity = self.new_activity()
        msg = (f"# 研究主线\n{self.mainline()}\n\n"
               f"# 工作 agent 会话的新动态（会话：{session_title(self.work) if self.work else '无'}）\n{activity}\n\n"
               f"# 研究者这次说的话\n{said or '（研究者没说话，只想要一次归纳）'}")
        cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose",
               "--permission-mode", "default",
               "--tools", "Read,Grep,Glob,Bash",
               "--allowedTools", *ALLOWED_TOOLS,
               "--add-dir", str(self.project)]
        if CLAUDE_MODEL:
            cmd += ["--model", CLAUDE_MODEL]
        if self.state.get("manager_session"):
            cmd += ["--resume", self.state["manager_session"]]
        else:
            cmd += ["--append-system-prompt", SYSTEM]
        print(f"{DIM}  管家思考中…{RESET}")
        # 在 .research 目录里运行，管家自己的会话不会和工作会话混在一起
        p = subprocess.Popen(cmd, cwd=self.dir, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
        p.stdin.write(msg)
        p.stdin.close()
        result = None
        for line in p.stdout:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("type") == "assistant":
                for b in d["message"].get("content", []):
                    if b.get("type") == "tool_use":
                        inp = b.get("input", {})
                        what = inp.get("file_path") or inp.get("pattern") or inp.get("command") or ""
                        print(f"{DIM}  · 查看 {b['name']} {clip(what, 80)}{RESET}")
            elif d.get("type") == "result":
                result = d
        p.wait()
        if not result or result.get("is_error"):
            raise RuntimeError("管家调用失败：" + (result or {}).get("result", p.stderr.read()[-500:]))
        if not self.state.get("manager_session"):
            self.state["manager_session"] = result["session_id"]
        self.save()
        return result["result"]

    def history(self):
        f = self.dir / "history.json"
        return json.loads(f.read_text()) if f.exists() else []

    def log(self, said, reply):
        h = self.history()
        h.append({"time": time.time(), "session": self.work.stem if self.work else None,
                  "session_title": session_title(self.work) if self.work else None,
                  "said": said, "reply": reply})
        (self.dir / "history.json").write_text(json.dumps(h, ensure_ascii=False, indent=1))
        with open(self.dir / "log.md", "a") as f:
            f.write(f"\n\n---\n\n## {time.strftime('%Y-%m-%d %H:%M')}"
                    f"（会话：{session_title(self.work) if self.work else '无'}）\n\n"
                    f"**我说**：{said or '（只要归纳）'}\n\n{reply}\n")

    def handle(self, said):
        reply = self.ask(said)
        self.log(said, reply)
        mainline, shown, prompt = parse_reply(reply)
        print("\n" + colorize(shown) + "\n")
        if prompt:
            subprocess.run("pbcopy", input=prompt, text=True)
            print(f"{GREEN}✓ prompt 已复制到剪贴板，粘贴给工作 agent 即可{RESET}")
        if mainline:
            print(f"{CYAN}管家建议更新主线：{RESET}\n{mainline}\n")
            if input("采纳？[y/N] ").strip().lower() == "y":
                self.mainline_file.write_text(mainline + "\n")
                print(f"{GREEN}✓ 主线已更新（.research/mainline.md）{RESET}")

    def loop(self):
        print(f"\n{BOLD}🔬 lab · {self.project.name}{RESET}")
        if self.work:
            print(f"{DIM}  工作会话：{session_title(self.work)}{RESET}")
        print(f"{DIM}  主线：.research/mainline.md   日志：.research/log.md{RESET}")
        print(f"{DIM}  [回车] 录音   [直接打字+回车] 文字输入   /s 只归纳   /m 看主线   /p 换工作会话   /q 退出{RESET}\n")
        while True:
            try:
                line = input(f"{CYAN}lab>{RESET} ").strip()
            except (KeyboardInterrupt, EOFError):
                print()
                return
            try:
                if line in ("/q", "q", "exit"):
                    return
                if line == "/m":
                    print(self.mainline_file.read_text())
                    continue
                if line == "/p":
                    self.choose_session(pick=True)
                    continue
                if line == "/s":
                    self.handle("")
                    continue
                said = line or transcribe_recording(record(), self.project.name)
                if not said:
                    print(f"{YELLOW}没听清，再说一次{RESET}")
                    continue
                print(f"{BOLD}你说：{RESET}{said}")
                self.handle(said)
            except KeyboardInterrupt:
                print(f"\n{YELLOW}已取消{RESET}")
            except Exception as ex:
                print(f"{RED}{ex}{RESET}")


def parse_reply(reply):
    """拆出 (建议的新主线 | None, 去掉主线块后的正文, prompt | None)。"""
    m = re.search(r"<mainline>\s*(.*?)\s*</mainline>", reply, re.S)
    shown = re.sub(r"<mainline>.*?</mainline>", "", reply, flags=re.S).strip()
    p = re.search(r"```prompt\s*\n(.*?)```", reply, re.S)
    return (m.group(1) if m else None), shown, (p.group(1).strip() if p else None)


def session_cwd(path):
    # 会话中途可能换过目录：优先取末尾最近的 cwd
    with open(path, "rb") as f:
        f.seek(max(0, path.stat().st_size - 300_000))
        tail = f.read().decode("utf-8", "ignore")
    found = re.findall(r'"cwd":"((?:[^"\\]|\\.)*)"', tail)
    if found:
        return json.loads(f'"{found[-1]}"')
    with open(path) as f:
        for i, line in enumerate(f):
            if '"cwd"' in line:
                cwd = json.loads(line).get("cwd")
                if cwd:
                    return cwd
            if i > 200:
                break
    return None


def all_sessions(days=60, limit=400):
    """列出最近的 Claude Code 会话（跳过管家自己的会话）。"""
    cutoff = time.time() - days * 86400
    files = [f for f in (Path.home() / ".claude/projects").glob("*/*.jsonl") if f.stat().st_mtime > cutoff]
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    out = []
    for f in files[:limit]:
        cwd = session_cwd(f)
        if not cwd or cwd.endswith("/.research"):
            continue
        st = f.stat()
        out.append({"id": f.stem, "cwd": cwd, "title": session_title(f),
                    "mtime": st.st_mtime, "size": st.st_size})
    return out


def find_session(sid):
    if not re.fullmatch(r"[0-9a-f-]{36}", sid):
        raise ValueError("bad session id")
    hits = list((Path.home() / ".claude/projects").glob(f"*/{sid}.jsonl"))
    if not hits:
        raise FileNotFoundError("找不到这个会话")
    return hits[0]


def colorize(text):
    text = re.sub(r"^(## .*)$", lambda m: BOLD + m.group(1) + RESET, text, flags=re.M)
    text = text.replace("【偏离主线】", RED + "【偏离主线】" + RESET)
    text = text.replace("【支线】", YELLOW + "【支线】" + RESET)
    text = text.replace("【主线推进】", GREEN + "【主线推进】" + RESET)
    return text


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="终端科研管家")
    ap.add_argument("project", nargs="?", default=".", help="研究项目目录（默认当前目录）")
    ap.add_argument("--pick", action="store_true", help="手动选择要盯的工作会话")
    ap.add_argument("-s", "--summary", action="store_true", help="只做一次归纳就退出")
    args = ap.parse_args()
    lab = Lab(args.project, args.pick)
    if args.summary:
        lab.handle("")
    else:
        lab.loop()
