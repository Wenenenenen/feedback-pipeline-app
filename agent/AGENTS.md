# 流水线运行规则

本目录是「CC/Codex 用户反馈标注」出题与评测流水线的自动化工作区，由后端通过 `codex exec` 无人值守调用。本文件的要求优先于全局 AGENTS.md 中与输出格式、称呼相关的要求。

## 输出

- 最终回复只输出符合本次 `--output-schema` 的 JSON，不添加称呼、问候、解释或 markdown 代码块。
- JSON 中面向用户的文本字段（User Prompt、Feedback、评分理由、备注）只遵循本目录 `style.md` 与对应 skill 的要求，不使用「大哥」等称呼。
- 过程中的说明文字保持简短，不需要向任何人汇报进度。

## 输入

- 本次任务数据在用户输入的 `<stdin>` 块中，以它为准；不要向人提问，信息不足时按 skill 的默认做法处理并在结果中注明。
- 输入开头给出本次运行 ID 与运行目录 `runs/<运行ID>/`；草稿、验证脚本、试跑生成的文件等临时内容只能放在这个目录，不要自己另起目录名。

## 权限边界

- 只允许写入：
  - 本目录下输入中给出的 `runs/<运行ID>/`；
  - 输入中给出的验收目录。
- 禁止：
  - 修改做题目录（流水线配置的做题目录根，默认 `~/jianzhi/feedback-tasks/`）下的任何内容；
  - 修改 WSL 的 `~/.claude`、Windows 的 `~/.codex`、`~/.agents`；
  - 修改本目录下的 `AGENTS.md`、`style.md`、`banned-topics.md`、`.agents/`、`schemas/`；
  - 执行 `git push`、`git commit --amend`、`git rebase`、`git reset` 等改写或推送 git 历史的操作；
  - 安装全局软件或使用 sudo（依赖只装在验收目录内，如 `npm install`、`python -m venv .venv`）。

## 在 WSL 中执行命令

- 统一写法：`wsl.exe -d <发行版> --cd <WSL 路径> -- bash -ic '<命令>'`，`<发行版>` 取输入「本次运行」中给出的 WSL 发行版名，`-i` 用于加载 nvm 等交互式环境。
- `wsl.exe` 自身可能输出一段 UTF-16 乱码提示（关于 localhost 代理），忽略即可；`bash -ic` 的 `no job control` 提示也忽略。
- 命令较长或含复杂引号时，先把脚本写到验收目录（通过 `\\wsl.localhost\<发行版>\...` 路径），再执行 `bash -i <脚本>`。
- 每条命令都要有超时意识：长时间运行的服务用 `nohup ... &` 后台启动并记录 PID，验证完成后必须 `kill`；只使用输入中分配的端口。
