#!/usr/bin/env bash
# 安装验收环节需要的工具（在 WSL 中执行，需要 sudo 密码）
#   ffmpeg/ffprobe：音视频类题目检查编码、时长、码率、响度
#   playwright + chromium：网页类题目截图
#   mesa-utils：查看 WSLg 下的 OpenGL 版本
set -euo pipefail

sudo apt-get update
sudo apt-get install -y ffmpeg mesa-utils

# nvm 安装的 node 只在交互式 shell 中可用
# shellcheck disable=SC1091
source "$HOME/.nvm/nvm.sh"
npm install -g playwright
npx playwright install chromium
sudo env "PATH=$PATH" npx playwright install-deps chromium

echo "已安装："
ffmpeg -version | head -1
ffprobe -version | head -1
npx playwright --version
glxinfo -B | grep -i "opengl core profile version" || true
