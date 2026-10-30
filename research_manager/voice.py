"""录音（ffmpeg）、语音识别（OpenAI），以及菜单栏 ⌃P 录音的状态。"""
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .macos import copy_to_clipboard, notify
from .settings import OPENAI_KEY_FILE, asr_model, openai_key


MIC = os.environ.get("LAB_MIC", ":0")  # ffmpeg avfoundation 音频设备，:0 = 内置麦克风
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


ASR_CONTEXT = "科研讨论，中文为主，夹杂英文术语。"


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


# ---------------------------------------------------------------- 网页录音

AUDIO_EXT = {"audio/webm": ".webm", "audio/mp4": ".mp4", "audio/mpeg": ".mp3", "audio/wav": ".wav",
             "audio/x-m4a": ".m4a", "audio/ogg": ".ogg"}


def transcribe_upload(audio_bytes, content_type="", hint=""):
    ext = AUDIO_EXT.get(content_type.split(";")[0].strip(), ".webm")
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / f"audio{ext}"
        src.write_bytes(audio_bytes)
        return transcribe(src, hint)


# ---------------------------------------------------------------- 菜单栏录音（⌃P）

# 最近一次录音：识别出的原话（自动复制）
QUICK = {"state": "idle", "proc": None, "wav": None, "text": "", "error": ""}


def quick_record(action, req):
    if action == "start":
        if QUICK["state"] == "recording":
            return {"error": "已经在录音"}
        try:
            QUICK["proc"], QUICK["wav"] = start_recording()
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
            wav = stop_recording(QUICK["proc"], QUICK["wav"])
            text = transcribe(wav).strip()
            os.unlink(wav)
            if not text:
                raise RuntimeError("没听清，再说一次")
        except Exception as ex:
            QUICK.update(state="idle", error=str(ex))
            notify("录音失败", str(ex))
            return {"error": str(ex)}
        copy_to_clipboard(text)
        QUICK.update(state="idle", text=text, error="")
        return {"state": "idle", "text": text}
    # copy：再复制一次最近的原话
    if not QUICK["text"]:
        return {"error": "还没有内容"}
    copy_to_clipboard(QUICK["text"])
    return {"ok": True}


def stop_quick_recording(*_):
    """服务退出时停掉正在进行的录音，避免 ffmpeg 留在后台一直占着麦克风。"""
    p = QUICK.get("proc")
    if p and p.poll() is None:
        p.kill()
    if QUICK.get("wav"):
        Path(QUICK["wav"]).unlink(missing_ok=True)
