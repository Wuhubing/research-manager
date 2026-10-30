#!/bin/zsh
# 安装依赖：whisper.cpp、ffmpeg、Whisper 模型，并把 lab 命令链接到 ~/.local/bin
set -e
cd "$(dirname "$0")"

command -v brew >/dev/null || { echo "需要 Homebrew：https://brew.sh"; exit 1; }
command -v claude >/dev/null || echo "⚠ 没找到 claude 命令，请先安装 Claude Code：npm install -g @anthropic-ai/claude-code"

brew list whisper-cpp >/dev/null 2>&1 || brew install whisper-cpp
command -v ffmpeg >/dev/null || brew install ffmpeg

MODEL=~/.research-manager/models/ggml-large-v3-turbo-q5_0.bin
if [ ! -f "$MODEL" ]; then
  echo "下载 Whisper 模型（约 550MB）…"
  mkdir -p "$(dirname "$MODEL")"
  curl -L --fail -o "$MODEL" https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin
fi

mkdir -p ~/.local/bin
chmod +x lab.py start.sh
ln -sf "$PWD/lab.py" ~/.local/bin/lab
echo "✓ 安装完成。运行 ./start.sh 打开网页，或在研究项目目录里运行 lab"
[[ ":$PATH:" == *":$HOME/.local/bin:"* ]] || echo "⚠ 请把 ~/.local/bin 加入 PATH"
