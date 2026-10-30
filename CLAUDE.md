# CLAUDE.md

给在这个仓库里干活的 agent 看的规则。架构、数据流和文件格式见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 这是什么

科研管家：盯着研究者正在跑的 Claude Code 会话，对照研究主线归纳“发生了什么、有没有越界”，把口述整理成 prompt，会话快满时生成交接文档。
有网页（本地服务 + `static/`）、终端命令 `lab`、macOS 桌面 App（Tauri，带菜单栏和 ⌃P 录音）三种用法。

- 仓库名和代码里叫 research-manager / Research Manager；**对外的名字是 Alfred**。以后改名时，App 名、菜单栏、README、网页标题都要跟着改。
- GitHub：`Wuhubing/research-manager`（公开），主分支 `main`。

## 各文件职责

| 文件 | 职责 |
| --- | --- |
| `server.py` | 本地 HTTP 服务（127.0.0.1:8765），只放路由，逻辑都在 `research_manager/` |
| `research_manager/sessions.py` | 读 Claude Code 会话记录（只读）：列表、标题、`condense()` 压缩动态、cwd、上下文用量 |
| `research_manager/manager.py` | 管家：`Lab` 类（`run_claude()` 和权限、新动态、历史和日志、主线、交接）、`git_root`、`parse_reply`、会话详情 |
| `research_manager/prompts.py` | 给管家的提示词（`SYSTEM`、`HANDOFF_SYSTEM`）和主线模板 |
| `research_manager/voice.py` | ffmpeg 录音、OpenAI 语音识别、网页上传的录音、菜单栏 ⌃P 录音状态 |
| `research_manager/settings.py` | OpenAI key、`settings.json`、设置页的内容 |
| `research_manager/watch.py` | 关注列表 `watch.json`、交接后的接替关系 `links.json` |
| `research_manager/macos.py` | 剪贴板、系统通知、在 Terminal 里开新会话 |
| `research_manager/cli.py` | 终端版 `lab` 命令（交互循环、`-s` 只归纳） |
| `lab.py` | `lab` 命令的入口（`~/.local/bin/lab` 软链到这里），只调用 `research_manager.cli` |
| `static/index.html` | 网页界面（左栏关注的会话 / 中栏会话动态 / 右栏管家 / 设置 / 交接 / 录音），CSS 和 JS 都在这一个文件里 |
| `tests/smoke.py` | 冒烟测试：假 HOME + 8799 端口，查主要接口和管家的权限参数 |
| `desktop/src-tauri/src/main.rs` | 桌面 App：启动 `server.py`、菜单栏、⌃P 全局快捷键录音 |
| `desktop/src-tauri/tauri.conf.json` | App 配置；`bundle.resources` 列出打进 App 的 Python 和静态文件 |
| `desktop/dist/index.html` | App 的启动页：等本地服务起来后跳到 `http://localhost:8765/` |
| `install.sh` / `start.sh` | 装依赖并把 `lab` 软链到 `~/.local/bin`；后台启动服务并打开浏览器 |

计划中的拆分（拆 `index.html`、拆 `main.rs`）做完后要同步更新这张表。

## 必须遵守

- **不加依赖**：Python 只用标准库；前端不用构建工具和框架；Rust 不加新 crate（除非研究者同意）。
- **只读 Claude Code 的会话记录**（`~/.claude/projects/**/*.jsonl`）：不删、不改、不移动。测试需要会话时自己用 `claude -p` 新建一个。
- **不提交 `design/`**，也不提交 `desktop/src-tauri/icons/tray.png` 等图标改动，除非研究者明确要求。提交前用 `git status` 看清楚，只 `git add` 本次要提交的文件。
- **只在研究者要求时提交和推送**。禁止 force push、rebase、修改已有提交（`--amend`）。push 失败或有冲突先停下来问。
- **不打印 OpenAI key**（`~/.research-manager/openai_key`，chmod 600），日志和汇报里也不要出现。
- 发布的截图里不要有研究者的个人信息；不要改 `~/Downloads` 里的原文件。
- 范围以外的问题（顺手发现的 bug 等）记下来汇报，不要顺手改。

## 涉及管家权限的改动

管家是只读的，权限在 `research_manager/manager.py` 的 `ALLOWED_TOOLS`、`SECRET_PATTERNS` 和 `Lab.run_claude()` 里（边界说明见 ARCHITECTURE.md）。改到这些地方时：

- 必须测试**会话在项目子目录里**的情况（管家的项目目录是 git 根目录，不是会话的 cwd）。
- 用 `run_claude()` 真正生成的命令跑一次真的 `claude -p`，逐项确认：项目文件能读；`.env*`、`*.pem`、`*.key`、`id_rsa*`、`.ssh/` 读不到；同目录下其他会话记录读不到；当前会话记录能 Read/Grep；git status/diff/log/show 能用。全部用临时假文件，不要用真密钥。
- 不要为了让测试通过而自己放宽 `ALLOWED_TOOLS`，先停下来说明。

## 测试和验证

- **改完先跑 `python3 tests/smoke.py`**（约 1 秒，不碰真实的 ~ 和 8765），全部通过才算没改坏。改了接口或权限，要同步更新它的检查。
- 诊断、一次性的测试脚本放 `.research/scratch/`（已被 .gitignore 忽略）。
- 需要跑接口时，另起一个服务实例，不要动研究者正在用的 8765：在脚本里 import `server` 后用 `ThreadingHTTPServer(("127.0.0.1", 8799), server.Handler)`（参考 `tests/smoke.py`）。不要用 `python3 server.py` 另起：它启动时会停掉本机所有残留的录音进程，包括 App 正在进行的 ⌃P 录音。`research_manager` 的模块在 import 时就读 `Path.home()`，要隔离数据就在 import 前设好 `HOME`。
- 不需要时不要真的调 `claude` 或 OpenAI（花钱、慢）；可以替换 `voice.start_recording` / `voice.transcribe` 之类的函数，或截获 `subprocess.Popen` 模拟。
- 网页的定时器：WebKit 会把隐藏或后台窗口的定时器降到大约每分钟一次。在内置浏览器里看时间相关的行为，要先让浏览器面板显示出来。
- App 里的网页没法直接检查 DOM；看行为可以用内置浏览器打开 `http://localhost:8765/`（同一份 `index.html`），或看 `~/.research-manager/server.log` 里的请求时间。

## 改完要重新打包 App

App 里的 `server.py`、`lab.py`、`research_manager/*`、`static/*` 是打包时复制进去的副本，**改了这些文件，App 要重新打包才会用到新代码**。新增文件要加进 `tauri.conf.json` 的 `bundle.resources`，不然 App 里没有。

```bash
# 1. 有管家请求在跑就等它结束
while pgrep -f "claude -p" >/dev/null; do sleep 2; done
# 2. 退出 App（退不掉就结束进程），停掉 8765 上的服务
osascript -e 'quit app "Research Manager"'; sleep 2; pkill -f "Research Manager.app" || true
lsof -ti tcp:8765 | xargs kill 2>/dev/null || true
# 3. 打包并打开
cd desktop && npx tauri build
open "src-tauri/target/release/bundle/macos/Research Manager.app"
# 4. 等服务起来
until curl -s localhost:8765/api/status; do sleep 1; done
```

打包后确认新文件在 `desktop/src-tauri/target/release/bundle/macos/Research Manager.app/Contents/Resources/core/` 里。

打包没结束前不要打开 App：打包最后才替换 `core/`，提前打开的 App 会用旧文件起服务，而且服务一直在跑，之后再打开 App 也不会换。确认的办法：8765 上的服务进程启动时间（`ps -o lstart= -p $(lsof -ti tcp:8765 -sTCP:LISTEN)`）要晚于 `core/` 的修改时间；不对就按上面的步骤再退出、重开一次。

## 汇报格式

每次完成任务都按这个格式汇报：

- **做了什么**
- **改动的文件**
- **关键结果**（验证结果、提交 hash 等）
- **与要求不一致或自作主张之处**
- **遇到的问题**
