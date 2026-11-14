# 出题评测流水线

「CC/Codex 用户反馈标注」作业的出题、做题管理、收集、验收、提交表流水线。设计见 `docs/2026-11-12-qa-pipeline-design.md`，实施计划与进度见 `docs/2026-11-12-qa-pipeline-plan.md`。

## 目录

| 位置 | 内容 |
|---|---|
| 仓库根目录（需放在 Windows 盘上，如 `E:\WorkSpace\jianzhi\feedback-pipeline-app\`） | `app/` 后端与页面、`tests/`、`scripts/`、`docs/` |
| 仓库 `agent/` | Codex 工作区：AGENTS.md、style.md、banned-topics.md、`.agents/skills/`、`schemas/`；`runs/` 为每次运行的输入输出，不进仓库 |
| WSL `~/jianzhi/feedback-pipeline/` | 运行时：`.venv/`、`data/`（数据库、配置、日志、轨迹、备份），首次启动自动创建，不进仓库 |
| WSL `~/jianzhi/feedback-tasks/<分支名>/` | 做题目录 |
| WSL `~/jianzhi/feedback-verify/<分支名>/` | 验收克隆 |
| WSL `~/jianzhi/feedback-record/<分支名>/` | 录屏克隆 |

## 新环境部署

### 1. 准备（仓库之外，需要在新机器上各自配好）

| 项目 | 要求 | 自检方式 |
|---|---|---|
| WSL | 一个 Linux 发行版（默认 `Ubuntu-24.04`），带 `python3`（3.12，含 venv）、`git`、`curl` | `wsl -l -v` |
| GitHub | WSL 中配好能推送产物仓库的 ssh 密钥 | WSL 中 `ssh -T git@github.com` |
| Node.js | WSL 中用 nvm 安装（`bash -ic` 能找到 `node`） | WSL 中 `bash -ic 'node -v'` |
| 做题客户端 | WSL 中安装 Claude Code（`bash -ic` 能找到 `claude`），并按作业要求配置：`~/.claude/settings.json` 的 env 含 `CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000`、`DISABLE_COMPACT=1`、`ANTHROPIC_BETAS` 含 `context-1m-2025-08-07`；不放全局 CLAUDE.md、rules、skills、agents、commands、插件、hooks、MCP | 页面「环境检查」会逐项体检 |
| Codex | Windows 中安装 Codex CLI（如 `npm i -g @openai/codex`），并用 ChatGPT 账号登录（`codex login`） | Windows 中 `codex login status` |
| 验收工具 | WSL 中的 ffmpeg、playwright + chromium | 运行下面的 `setup-verify-tools.sh` |
| 录屏软件 | 输出 mp4 到固定目录（默认 `E:\GSB-Recordings`） | — |

### 2. 克隆与启动

```powershell
# 克隆到 Windows 盘上（Windows 版 codex 要直接读写仓库里的 agent/，放在 WSL 原生目录里不行）
git clone git@github.com:Wenenenenen/feedback-pipeline-app.git E:\WorkSpace\jianzhi\feedback-pipeline-app

# 后台常驻启动（首次会在 WSL 中创建虚拟环境并安装依赖）；发行版不是 Ubuntu-24.04 时加 -Distro <名称>
pwsh -File E:\WorkSpace\jianzhi\feedback-pipeline-app\scripts\start-detached.ps1
```

在 WSL 中安装验收工具（需要 sudo 密码，只需一次）：

```bash
bash /mnt/e/WorkSpace/jianzhi/feedback-pipeline-app/scripts/setup-verify-tools.sh
```

### 3. 配置

浏览器打开 `http://localhost:8765`，在「配置」页按本机情况修改并保存（保存在 WSL `~/jianzhi/feedback-pipeline/data/config.json`）：

- **codex 可执行文件路径**：Windows 版 `codex.exe` 的 WSL 路径。npm 全局安装时一般在 `<npm 全局目录>/node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe`，Windows 中 `npm root -g` 可查 npm 全局目录；
- **GitHub 仓库**：存放做题快照的产物仓库 ssh 地址；
- **WSL 发行版名**：与 `wsl -l` 中的名称一致；
- **录屏文件目录**：录屏软件输出目录的 WSL 路径（如 `/mnt/e/GSB-Recordings`）；
- **提交表已有最大序号**：提交表里已有的最后一个序号，新完成的题目从下一个号开始；
- 模型名、推理强度、并发数、超时、端口段、各目录根一般保持默认。

Codex 工作区路径默认取仓库里的 `agent/`，不用改。

### 4. 冒烟

配置保存后在 WSL 中运行一次，确认 WSL 能调起 Windows 版 codex、ChatGPT 登录有效、`agent/` 下的 skill 能被发现、输出符合 schema、codex 能回调 `wsl.exe` 并读取 WSL 文件：

```bash
bash /mnt/e/WorkSpace/jianzhi/feedback-pipeline-app/scripts/smoke-codex.sh
```

看到「冒烟通过」即可开始使用。升级 codex 或改动配置后建议重跑。

## 启动方式

```powershell
# Windows 中，后台常驻（推荐，不随终端或会话退出而结束，日志写入 ~/jianzhi/feedback-pipeline/data/logs/server.log）
pwsh -File E:\WorkSpace\jianzhi\feedback-pipeline-app\scripts\start-detached.ps1
```

```bash
# WSL 中，前台运行（关闭终端即停止）
bash /mnt/e/WorkSpace/jianzhi/feedback-pipeline-app/scripts/start.sh
```

后端退出会中断正在运行的 skill，重启后该运行记为失败，需重新发起。端口可用环境变量 `PIPELINE_PORT` 修改，运行时目录可用 `PIPELINE_HOME` 修改。

## 使用流程

1. **批量出题**：在「批量出题」页填写数量、子方向等，后台调用 `codex exec '$qa-gen'`，题目入库为「待初始化」。
2. **初始化**：创建 `~/jianzhi/feedback-tasks/<分支名>/` 孤儿分支仓库（此时不推送）。
3. **准备环境**：自动下载素材、执行生成脚本或人工放入后确认；执行环境检查（工具链、显卡、外网、做题客户端体检）；全部通过后提交并推送初始快照。
4. **做题**：复制做题命令，在 WSL 中启动 Claude Code，发送 prompt 原文；结束后点「做题完成」。模型无故中断或网络报错时点「重做」。
5. **收集**：定位轨迹、校验单轮与污染、提交并推送产物快照、生成轨迹摘要。
6. **验收**：全新克隆产物，调用 `codex exec '$qa-review'` 实测并写 Feedback。
7. **查验**：编辑评分与 Feedback（口吻与内容检查通过才能点「通过」），或填写意见打回重新生成。
8. **录屏**：「准备录屏环境」得到干净克隆，按录屏步骤录制；页面自动取录屏目录中最新的 mp4，核对时长与分辨率后标记完成，此时按配置的已有最大序号接续编号。
9. **提交表**：列表上方点「提交表」弹窗查看勾选题目（未勾选时为全部已完成题目）的提交表各列，单元格点击即复制，也可整行复制后粘贴到表格。

## 测试

```bash
cd /mnt/e/WorkSpace/jianzhi/feedback-pipeline-app
~/jianzhi/feedback-pipeline/.venv/bin/python -m pytest -q -p no:cacheprovider
```

## 注意

- codex 使用 Windows 默认 `~/.codex` 的 ChatGPT 登录，与 Codex 客户端共用；不要复制 `auth.json`，不要在 WSL 中运行 Linux 版 codex 指向 Windows 的 `.codex`。
- Windows `~/.codex/skills/` 下的个人 skill 会被 codex 一并加载，流水线不依赖它们；不要把 `agent/` 里的 skill 装到全局目录。
- 不要在仓库根目录放 AGENTS.md，codex 会从仓库根一路加载到 `agent/`，根目录的规则会混入流水线运行。
- 数据库每天备份到 `data/backups/`，保留 14 份。
