"""关注的会话列表和交接后的接替关系，存在 ~/.research-manager/watch.json、links.json。"""
import json
import re
import threading
import time
from pathlib import Path

from .sessions import list_sessions


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
            new = [f for f in list_sessions(cwd) if f.stat().st_mtime > started and f.stem != old_id]
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
