"""读 Claude Code 的会话记录（~/.claude/projects 下的 jsonl，只读）：列表、标题、压缩动态、cwd、上下文用量。"""
import json
import re
import time
from pathlib import Path

from .settings import load_settings


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


def project_sessions(project, exclude=()):
    """项目根目录和它所有子目录里的会话，最新的在前（终端版 lab 用）。
    在子目录里开的会话记在子目录自己的文件夹下，只看根目录的文件夹会漏掉。
    文件夹名是把路径里的非字母数字换成 -，会和别的项目撞前缀（proj-old），所以按会话记录里的 cwd 再确认一次；
    管家自己的会话（cwd 在 .research 里）不算。"""
    root = Path(project).resolve()
    prefix = sessions_dir(root).name
    files = list_sessions(root, exclude)
    base = Path.home() / ".claude/projects"
    for d in base.glob(prefix + "-*") if base.exists() else []:
        for f in d.glob("*.jsonl"):
            cwd = f.stem not in exclude and session_cwd(f)
            if cwd and Path(cwd).resolve().is_relative_to(root) \
                    and not Path(cwd).resolve().is_relative_to(root / ".research"):
                files.append(f)
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
