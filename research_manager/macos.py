"""macOS 上的系统操作：剪贴板、通知、在 Terminal 里开新会话。"""
import os
import subprocess


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


def notify(title, msg):
    # 用参数传文字，不用拼进脚本，引号、中文都不用转义
    msg = " ".join(msg.split())[:120]
    subprocess.run(["osascript", "-e", "on run argv", "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
                    "-e", "end run", title, msg], capture_output=True)
