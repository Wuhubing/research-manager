# 架构

本文描述现在的代码（重构阶段 ② 之后：核心逻辑在 `research_manager/` 包里）。行为规则和工作约定见仓库根目录的 [CLAUDE.md](../CLAUDE.md)。

## 组件

```
                    ┌──────────────── 桌面 App（Tauri，main.rs）────────────────┐
                    │  启动 server.py      菜单栏（关注的会话、最近一次录音）      │
                    │  WKWebView 窗口      ⌃P 全局快捷键 → 开始/停止录音           │
                    └────────┬───────────────────────┬──────────────────────────┘
                             │ 打开 localhost:8765    │ HTTP（/api/watched、/api/quick_record/*）
  浏览器 ──────────────────┐ │                       │
                           ▼ ▼                       ▼
                 ┌──────────────────────────────────────────────┐
  终端 `lab` ──┐  │ server.py  本地 HTTP 服务 127.0.0.1:8765        │
  （lab.py →   │  │  只有路由                                      │
   cli.py）    │  └───────────────┬──────────────────────────────┘
               │                  │ import research_manager
               ▼                  ▼
       ┌───────────────────────────────────────────────────────────┐
       │ research_manager/                                          │
       │  sessions 读会话 · 压缩动态 · 上下文用量   manager 管家 Lab   │
       │  prompts 提示词   voice 录音 · 识别 · ⌃P   settings key 设置 │
       │  watch 关注列表 · 接替关系   macos 剪贴板 · 通知 · Terminal  │
       └──┬──────────────┬───────────────┬──────────────┬──────────┘
          │ 只读          │ 子进程         │ HTTPS         │ 子进程
          ▼              ▼               ▼              ▼
  ~/.claude/projects   claude -p       OpenAI 语音识别   ffmpeg（麦克风）
  （工作会话记录）      （管家，只读）   gpt-4o-transcribe  osascript（通知、Terminal）
                                                         pbcopy（剪贴板）
```

- **工作会话**：研究者自己开的 Claude Code 会话，记录在 `~/.claude/projects/<编码后的 cwd>/<会话 id>.jsonl`。本项目只读这些文件。
- **管家**：`claude -p` 子进程，在 `<项目>/.research/` 目录里运行，靠 `--resume` 接着自己的长期会话。它自己的会话记录也在 `~/.claude/projects/` 下（cwd 以 `/.research` 结尾），列会话时会被跳过。
- **项目目录** = 工作会话 cwd 所在的 git 根目录（`manager.git_root()`，不是 git 仓库时就用 cwd 本身）。`.research/` 放在这里。
- **桌面 App** 打包时把 `server.py`、`lab.py`、`research_manager/*.py`、`static/*` 复制到 `Research Manager.app/Contents/Resources/core/`（清单在 `tauri.conf.json` 的 `bundle.resources`），启动时在那里运行 `python3 server.py`（开发时用仓库根目录）。服务已经在跑就不再启动。

## 数据流

### 问管家（网页右栏“问管家” / 终端 `lab` 里说话）

1. 网页：`POST /api/sessions/<id>/ask {said, want_prompt}`。服务端 `Lab(session_cwd(会话), work=会话文件)`，管家固定盯这个会话。
   终端：`Lab(当前目录)`，默认盯项目里（包括子目录里开的）最新的会话，出现更新的会话时自动跟过去。
2. `new_activity()`：从 `state.json` 里这个会话的 offset 开始读新增的记录，用 `condense()` 压缩到约 4 万字（研究者的话和每轮 agent 最后的回答保留全文，其余截头尾，工具的正常输出不放）。第一次看这个会话时只看最后 400KB。
3. 拼成一条消息：研究主线 + 新动态（附原始记录路径，管家需要核实原话时自己 Grep）+ 研究者的话；`want_prompt` 时要求一定输出 prompt。
4. `run_claude()` 调管家，见下文“管家的权限边界”。成功后才保存 offset 和管家会话 id；失败的话下次会重新读这段。
5. `log()` 把这一轮追加到 `history.json` 和 `log.md`。`parse_reply()` 拆出建议的新主线、正文和 prompt。
6. 网页显示回复，可以复制 prompt、采纳主线（`POST /api/sessions/<id>/mainline`）；终端版自动复制 prompt，问要不要采纳主线。

### 交接（会话上下文快满时）

1. 网页“交接”按钮：`POST /api/sessions/<id>/handoff {force}`；终端：`/h`（`/h!` 强制重新生成）。
2. `handoff_checkpoint()`：`handoffs.json` 里记着上次交接时会话文件的大小；大小没变且没有 `force`，直接返回上次的结果。
3. 否则读整个会话（压缩到约 15 万字）+ 管家最近 8 次对这个会话的归纳，用 `HANDOFF_SYSTEM` 做一次**一次性**调用（不 `--resume`、不保存会话，不占管家的上下文）。
4. 写出 `.research/handoff-<时间>.md`（交接文档）和同名的 `.prompt.txt`（新会话的第一条消息，开头是交接文档的路径），更新 `handoffs.json`。
5. “在新会话中继续”：`POST /api/sessions/<id>/newsession {path}`。服务端只接受 `.research/` 里 `handoff-` 开头且有 `.prompt.txt` 的文件，用 osascript 在 Terminal 里 `cd <项目目录> && claude "$(cat <prompt 文件>)"`。
6. `adopt_new_session()` 在后台每 2 秒看一次（最多 4 分钟），新会话文件出现后加进关注列表（放在旧会话后面），并在 `links.json` 里记下接替关系，网页上显示“接续自…”。

### ⌃P 菜单栏录音

1. `main.rs` 注册全局快捷键 ⌃P。空闲时按下：`POST /api/quick_record/start`，弹出菜单栏菜单；再按一次（或点菜单里的录音项）：`POST /api/quick_record/stop`，转写完再弹出菜单显示原话。
2. 服务端 `quick_record()`：`lab.start_recording()` 用 ffmpeg 录内置麦克风到临时 wav（最长 10 分钟）；停止时转写，结果复制到剪贴板，记在 `QUICK` 里。失败时发 macOS 通知并在 `QUICK.error` 里记下原因。
3. 菜单栏每 15 秒读一次 `/api/watched` 和 `/api/quick_record/last`，显示关注的会话、最近一次录音的原话；“再复制一次”调 `/api/quick_record/copy`。
4. 服务退出（SIGTERM / atexit）时停掉正在进行的录音；启动时 `kill_stale_recordings()` 清掉上次残留的 ffmpeg 和临时文件。

### 网页里的录音

浏览器 `MediaRecorder` 录音 → `POST /api/transcribe`（请求体是音频，Content-Type 是音频类型）→ 服务端写临时文件 → `lab.transcribe()` → 文字填进输入框。

### 左栏刷新

页面每 15 秒（以及窗口回到前台、获得焦点时）分别刷新：左栏（`/api/sessions`、`/api/watch`、`/api/watched` 三个请求互不影响，失败的沿用上次的数据）和当前打开的会话（`/api/sessions/<id>`）。会话文件 3 分钟内有更新显示“运行中”。

## HTTP 接口

| 方法 | 路径 | 用途 | 调用方 |
| --- | --- | --- | --- |
| GET | `/api/status` | `{openai, claude}`：key 和 claude 命令是否就绪 | 网页、App 启动页 |
| GET | `/api/sessions` | 最近 60 天的会话（最多 400 个，跳过管家自己的） | 网页 |
| GET | `/api/sessions/<id>` | 会话详情：最近 2MB 的动态、主线、最近 30 条管家记录、上下文用量、接替关系、交接检查点 | 网页 |
| GET/POST | `/api/watch` | 读 / 保存关注列表 `{ids}` | 网页 |
| GET | `/api/watched` | 关注的会话：标题、修改时间、上下文用量 | 网页、菜单栏 |
| POST | `/api/sessions/<id>/ask` | 问管家 `{said, want_prompt}` | 网页 |
| POST | `/api/sessions/<id>/mainline` | 保存主线 `{text}` | 网页 |
| POST | `/api/sessions/<id>/handoff` | 生成交接 `{force}` | 网页 |
| POST | `/api/sessions/<id>/newsession` | 在 Terminal 里用交接 prompt 开新会话 `{path}` | 网页 |
| POST | `/api/transcribe` | 音频 → 文字 | 网页 |
| GET/POST | `/api/settings` | key（只返回末 4 位）、识别模型、上下文窗口 | 网页设置 |
| POST | `/api/quick_record/start` · `stop` · `copy` | ⌃P 录音 | 菜单栏 |
| GET | `/api/quick_record/last` | 录音状态、最近的原话、错误 | 菜单栏 |

除 `/api/transcribe` 外，POST 只接受 `Content-Type: application/json`，防止别的网页不经预检就跨域改设置。服务只监听 127.0.0.1。

## 管家的权限边界

`Lab.run_claude()` 生成的命令（`research_manager/manager.py`）：

```
claude -p --output-format stream-json --verbose --permission-mode default
  --tools Read,Grep,Glob,Bash
  --allowedTools Bash(git status:*) Bash(git diff:*) Bash(git log:*) Bash(git show:*) Bash(ls:*)
  --add-dir <项目目录（git 根目录）>
  --allowedTools Read(//<当前会话记录>) Grep(//<当前会话记录>)
  --disallowedTools Read(//<项目目录>/**/.env*) … Grep(//<项目目录>/**/.ssh/**)   # SECRET_PATTERNS × Read/Grep
  [--model $LAB_CLAUDE_MODEL]
  [--resume <管家会话 id> | --append-system-prompt <SYSTEM 或 HANDOFF_SYSTEM>]
  [--no-session-persistence]   # 交接这类一次性调用
  （cwd = <项目目录>/.research）
```

- **能做的**：读、搜项目目录里的文件（`--add-dir`）；读、搜当前这一个工作会话的记录；`git status/diff/log/show`、`ls`。
- **不能做的**：
  - 写任何文件（没有 Edit/Write，`--permission-mode default` 下没放行的操作在 `-p` 模式里直接被拒绝）；
  - 读项目里的 `.env*`、`*.pem`、`*.key`、`id_rsa*`、`.ssh/**`；
  - 读同一文件夹里的其他会话、子 agent 的记录，或本机其他位置的文件（不放行不限路径的 Read/Grep/Glob）；
  - 其他 Bash 命令。
- 改这些规则时的测试要求见 CLAUDE.md。

## 文件格式

### `<项目>/.research/`（管家生成；要不要被 git 忽略由各项目自己决定，本仓库忽略了）

| 文件 | 格式 | 写入方 |
| --- | --- | --- |
| `mainline.md` | 研究主线，markdown。初始是 `MAINLINE_TEMPLATE`：核心问题 / 当前假设 / 当前焦点 / 明确不做的事 | `Lab.__init__`（模板）、采纳主线 |
| `state.json` | `{"offsets": {会话 id: 已读到的字节位置}, "manager_session": 管家会话 id, "work_session": 会话 id}`；`work_session` 只有终端版写 | `Lab.save()` |
| `history.json` | 数组，每项 `{"time": 秒, "session": 会话 id, "session_title": 标题, "said": 研究者的话, "reply": 管家原始回复}` | `Lab.log()` |
| `log.md` | 给人看的日志：每轮一节 `## 时间（会话：标题）`、`**我说**：…`、管家回复 | `Lab.log()` |
| `handoffs.json` | `{会话 id: {"path": 交接文档路径, "size": 生成时会话文件大小, "time": 秒}}`，只记每个会话最近一次 | `Lab.handoff()` |
| `handoff-YYYYMMDD-HHMMSS.md` | 交接文档（`HANDOFF_SYSTEM` 规定的小节，末尾有 ```prompt 块） | `Lab.handoff()` |
| `handoff-YYYYMMDD-HHMMSS.prompt.txt` | 新会话的第一条消息：`交接文档在 <路径>，请先用 Read 完整读一遍。` + prompt | `Lab.handoff()` |
| `scratch/` | agent 的诊断和测试脚本（约定，不是程序生成的） | 人 / agent |

管家回复的格式（`SYSTEM` 规定，`parse_reply()` 解析）：`## 发生了什么`、`## ⚠ 越界 / 可疑`、`## 和主线的关系`（含【主线推进】/【支线】/【偏离主线】标签）、`## 建议下一步`、`## Prompt`（```prompt 代码块），需要更新主线时末尾附 `<mainline>…</mainline>`。

### `~/.research-manager/`（本机设置和状态）

| 文件 | 格式 |
| --- | --- |
| `openai_key` | OpenAI key 纯文本，权限 600。环境变量 `OPENAI_API_KEY` 优先 |
| `settings.json` | `{"asr_model": "gpt-4o-transcribe" \| "gpt-4o-mini-transcribe", "context_window": "auto" \| "200000" \| "1000000"}`，没设的键不出现 |
| `watch.json` | 关注的会话 id 数组，顺序就是左栏顺序 |
| `links.json` | `{新会话 id: 旧会话 id}`，交接产生的接替关系 |
| `server.log` | App 启动的服务的标准输出和错误输出；请求日志只记 `/api/` 开头的 |

### 读取的会话记录

`~/.claude/projects/<cwd 里非字母数字换成 ->/<会话 id>.jsonl`，每行一个 JSON。用到的字段：`type`（`user` / `assistant` / `custom-title` …）、`message.content`、`message.usage`（算上下文用量：最近一条主对话 assistant 的 input + cache 读 + cache 写 token）、`isSidechain`（子 agent，跳过）、`cwd`（取最后出现的）、`customTitle`（会话标题，没有就用第一条用户消息）。
