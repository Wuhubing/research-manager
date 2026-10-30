# 🔬 research-manager

科研时给 Claude Code 配的一个“管家”。

用 agent 做科研，常见的两个问题是：

1. **agent 过度发挥。** 它会做没要求的事、改范围外的文件、悄悄换方法，你最后不知道到底发生了什么。
2. **想法很难写成精确的 prompt**，尤其是脑子里想得清楚、打字表达不出来的时候。

research-manager 的做法是：你用语音说话，一个只读的 Claude CLI 管家去读你正在跑的 Claude Code 会话，然后对照你的研究主线告诉你：

- **发生了什么**：agent 实际做了什么，具体到文件、命令和数字
- **⚠ 越界 / 可疑操作**：做了没要求的事、改了评测、偷偷调了超参等
- **跟主线的关系**：属于【主线推进】、【支线】还是【偏离主线】，以及有哪些新发现
- **下一步建议**
- **Prompt**：你想派任务时，它会写一条带范围边界的 prompt，并自动复制到剪贴板

语音识别用本地的 [whisper.cpp](https://github.com/ggml-org/whisper.cpp)，不上传音频。LLM 调用走本机的 `claude -p`，用你的 Claude 订阅，不需要 API key。只依赖 Python 标准库。

## 安装

需要 macOS、Homebrew、Python 3.9+，以及 [Claude Code](https://claude.com/claude-code)。

```bash
git clone https://github.com/Wuhubing/research-manager.git
cd research-manager
./install.sh
```

`install.sh` 会做这些事：
- 安装 `whisper-cpp` 和 `ffmpeg`
- 下载 `large-v3-turbo` 量化模型（约 550MB），放到 `~/.research-manager/models/`
- 把 `lab` 命令链接到 `~/.local/bin`

## 使用

### 网页版

```bash
./start.sh
```

用 Chrome 或 Safari 打开 http://localhost:8765。Claude 桌面版的内置浏览器不支持麦克风，所以不能在那里用语音。

- **左栏**：最近 14 天的所有 Claude Code 会话，按项目分组。最近 3 分钟有活动的会话会亮绿点。
- **中栏**：选中会话里 agent 的动作记录。改文件的操作标橙色，报错标红色。
- **右栏**：管家。点 🎙 说话，或直接打字。点“只归纳”可以快速看一次进展。

### 终端版

在研究项目目录里运行：

```bash
lab
```

- **回车**：开始录音，再按回车结束
- **直接打字再回车**：用文字输入
- `/s`：只归纳
- `/m`：查看主线
- `/p`：切换要盯的工作会话
- `/q`：退出

终端录音需要在「系统设置 → 隐私与安全性 → 麦克风」里允许你用的终端 App。

## 工作方式

- 管家按项目记忆。每个研究项目会有一个持续对话的 Claude 会话，同一项目下的多个工作会话共用它。
- 每次提问时，管家读取工作会话自上次以来的新动态。Claude Code 的会话记录在 `~/.claude/projects/` 下。
- **管家只读。** 它只能用 `Read`、`Grep`、`Glob`，以及 `git status/diff/log/show` 和 `ls`，不能修改任何文件。
- 记录保存在研究项目的 `.research/` 目录里：
  - `mainline.md`：研究主线
  - `history.json`、`log.md`：管家的对话历史和日志

  建议把 `.research/` 加进研究项目的 `.gitignore`。

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `RM_PORT` | `8765` | 网页端口 |
| `RM_CLAUDE_MODEL` / `LAB_CLAUDE_MODEL` | Claude 默认模型 | 例如 `sonnet`，速度更快 |
| `RM_WHISPER_MODEL` / `LAB_WHISPER_MODEL` | `~/.research-manager/models/ggml-large-v3-turbo-q5_0.bin` | Whisper 模型路径 |
| `LAB_MIC` | `:0` | 终端录音设备（ffmpeg avfoundation）。用 `ffmpeg -f avfoundation -list_devices true -i ""` 查看可用设备 |

## 文件

- `server.py`：本地网页服务，负责会话列表、管家接口和语音转写
- `lab.py`：终端版管家，网页服务也复用它的会话解析和管家逻辑
- `static/index.html`：会话视图
- `static/projects.html`：旧版按项目手动管理的页面
