"""终端版管家 `lab`：在研究项目目录里运行，录音或打字 → 管家归纳 → prompt 复制到剪贴板。"""
import argparse
import re
import time
from pathlib import Path

from .macos import copy_to_clipboard, open_terminal_session
from .manager import BOLD, CYAN, DIM, GREEN, RED, RESET, YELLOW, Lab, parse_reply
from .sessions import context_usage, session_title
from .voice import start_recording, stop_recording, transcribe_recording


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


def colorize(text):
    text = re.sub(r"^(## .*)$", lambda m: BOLD + m.group(1) + RESET, text, flags=re.M)
    text = text.replace("【偏离主线】", RED + "【偏离主线】" + RESET)
    text = text.replace("【支线】", YELLOW + "【支线】" + RESET)
    text = text.replace("【主线推进】", GREEN + "【主线推进】" + RESET)
    return text


def handle(lab, said):
    reply = lab.ask(said, want_prompt=bool(said))
    lab.log(said, reply)
    mainline, shown, prompt = parse_reply(reply)
    print("\n" + colorize(shown) + "\n")
    if prompt:
        copy_to_clipboard(prompt)
        print(f"{GREEN}✓ prompt 已复制到剪贴板，粘贴给工作 agent 即可{RESET}")
    if mainline:
        print(f"{CYAN}管家建议更新主线：{RESET}\n{mainline}\n")
        if input("采纳？[y/N] ").strip().lower() == "y":
            lab.mainline_file.write_text(mainline + "\n")
            print(f"{GREEN}✓ 主线已更新（.research/mainline.md）{RESET}")

def do_handoff(lab, force=False):
    rec = lab.handoff_checkpoint()
    if rec and rec["up_to_date"] and not force:
        print(f"{DIM}  会话自上次交接后没有变化，直接用上次的交接（/h! 强制重新生成）{RESET}")
    else:
        print(f"{DIM}  读完整个会话、整理交接文档，约 1–2 分钟…{RESET}")
    h = lab.handoff(force)
    print("\n" + colorize(re.sub(r"```prompt.*?```", "", h["doc"], flags=re.S)) + "\n")
    copy_to_clipboard(h["prompt"])
    print(f"{GREEN}✓ 交接文档已保存：{h['path']}，新会话的第一条消息已复制到剪贴板{RESET}")
    if input("在 Terminal 里新开一个 claude 会话？[y/N] ").strip().lower() == "y":
        open_terminal_session(lab.project, Path(h["path"]).with_suffix(".prompt.txt"))
        print(f"{GREEN}✓ 已在 Terminal 打开新会话{RESET}")

def loop(lab):
    print(f"\n{BOLD}🔬 lab · {lab.project.name}{RESET}")
    if lab.work:
        c = context_usage(lab.work)
        color = RED if c["pct"] >= 85 else YELLOW if c["pct"] >= 70 else DIM
        print(f"{DIM}  工作会话：{session_title(lab.work)}{RESET}  "
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
                print(lab.mainline_file.read_text())
                continue
            if line == "/p":
                lab.choose_session(pick=True)
                continue
            if line == "/s":
                handle(lab, "")
                continue
            if line in ("/h", "/h!"):
                do_handoff(lab, force=line == "/h!")
                continue
            said = line or transcribe_recording(record(), lab.project.name)
            if not said:
                print(f"{YELLOW}没听清，再说一次{RESET}")
                continue
            print(f"{BOLD}你说：{RESET}{said}")
            handle(lab, said)
        except KeyboardInterrupt:
            print(f"\n{YELLOW}已取消{RESET}")
        except Exception as ex:
            print(f"{RED}{ex}{RESET}")


def main():
    ap = argparse.ArgumentParser(description="终端科研管家")
    ap.add_argument("project", nargs="?", default=".", help="研究项目目录（默认当前目录）")
    ap.add_argument("--pick", action="store_true", help="手动选择要盯的工作会话")
    ap.add_argument("-s", "--summary", action="store_true", help="只做一次归纳就退出")
    args = ap.parse_args()
    lab = Lab(args.project, args.pick)
    if args.summary:
        handle(lab, "")
    else:
        loop(lab)
