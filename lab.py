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

# 不放行不限路径的 Read/Grep/Glob（那等于能读本机所有文件）。项目目录靠 --add-dir 可读，
# 当前工作会话的记录在 run_claude() 里按单个文件放行。
ALLOWED_TOOLS = ["Bash(git status:*)", "Bash(git diff:*)",
                 "Bash(git log:*)", "Bash(git show:*)", "Bash(ls:*)"]
SECRET_PATTERNS = ["**/.env*", "**/*.pem", "**/*.key", "**/id_rsa*", "**/.ssh/**"]

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

HANDOFF_SYSTEM = """你是研究者的科研管家。研究者的一个 Claude Code 工作会话上下文快满了，要换一个新会话继续。
你的任务：读完整个旧会话，写一份交接文档，让新会话里的 agent 不丢任何重要信息，也不重复踩坑。
需要时可以用只读工具核实项目里的文件和 git 状态（比如确认哪些改动已经提交）。

只写真实发生过、你能从记录或文件里确认的内容；不确定的标“待确认”。具体到文件路径、命令、数字。

输出格式（中文 markdown，小节没有内容就写“无”）：

# 交接：<一句话说明这个会话在做什么>

## 目标和研究主线
## 已完成
（做了什么、关键结果和数字、改动或新增的文件、是否已提交）
## 关键决定和原因
（选了什么方案、为什么、否决了什么）
## 当前进度
（停在哪一步、哪些东西还没验证、正在跑的东西）
## 不要做的事
（研究者明确说过不许做的、之前 agent 越界被纠正过的）
## 下一步
## 建议写进 CLAUDE.md 的长期信息
（每次新会话都该知道的约定，没有就写“无”）

## Prompt
```prompt
<新会话的第一条消息：说明这是接续之前的工作，概括上面的要点（交接文档的路径会由系统自动加在开头，你不用写），
写明下一步要做什么和范围边界，并要求 agent 先复述它理解的当前状态、等研究者确认后再动手>
```"""

MAINLINE_TEMPLATE = """# 研究主线

- **核心问题**：
- **当前假设**：
- **当前焦点**：
- **明确不做的事**：
"""


# ---------------------------------------------------------------- 工作会话

def sessions_dir(project):
    return Path.home() / ".claude/projects" / re.sub(r"[^A-Za-z0-9]", "-", str(project))


_FILE_CACHE = {}


def _cached(kind, path, compute):
    """文件没变（修改时间、大小相同）就复用上次的结果。会话文件可能有几十 MB，每次重读很慢。"""
    st = Path(path).stat()
    key = (kind, str(path))
    hit = _FILE_CACHE.get(key)
    if hit and hit[0] == (st.st_mtime, st.st_size):
        return hit[1]
    value = compute()
    _FILE_CACHE[key] = ((st.st_mtime, st.st_size), value)
    return value


def session_title(path):
    return _cached("title", path, lambda: _session_title(path))


def _session_title(path):
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


def headtail(s, n):
    """超过 n 字时保留开头 2/3 和结尾 1/3，中间注明省略了多少字（agent 的结论常在结尾）。"""
    s = str(s).strip()
    if len(s) <= n:
        return s
    head, tail = n * 2 // 3, n // 3
    return f"{s[:head]}…（中间省略 {len(s) - head - tail} 字）…{s[-tail:]}"


def condense(path, offset, budget=40000):
    """把工作会话 jsonl 从 offset 开始压缩成可读的动态记录。返回 (文本, 新 offset)。

    - 研究者的话、agent 每轮最后一段话（通常是汇报）保留全文；agent 其他长段落保留开头和结尾
    - 只处理到最后一个完整的行：会话可能正在写入，没写完的那行留到下次再读，不会丢
    """
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
    end = data.rfind(b"\n") + 1
    data, new_offset = data[:end], offset + end
    out = []
    last_agent = None   # 本轮最新一段 agent 文字在 out 里的位置；被后面的 agent 文字盖过时才压缩它

    def settle_agent(full):
        nonlocal last_agent
        if last_agent is not None:
            i, text = last_agent
            out[i] = f"🤖 agent：{text.strip() if full else headtail(text, 1500)}"
            last_agent = None

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
                    settle_agent(full=True)      # 研究者开口了：上一段就是那一轮的最后一段，保留全文
                    out.append(f"\n👤 研究者：{text.strip()}")
                else:
                    settle_agent(full=False)     # 后面还有话：前一段不是最后一段，保留开头和结尾
                    out.append(None)
                    last_agent = (len(out) - 1, text)
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
    settle_agent(full=True)                      # 到目前为止的最后一段（可能是正在进行的这一轮）保留全文
    text = "\n".join(out)
    if len(text) > budget:
        text = "（更早的内容已省略）…\n" + text[-budget:]
    return text, new_offset


# ---------------------------------------------------------------- 录音 / 识别

REC_PREFIX = "rm-rec-"     # 录音临时文件名前缀，用来找出残留的录音进程
REC_MAX_SECONDS = 600      # 单次录音最长 10 分钟；就算启动它的进程意外退出，ffmpeg 也会自己停


def start_recording():
    """后台开始录音，返回 (进程, wav 路径)。终端版和菜单栏录音共用。"""
    with tempfile.NamedTemporaryFile(prefix=REC_PREFIX, suffix=".wav", delete=False) as tmp:
        wav = tmp.name
    p = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "avfoundation", "-i", MIC,
                          "-t", str(REC_MAX_SECONDS), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", wav],
                         stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(0.3)
    if p.poll() is not None:
        raise RuntimeError("录音启动失败：" + p.stderr.read().decode("utf-8", "ignore")[-300:])
    return p, wav


def kill_stale_recordings():
    """停掉上次残留的录音进程（比如服务被强制结束时没来得及停的），并删掉它们的临时文件。"""
    subprocess.run(["pkill", "-f", f"avfoundation.*{REC_PREFIX}"], capture_output=True)
    for f in Path(tempfile.gettempdir()).glob(f"{REC_PREFIX}*.wav"):
        f.unlink(missing_ok=True)


def stop_recording(p, wav):
    try:
        p.communicate(b"q", timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()
    if p.returncode not in (0, 255) or not Path(wav).exists() or Path(wav).stat().st_size < 2000:
        err = (p.stderr.read().decode("utf-8", "ignore") if p.stderr and not p.stderr.closed else "")
        raise RuntimeError("录音失败。请在「系统设置 → 隐私与安全性 → 麦克风」里允许录音的 App。" + err[-300:])
    return wav


def record():
    p, wav = start_recording()
    t0 = time.time()
    try:
        input(f"{RED}● 录音中…{RESET} 说完按回车结束 ")
    except (KeyboardInterrupt, EOFError):
        pass
    stop_recording(p, wav)
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

def git_root(path):
    """会话可能停在项目的子目录里，管家的项目目录要用 git 根目录；不是 git 仓库就用原目录。"""
    try:
        r = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return Path(r.stdout.strip()).resolve()
    except Exception:
        pass
    return Path(path).resolve()


class Lab:
    def __init__(self, project, pick=False, work=None):
        self.project = git_root(project)
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

    def raw_hint(self):
        """动态是压缩过的；告诉管家原始记录在哪，需要核实原话时可以用 Grep 查"""
        if not self.work:
            return ""
        return (f"（下面是压缩过的记录：长段落省略了中间，工具的正常输出没有包含。"
                f"原始记录在 {self.work}，每行一个 JSON，需要核实原话或工具输出时用 Grep 搜关键词，不要整个读。）\n")

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

    def ask(self, said, want_prompt=False):
        activity = self.new_activity()
        msg = (f"# 研究主线\n{self.mainline()}\n\n"
               f"# 工作 agent 会话的新动态（会话：{session_title(self.work) if self.work else '无'}）\n{self.raw_hint()}{activity}\n\n"
               f"# 研究者这次说的话\n{said or '（研究者没说话，只想要一次归纳）'}")
        if want_prompt:
            msg += ("\n\n# 这次必须输出 Prompt\n"
                    "不管研究者是在派任务、聊想法还是问情况，回复最后都要有 ## Prompt 和 ```prompt 代码块，"
                    "把他的意思整理成一条能直接发给工作 agent 的 prompt。"
                    "如果他只是问情况，就写一条让 agent 去核实并汇报这件事的 prompt。")
        return self.run_claude(msg)

    def run_claude(self, msg, resume=True, system=SYSTEM):
        """调用只读的管家。resume=True 时接着管家自己的长期会话；False 时是一次性调用（不占用管家的上下文）。"""
        cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose",
               "--permission-mode", "default",
               "--tools", "Read,Grep,Glob,Bash",
               "--allowedTools", *ALLOWED_TOOLS,
               "--add-dir", str(self.project)]
        if self.work:
            # 只放行当前这一个会话记录（// 开头表示绝对路径），同一文件夹里的其他会话和子 agent 记录都读不到
            cmd += ["--allowedTools", f"Read(/{self.work})", f"Grep(/{self.work})"]
        # 项目里的密钥类文件一律不许读（Grep/Glob 也会跳过它们）
        cmd += ["--disallowedTools", *(f"{tool}(/{self.project}/{pattern})" for tool in ("Read", "Grep")
                                       for pattern in SECRET_PATTERNS)]
        if CLAUDE_MODEL:
            cmd += ["--model", CLAUDE_MODEL]
        if resume and self.state.get("manager_session"):
            cmd += ["--resume", self.state["manager_session"]]
        else:
            cmd += ["--append-system-prompt", system]
        if not resume:
            cmd += ["--no-session-persistence"]
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
        if resume and not self.state.get("manager_session"):
            self.state["manager_session"] = result["session_id"]
        self.save()   # 成功后才保存（包括 new_activity 推进的 offset）；失败的话下次会重新读这段
        return result["result"]

    def handoff_checkpoint(self):
        """上次为这个会话生成的交接；up_to_date 表示会话之后没有任何变化。"""
        if not self.work:
            return None
        cp = json.loads((self.dir / "handoffs.json").read_text()) if (self.dir / "handoffs.json").exists() else {}
        rec = cp.get(self.work.stem)
        if not rec or not Path(rec["path"]).exists():
            return None
        return {**rec, "up_to_date": rec["size"] == self.work.stat().st_size}

    def handoff(self, force=False):
        """读完整个工作会话，生成交接文档和新会话的第一条 prompt，存到 .research/handoff-*.md。
        会话自上次交接后没有变化时直接复用上次的结果（检查点），force=True 强制重新生成。"""
        if not self.work:
            raise RuntimeError("没有工作会话")
        rec = self.handoff_checkpoint()
        if rec and rec["up_to_date"] and not force:
            path = Path(rec["path"])
            return {"doc": path.read_text(), "prompt": path.with_suffix(".prompt.txt").read_text(),
                    "path": str(path), "time": rec["time"], "cached": True}
        size = self.work.stat().st_size  # 生成过程中会话又变了，就不算最新
        full, _ = condense(self.work, 0, budget=150_000)
        recent = "\n\n".join(f"- {time.strftime('%m-%d %H:%M', time.localtime(h['time']))} "
                              f"我说：{h['said'] or '（只要归纳）'}\n{clip(h['reply'], 1500)}"
                              for h in self.history()[-8:] if h.get("session") == self.work.stem)
        msg = (f"# 研究主线\n{self.mainline()}\n\n"
               f"# 管家之前对这个会话的归纳（最近几次）\n{recent or '（没有）'}\n\n"
               f"# 工作会话的完整记录（会话：{session_title(self.work)}，项目目录：{self.project}）\n{self.raw_hint()}{full}")
        doc = self.run_claude(msg, resume=False, system=HANDOFF_SYSTEM)
        path = self.dir / f"handoff-{time.strftime('%Y%m%d-%H%M%S')}.md"
        path.write_text(doc + "\n")
        _, _, prompt = parse_reply(doc)
        prompt = f"交接文档在 {path}，请先用 Read 完整读一遍。\n\n{prompt or doc}"
        path.with_suffix(".prompt.txt").write_text(prompt)
        cpf = self.dir / "handoffs.json"
        cp = json.loads(cpf.read_text()) if cpf.exists() else {}
        cp[self.work.stem] = {"path": str(path), "size": size, "time": time.time()}
        cpf.write_text(json.dumps(cp, ensure_ascii=False, indent=1))
        return {"doc": doc, "prompt": prompt, "path": str(path), "time": cp[self.work.stem]["time"], "cached": False}

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
        reply = self.ask(said, want_prompt=bool(said))
        self.log(said, reply)
        mainline, shown, prompt = parse_reply(reply)
        print("\n" + colorize(shown) + "\n")
        if prompt:
            copy_to_clipboard(prompt)
            print(f"{GREEN}✓ prompt 已复制到剪贴板，粘贴给工作 agent 即可{RESET}")
        if mainline:
            print(f"{CYAN}管家建议更新主线：{RESET}\n{mainline}\n")
            if input("采纳？[y/N] ").strip().lower() == "y":
                self.mainline_file.write_text(mainline + "\n")
                print(f"{GREEN}✓ 主线已更新（.research/mainline.md）{RESET}")

    def do_handoff(self, force=False):
        rec = self.handoff_checkpoint()
        if rec and rec["up_to_date"] and not force:
            print(f"{DIM}  会话自上次交接后没有变化，直接用上次的交接（/h! 强制重新生成）{RESET}")
        else:
            print(f"{DIM}  读完整个会话、整理交接文档，约 1–2 分钟…{RESET}")
        h = self.handoff(force)
        print("\n" + colorize(re.sub(r"```prompt.*?```", "", h["doc"], flags=re.S)) + "\n")
        copy_to_clipboard(h["prompt"])
        print(f"{GREEN}✓ 交接文档已保存：{h['path']}，新会话的第一条消息已复制到剪贴板{RESET}")
        if input("在 Terminal 里新开一个 claude 会话？[y/N] ").strip().lower() == "y":
            open_terminal_session(self.project, Path(h["path"]).with_suffix(".prompt.txt"))
            print(f"{GREEN}✓ 已在 Terminal 打开新会话{RESET}")

    def loop(self):
        print(f"\n{BOLD}🔬 lab · {self.project.name}{RESET}")
        if self.work:
            c = context_usage(self.work)
            color = RED if c["pct"] >= 85 else YELLOW if c["pct"] >= 70 else DIM
            print(f"{DIM}  工作会话：{session_title(self.work)}{RESET}  "
                  f"{color}上下文 {c['tokens'] // 1000}K / {c['window'] // 1000}K · {c['pct']}%{RESET}")
        print(f"{DIM}  主线：.research/mainline.md   日志：.research/log.md{RESET}")
        print(f"{DIM}  [回车] 录音   [直接打字+回车] 文字输入   /s 只归纳   /h 交接到新会话   /m 看主线   /p 换工作会话   /q 退出{RESET}\n")
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
                if line in ("/h", "/h!"):
                    self.do_handoff(force=line == "/h!")
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


CONTEXT_WINDOWS = {"auto": "自动", "200000": "200K", "1000000": "1M"}


def context_window(tokens=0):
    """设置里指定了就用指定的；"自动"时默认 200K，已经超过 200K 的会话说明模型支持 1M。"""
    w = str(load_settings().get("context_window") or "auto")
    if w != "auto":
        return int(w)
    return 1_000_000 if tokens > 200_000 else 200_000


def context_usage(path):
    return _cached("context", path, lambda: _context_usage(path))


def _context_usage(path):
    """从会话记录末尾找最近一轮主对话的 token 用量，估算当前上下文大小。"""
    with open(path, "rb") as f:
        f.seek(max(0, path.stat().st_size - 2_000_000))
        lines = f.read().decode("utf-8", "ignore").splitlines()
    for raw in reversed(lines):
        if '"usage"' not in raw:
            continue
        try:
            d = json.loads(raw)
        except Exception:
            continue
        msg = d.get("message")
        if d.get("type") != "assistant" or d.get("isSidechain") or not isinstance(msg, dict) or not msg.get("usage"):
            continue
        u = msg["usage"]
        tokens = sum(u.get(k, 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        win = context_window(tokens)
        return {"tokens": tokens, "window": win, "pct": round(100 * tokens / win), "model": msg.get("model")}
    return {"tokens": 0, "window": context_window(), "pct": 0, "model": None}


def open_terminal_session(cwd, prompt_file):
    """在 Terminal 里打开一个新的 claude 会话，第一条消息是交接 prompt。"""
    import shlex
    shell = f"cd {shlex.quote(str(cwd))} && claude \"$(cat {shlex.quote(str(prompt_file))})\""
    script = shell.replace("\\", "\\\\").replace('"', '\\"')
    subprocess.run(["osascript", "-e", f'tell application "Terminal" to do script "{script}"',
                    "-e", 'tell application "Terminal" to activate'], check=True, capture_output=True)


def copy_to_clipboard(text):
    """写入系统剪贴板。pbcopy 按 __CF_USER_TEXT_ENCODING 决定编码，从 App 启动的服务里没有它，
    中文会变成问号，所以明确指定 UTF-8（0x08000100）。"""
    env = {**os.environ, "LANG": "en_US.UTF-8", "__CF_USER_TEXT_ENCODING": f"{os.getuid():#x}:0x08000100:0x08000100"}
    subprocess.run("pbcopy", input=text.encode("utf-8"), env=env)


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
