#!/bin/zsh
# 安装依赖（ffmpeg，用于终端录音），并把 lab 命令链接到 ~/.local/bin
set -e
cd "$(dirname "$0")"

command -v brew >/dev/null || { echo "需要 Homebrew：https://brew.sh"; exit 1; }
command -v claude >/dev/null || echo "⚠ 没找到 claude 命令，请先安装 Claude Code：npm install -g @anthropic-ai/claude-code"
command -v ffmpeg >/dev/null || brew install ffmpeg

mkdir -p ~/.local/bin ~/.research-manager
chmod +x lab.py start.sh
ln -sf "$PWD/lab.py" ~/.local/bin/lab

if [ ! -s ~/.research-manager/openai_key ] && [ -z "$OPENAI_API_KEY" ]; then
  echo "⚠ 语音识别需要 OpenAI API key，把它写进 ~/.research-manager/openai_key（见 README）"
fi
echo "✓ 安装完成。运行 ./start.sh 打开网页，或在研究项目目录里运行 lab"
[[ ":$PATH:" == *":$HOME/.local/bin:"* ]] || echo "⚠ 请把 ~/.local/bin 加入 PATH"
