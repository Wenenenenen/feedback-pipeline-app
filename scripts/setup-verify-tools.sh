#!/usr/bin/env bash
# 安装验收环节需要的工具（在 WSL 中执行，需要 sudo 密码）
#   ffmpeg/ffprobe：音视频类题目检查编码、时长、码率、响度
#   playwright + chromium：网页类题目截图
#   mesa-utils：查看 WSLg 下的 OpenGL 版本
# 自动做题与自动录屏另外需要：
#   tmux：后台运行做题的 Claude Code 会话、可只读观看
#   xvfb：录屏用的虚拟屏，pulseaudio：录屏用的虚拟声卡，fonts-noto-cjk：录屏画面里的中文
#   xterm：录屏里的终端窗口，xdotool：驱动系统指针与键盘，x11-xserver-utils：设置虚拟屏底色与指针
set -euo pipefail

sudo apt-get update
sudo apt-get install -y ffmpeg mesa-utils tmux xvfb pulseaudio fonts-noto-cjk xterm xdotool x11-xserver-utils

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
tmux -V
command -v Xvfb
pulseaudio --version
echo "中文字体 $(fc-list :lang=zh | wc -l) 个"
