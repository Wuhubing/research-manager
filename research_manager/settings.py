"""OpenAI key 和本机设置，存在 ~/.research-manager/。"""
import json
import os
from pathlib import Path


OPENAI_KEY_FILE = Path.home() / ".research-manager/openai_key"
SETTINGS_FILE = Path.home() / ".research-manager/settings.json"
ASR_MODELS = ["gpt-4o-transcribe", "gpt-4o-mini-transcribe"]
CONTEXT_WINDOWS = {"auto": "自动", "200000": "200K", "1000000": "1M"}


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


def settings_view():
    """给设置页看的内容：key 只露最后 4 位。"""
    key = openai_key()
    return {"openai_key_set": bool(key), "openai_key_hint": f"sk-…{key[-4:]}" if key else "",
            "openai_key_from_env": bool(os.environ.get("OPENAI_API_KEY")),
            "asr_model": asr_model(), "asr_models": ASR_MODELS,
            "context_window": str(load_settings().get("context_window") or "auto"),
            "context_windows": CONTEXT_WINDOWS}
