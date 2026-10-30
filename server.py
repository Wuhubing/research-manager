#!/usr/bin/env python3
"""科研管家 — 本地服务（127.0.0.1:8765），网页和桌面 App 都连它。

这里只放 HTTP 路由，逻辑都在 research_manager/ 包里。接口列表见 docs/ARCHITECTURE.md。
只依赖 Python 标准库；管家走本机的 `claude -p`，语音识别走 OpenAI。
"""
import atexit
import json
import os
import re
import shutil
import signal
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from research_manager import manager, sessions, settings, voice, watch
from research_manager.macos import open_terminal_session

ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get("RM_PORT", "8765"))


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
            if self.path == "/api/sessions":
                return self.send_json(sessions.all_sessions())
            if self.path == "/api/watch":
                return self.send_json(watch.load_watch())
            if self.path == "/api/watched":  # 菜单栏用：关注的会话及其状态
                ids = watch.load_watch()
                found = {}
                for i in ids:
                    try:
                        p = sessions.find_session(i)
                        found[i] = {"id": i, "title": sessions.session_title(p), "mtime": p.stat().st_mtime,
                                    "context": sessions.context_usage(p)}
                    except Exception:
                        pass
                return self.send_json([found[i] for i in ids if i in found])
            m = re.fullmatch(r"/api/sessions/([0-9a-f-]+)", self.path)
            if m:
                return self.send_json(manager.session_detail(m.group(1)))
            if self.path == "/api/status":
                return self.send_json({"openai": bool(settings.openai_key()),
                                       "claude": bool(shutil.which("claude"))})
            if self.path == "/api/settings":
                return self.send_json(settings.settings_view())
            if self.path == "/api/quick_record/last":
                return self.send_json({k: voice.QUICK[k] for k in ("state", "text", "error")})
            if self.path == "/api/asks":  # 正在进行的管家请求（排队中 / 思考中）
                return self.send_json(manager.asking())
        except Exception as ex:
            return self.send_json({"error": str(ex)}, 400)
        return super().do_GET()

    def do_POST(self):
        try:
            if self.path.startswith("/api/transcribe"):
                return self.send_json({"text": voice.transcribe_upload(self.body(), self.headers.get("Content-Type", ""))})

            # 只接受 JSON：别的网页无法不经预检就跨域发 JSON，防止被偷偷改设置
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                return self.send_json({"error": "需要 application/json"}, 415)
            req = json.loads(self.body() or b"{}")
            if self.path == "/api/settings":
                if req.get("openai_key"):
                    key = req["openai_key"].strip()
                    if not re.fullmatch(r"sk-[A-Za-z0-9_\-]{20,}", key):
                        return self.send_json({"error": "key 格式不对，应该以 sk- 开头"}, 400)
                    settings.set_openai_key(key)
                if req.get("asr_model") in settings.ASR_MODELS:
                    settings.save_settings(asr_model=req["asr_model"])
                if req.get("context_window") in settings.CONTEXT_WINDOWS:
                    settings.save_settings(context_window=req["context_window"])
                return self.send_json(settings.settings_view())
            m = re.fullmatch(r"/api/quick_record/(start|stop|copy)", self.path)
            if m:
                return self.send_json(voice.quick_record(m.group(1), req))
            if self.path == "/api/watch":
                watch.save_watch(req.get("ids", []))
                return self.send_json(watch.load_watch())
            m = re.fullmatch(r"/api/sessions/([0-9a-f-]+)/ask", self.path)
            if m:
                said = req.get("said", "").strip()
                reply = manager.ask_session(m.group(1), said, want_prompt=bool(said) and req.get("want_prompt", True))
                mainline, shown, prompt = manager.parse_reply(reply)
                return self.send_json({"reply": shown, "prompt": prompt, "mainline": mainline,
                                       "time": time.time(), "said": said})
            m = re.fullmatch(r"/api/sessions/([0-9a-f-]+)/(mainline|handoff|newsession)", self.path)
            if m:
                path = sessions.find_session(m.group(1))
                mgr = manager.Lab(sessions.session_cwd(path), work=path)
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
                    open_terminal_session(mgr.project, pfile)
                    watch.adopt_new_session(mgr.project, m.group(1), started)
                    return self.send_json({"ok": True})
            return self.send_json({"error": "not found"}, 404)
        except Exception as ex:
            return self.send_json({"error": str(ex)}, 500)


def on_sigterm(*_):
    voice.stop_quick_recording()
    raise SystemExit(0)


if __name__ == "__main__":
    voice.kill_stale_recordings()
    atexit.register(voice.stop_quick_recording)
    signal.signal(signal.SIGTERM, on_sigterm)
    print(f"科研管理器运行在 http://localhost:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
