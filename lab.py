#!/usr/bin/env python3
"""lab — 终端里的科研管家。

在研究项目目录里运行 `lab`：
  - 回车开始录音，再回车结束 → OpenAI 转文字（gpt-4o-transcribe）
  - 管家（一个持续对话的 Claude CLI 会话）会：
      1. 读取你正在运行的 Claude Code 工作会话里“自上次以来”的新动态
      2. 必要时只读地查看项目文件 / git diff 核实
      3. 对照研究主线给出归纳：做了什么、有没有越界、和主线的关系、发现
      4. 按你说的话写下一条 prompt，自动复制到剪贴板
  - 一切记录在 <项目>/.research/ 下：mainline.md（主线）、log.md（日志）

管家是只读的：只能 Read/Grep/Glob 和 git status/diff/log/show、ls，不能改任何文件。
"""
from research_manager.cli import main

if __name__ == "__main__":
    main()
