#!/usr/bin/env python3
"""冒烟测试：重构时用来确认功能没变。运行：python3 tests/smoke.py（全部通过退出码为 0）

- HOME 指向临时目录，里面放几份假的 Claude Code 会话记录；不碰真实的 ~。
- 在本进程里另起一个服务（127.0.0.1:8799），用的是 server.py 里同一套路由，不碰 8765。
  不用 `python3 server.py` 启动，因为它启动时会停掉本机所有残留的录音进程（kill_stale_recordings）。
- 抓一次 Lab.run_claude() 生成的 claude 命令（会话在项目子目录里），检查权限参数。
- 不调用 claude、OpenAI、ffmpeg。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PORT = 8799
REAL_HOME = Path.home()

# ---------------------------------------------------------------- 假数据（必须在 import lab/server 之前设好 HOME）

T = Path(tempfile.mkdtemp(prefix="rm-smoke-")).resolve()
os.environ["HOME"] = str(T)
os.environ["RM_DATA_DIR"] = str(T / "data")
os.environ.pop("OPENAI_API_KEY", None)
os.environ.pop("LAB_OPENAI_ASR_MODEL", None)

PROJ = T / "proj"                 # 一个 git 仓库
SUB = PROJ / "sub" / "deep"       # 工作会话的 cwd 在仓库的子目录里
OTHER = T / "other"               # 另一个不是 git 仓库的项目
for d in (SUB, OTHER):
    d.mkdir(parents=True)
subprocess.run(["git", "init", "-q", str(PROJ)], check=True)

MAIN_ID = "11111111-1111-4111-8111-111111111111"     # 子目录里的会话，有自定义标题和 token 用量
OTHER_ID = "22222222-2222-4222-8222-222222222222"    # 另一个项目的会话，标题取第一条消息
MGR_ID = "33333333-3333-4333-8333-333333333333"      # 管家自己的会话（cwd 以 /.research 结尾），不该出现在列表里


def session_file(cwd, sid, lines):
    d = T / ".claude/projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{sid}.jsonl"
    f.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines))
    return f


def user(cwd, text):
    return {"type": "user", "cwd": str(cwd), "message": {"role": "user", "content": text}}


def agent(cwd, text, usage=None):
    msg = {"role": "assistant", "model": "claude-smoke", "content": [{"type": "text", "text": text}]}
    if usage:
        msg["usage"] = usage
    return {"type": "assistant", "cwd": str(cwd), "message": msg}


USAGE = {"input_tokens": 1000, "cache_read_input_tokens": 50000, "cache_creation_input_tokens": 2000, "output_tokens": 10}
MAIN = session_file(SUB, MAIN_ID, [
    user(SUB, "SMOKE_USER_TEXT 请跑一下实验"),
    agent(SUB, "SMOKE_AGENT_TEXT 实验跑完了", USAGE),
    {"type": "custom-title", "customTitle": "冒烟测试会话"},
])
session_file(OTHER, OTHER_ID, [user(OTHER, "另一个项目的第一条消息"), agent(OTHER, "好的")])
session_file(PROJ / ".research", MGR_ID, [user(PROJ / ".research", "管家自己的会话")])

# 记下真实 ~ 下的文件状态，最后确认没被改过
REAL_FILES = [REAL_HOME / ".research-manager" / n for n in ("watch.json", "links.json", "settings.json")]
real_before = {f: f.stat().st_mtime if f.exists() else None for f in REAL_FILES}

sys.path.insert(0, str(REPO))
import server  # noqa: E402
from research_manager import manager, prompts  # noqa: E402

# ---------------------------------------------------------------- 检查工具

failures = []


def check(name, ok, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + name + ("" if ok else f"  —— {detail}"))
    if not ok:
        failures.append(name)


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            code, raw = r.status, r.read()
    except urllib.error.HTTPError as ex:
        code, raw = ex.code, ex.read()
    try:
        return code, json.loads(raw)
    except ValueError:
        return code, raw.decode("utf-8", "ignore")[:200]   # 比如不匹配任何接口时的静态文件 404 页面


def main():
    server.Handler.log_message = lambda *a: None    # 不打印每个请求
    httpd = server.ThreadingHTTPServer(("127.0.0.1", PORT), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        http_checks()
        command_checks()
    finally:
        httpd.shutdown()
    check("真实的 ~/.research-manager 没被改动",
          all((f.stat().st_mtime if f.exists() else None) == real_before[f] for f in REAL_FILES))


def http_checks():
    print("接口")
    code, st = call("GET", "/api/status")
    check("GET /api/status → 200，有 openai / claude 两个布尔值",
          code == 200 and isinstance(st.get("openai"), bool) and isinstance(st.get("claude"), bool), (code, st))
    check("没有配置 key 时 openai 为 false", st.get("openai") is False, st)

    code, sessions = call("GET", "/api/sessions")
    ids = [s.get("id") for s in sessions] if isinstance(sessions, list) else []
    check("GET /api/sessions → 200，列出两个工作会话", code == 200 and set(ids) == {MAIN_ID, OTHER_ID}, (code, ids))
    check("管家自己的会话（cwd 以 /.research 结尾）不在列表里", MGR_ID not in ids, ids)
    m = next((s for s in sessions if s.get("id") == MAIN_ID), {}) if isinstance(sessions, list) else {}
    check("会话字段：cwd 是子目录、标题用自定义标题、有 mtime 和 size",
          m.get("cwd") == str(SUB) and m.get("title") == "冒烟测试会话"
          and isinstance(m.get("mtime"), (int, float)) and m.get("size") == MAIN.stat().st_size, m)
    o = next((s for s in sessions if s.get("id") == OTHER_ID), {}) if isinstance(sessions, list) else {}
    check("没有自定义标题时用第一条消息做标题", o.get("title") == "另一个项目的第一条消息", o)

    code, w = call("GET", "/api/watch")
    check("GET /api/watch → 200，一开始是空列表", code == 200 and w == [], (code, w))
    code, w = call("POST", "/api/watch", {"ids": [OTHER_ID, MAIN_ID, "not-a-session-id"]})
    check("POST /api/watch → 200，保留顺序、丢掉不合法的 id", code == 200 and w == [OTHER_ID, MAIN_ID], (code, w))
    code, w = call("GET", "/api/watch")
    check("再 GET /api/watch 读回刚写的列表", code == 200 and w == [OTHER_ID, MAIN_ID], (code, w))
    check("关注列表写在临时 HOME 里", json.loads((T / ".research-manager/watch.json").read_text()) == [OTHER_ID, MAIN_ID])
    code, w = call("POST", "/api/watch", None)
    check("POST 不是 JSON 时 → 415", code == 415, code)

    code, watched = call("GET", "/api/watched")
    check("GET /api/watched → 200，按关注顺序返回",
          code == 200 and [x.get("id") for x in watched] == [OTHER_ID, MAIN_ID], (code, watched))
    wm = next((x for x in watched if x.get("id") == MAIN_ID), {}) if isinstance(watched, list) else {}
    ctx = wm.get("context") or {}
    check("watched 里有标题、mtime 和上下文用量（53K / 200K = 26%）",
          wm.get("title") == "冒烟测试会话" and isinstance(wm.get("mtime"), (int, float))
          and ctx.get("tokens") == 53000 and ctx.get("window") == 200000 and ctx.get("pct") == 26, wm)

    code, d = call("GET", f"/api/sessions/{MAIN_ID}")
    check("GET /api/sessions/<id> → 200", code == 200, (code, d))
    d = d if isinstance(d, dict) else {}
    check("详情字段齐全", {"id", "cwd", "title", "mtime", "activity", "mainline", "history",
                          "context", "continues", "handoff"} <= d.keys(), sorted(d))
    act = d.get("activity") or ""
    check("动态里有研究者的话和 agent 的回答", "SMOKE_USER_TEXT" in act and "SMOKE_AGENT_TEXT" in act, act[:300])
    check("还没有主线时返回主线模板", d.get("mainline") == prompts.MAINLINE_TEMPLATE, d.get("mainline"))
    check("没有历史、接替关系和交接", d.get("history") == [] and d.get("continues") is None and d.get("handoff") is None,
          {k: d.get(k) for k in ("history", "continues", "handoff")})
    code, _ = call("GET", "/api/sessions/not-a-uuid")
    check("GET /api/sessions/<不合法 id> → 不是 200", code != 200, code)
    code, _ = call("GET", "/api/sessions/44444444-4444-4444-8444-444444444444")
    check("GET /api/sessions/<不存在的 id> → 不是 200", code != 200, code)

    code, s = call("GET", "/api/settings")
    check("GET /api/settings → 200，没有 key、默认识别模型、上下文窗口自动",
          code == 200 and s.get("openai_key_set") is False and s.get("openai_key_hint") == ""
          and s.get("asr_model") == "gpt-4o-transcribe" and s.get("context_window") == "auto"
          and "gpt-4o-mini-transcribe" in (s.get("asr_models") or []), (code, s))


def capture(mgr, **kw):
    """截获 run_claude() 要启动的命令，不真的调用 claude。"""
    got = {}

    class Captured(Exception):
        pass

    def fake(cmd, **popen_kw):
        got.update(cmd=cmd, cwd=popen_kw.get("cwd"))
        raise Captured

    real = manager.subprocess.Popen
    manager.subprocess.Popen = fake
    try:
        mgr.run_claude("smoke", **kw)
    except Captured:
        pass
    finally:
        manager.subprocess.Popen = real
    return got.get("cmd") or [], got.get("cwd")


def values(cmd, flag):
    """取出某个参数后面的值（直到下一个 -- 开头的参数），同一参数出现多次时合并。"""
    out, i = [], 0
    while i < len(cmd):
        if cmd[i] == flag:
            i += 1
            while i < len(cmd) and not cmd[i].startswith("--"):
                out.append(cmd[i])
                i += 1
        else:
            i += 1
    return out


def command_checks():
    print("管家的权限（会话在项目子目录里）")
    check("git_root(子目录) 是仓库根目录", manager.git_root(SUB) == PROJ, manager.git_root(SUB))
    check("不是 git 仓库时 git_root 退回原目录", manager.git_root(OTHER) == OTHER, manager.git_root(OTHER))

    mgr = manager.Lab(SUB, work=MAIN)       # 先创建（git_root 要真的调用 git），再截获命令
    check("管家的项目目录是 git 根目录", mgr.project == PROJ, mgr.project)
    cmd, cwd = capture(mgr)
    check("截获到 claude -p 命令", cmd[:2] == ["claude", "-p"], cmd[:3])
    check("在 <git 根目录>/.research 里运行", Path(cwd or "") == PROJ / ".research", cwd)
    check("--permission-mode default", values(cmd, "--permission-mode") == ["default"], values(cmd, "--permission-mode"))
    check("--tools 只有 Read,Grep,Glob,Bash（没有写文件的工具）",
          values(cmd, "--tools") == ["Read,Grep,Glob,Bash"], values(cmd, "--tools"))
    check("--add-dir 是 git 根目录（不是会话所在的子目录）", values(cmd, "--add-dir") == [str(PROJ)], values(cmd, "--add-dir"))

    allowed = values(cmd, "--allowedTools")
    expected = ["Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)", "Bash(ls:*)",
                f"Read(/{MAIN})", f"Grep(/{MAIN})"]
    check("--allowedTools 只有 5 条 git/ls 规则 + 当前会话记录的单文件 Read/Grep",
          allowed == expected, allowed)
    check("没有不限路径的 Read / Grep / Glob / Bash", not {"Read", "Grep", "Glob", "Bash"} & set(allowed), allowed)

    denied = values(cmd, "--disallowedTools")
    secrets = ["**/.env*", "**/*.pem", "**/*.key", "**/id_rsa*", "**/.ssh/**"]
    check("--disallowedTools 覆盖 git 根目录下的 5 类密钥文件（Read 和 Grep 各一条）",
          set(denied) == {f"{t}(/{PROJ}/{p})" for t in ("Read", "Grep") for p in secrets}, denied)
    check("第一次调用：没有 --resume，带上管家的系统提示词",
          "--resume" not in cmd and values(cmd, "--append-system-prompt") == [prompts.SYSTEM], cmd[-4:])

    mgr.state["manager_session"] = "55555555-5555-4555-8555-555555555555"
    cmd, _ = capture(mgr)
    check("有管家会话后：--resume 接着它的会话，不再带系统提示词",
          values(cmd, "--resume") == ["55555555-5555-4555-8555-555555555555"] and "--append-system-prompt" not in cmd,
          cmd[-4:])
    cmd, _ = capture(mgr, resume=False, system=prompts.HANDOFF_SYSTEM)
    check("一次性调用（交接）：不 --resume，用交接提示词，不保存会话",
          "--resume" not in cmd and values(cmd, "--append-system-prompt") == [prompts.HANDOFF_SYSTEM]
          and "--no-session-persistence" in cmd, cmd[-4:])


if __name__ == "__main__":
    t0 = time.time()
    try:
        main()
    finally:
        shutil.rmtree(T, ignore_errors=True)
    print(f"\n{'全部通过' if not failures else f'{len(failures)} 项失败：' + '；'.join(failures)}"
          f"（{time.time() - t0:.1f}s）")
    sys.exit(1 if failures else 0)
