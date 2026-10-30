#!/bin/zsh
# 启动科研管理器（后台运行）并打开浏览器；已经在运行就只打开浏览器
cd "$(dirname "$0")"
if ! curl -s -o /dev/null localhost:8765/api/status; then
  nohup python3 server.py > /tmp/research-manager.log 2>&1 &
  sleep 1
  echo "科研管理器已启动：http://localhost:8765（日志在 /tmp/research-manager.log）"
else
  echo "科研管理器已在运行：http://localhost:8765"
fi
open http://localhost:8765
