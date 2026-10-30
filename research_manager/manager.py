"""管家：调用只读的 claude -p，读工作会话的新动态，记录历史、主线和交接。"""
import json
import os
import re
import subprocess
import time
from pathlib import Path

from .prompts import HANDOFF_SYSTEM, MAINLINE_TEMPLATE, SYSTEM
from .sessions import clip, condense, context_usage, find_session, project_sessions, session_cwd, session_title
from .watch import load_links


CLAUDE_MODEL = os.environ.get("LAB_CLAUDE_MODEL", "")

DIM, BOLD, RED, GREEN, YELLOW, CYAN, RESET = "\033[2m", "\033[1m", "\033[31m", "\033[32m", "\033[33m", "\033[36m", "\033[0m"

# 不放行不限路径的 Read/Grep/Glob（那等于能读本机所有文件）。项目目录靠 --add-dir 可读，
# 当前工作会话的记录在 run_claude() 里按单个文件放行。
ALLOWED_TOOLS = ["Bash(git status:*)", "Bash(git diff:*)",
                 "Bash(git log:*)", "Bash(git show:*)", "Bash(ls:*)"]
SECRET_PATTERNS = ["**/.env*", "**/*.pem", "**/*.key", "**/id_rsa*", "**/.ssh/**"]


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
        files = project_sessions(self.project, exclude={self.state.get("manager_session")})
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
        latest = [] if self.pinned else project_sessions(self.project, exclude={self.state.get("manager_session")})
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


def parse_reply(reply):
    """拆出 (建议的新主线 | None, 去掉主线块后的正文, prompt | None)。"""
    m = re.search(r"<mainline>\s*(.*?)\s*</mainline>", reply, re.S)
    shown = re.sub(r"<mainline>.*?</mainline>", "", reply, flags=re.S).strip()
    p = re.search(r"```prompt\s*\n(.*?)```", reply, re.S)
    return (m.group(1) if m else None), shown, (p.group(1).strip() if p else None)


def session_detail(sid):
    path = find_session(sid)
    cwd = session_cwd(path)
    size = path.stat().st_size
    activity, _ = condense(path, max(0, size - 2_000_000), budget=120_000)
    rdir = git_root(cwd) / ".research"
    mainline = (rdir / "mainline.md").read_text() if (rdir / "mainline.md").exists() else MAINLINE_TEMPLATE
    history = json.loads((rdir / "history.json").read_text()) if (rdir / "history.json").exists() else []
    for h in history:
        _, h["reply"], h["prompt"] = parse_reply(h["reply"])
    prev = load_links().get(sid)
    try:
        prev_title = session_title(find_session(prev)) if prev else None
    except Exception:
        prev_title = None
    return {"id": sid, "cwd": cwd, "title": session_title(path), "mtime": path.stat().st_mtime,
            "activity": activity, "mainline": mainline, "history": history[-30:],
            "context": context_usage(path),
            "continues": {"id": prev, "title": prev_title} if prev else None,
            "handoff": Lab(cwd, work=path).handoff_checkpoint() if rdir.exists() else None}
