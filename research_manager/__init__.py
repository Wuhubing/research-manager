"""科研管家的核心逻辑。server.py（网页和 App 的本地服务）和 lab.py（终端命令）都用这个包。

- sessions  读 Claude Code 的会话记录：列表、标题、压缩动态、cwd、上下文用量（只读）
- manager   管家：调用只读的 claude -p，权限、历史、主线、交接
- prompts   给管家的提示词和主线模板
- voice     录音、语音识别、菜单栏 ⌃P 录音
- settings  OpenAI key 和本机设置
- watch     关注的会话列表、交接后的接替关系
- macos     剪贴板、通知、在 Terminal 里开新会话
- cli       终端版 lab 命令
"""
