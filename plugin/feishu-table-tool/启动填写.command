#!/bin/zsh
cd -- "$(dirname -- "$0")" || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
node bin/feishu-fill.mjs fill
result=$?
printf '\n按回车关闭窗口…'
read -r reply
exit "$result"
