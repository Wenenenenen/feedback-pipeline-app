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
| Codex | Windows 中安装 Codex CLI（如 `npm i -g @openai/codex`），用 ChatGPT 账号登录（`codex login`），或用 CC Switch 等工具配置第三方供应商（见下面配置里的「沿用 config.toml 的供应商与模型」） | Windows 中 `codex login status`；第三方供应商以冒烟为准 |
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
- **沿用 config.toml 的供应商与模型**：codex 用 CC Switch 或手工配置的第三方 API 时勾选。流水线调用 codex 时带 `--ignore-user-config`，不加载 `config.toml`；勾选后每次运行前只读取 Windows `config.toml` 中的 `model`、`model_provider` 和对应的 `[model_providers.<名称>]` 一节，转成 `-m`、`-c` 参数传给 codex，MCP、插件等仍不加载。CC Switch 切换供应商后流水线自动跟随，不用改配置；API key 由 CC Switch 写在 `auth.json`，流水线不读也不改。`config.toml` 没有 `model` 时用配置页的模型名；推理强度始终用配置页的值；
- **config.toml 路径**：一般留空，自动按 Windows 的 `CODEX_HOME` 或 `%USERPROFILE%\.codex\config.toml` 定位；定位不到时填它的 WSL 路径（如 `/mnt/c/Users/<用户名>/.codex/config.toml`）；
- 模型名、推理强度、并发数、超时、连续重连上限、端口段、各目录根一般保持默认。

Codex 工作区路径默认取仓库里的 `agent/`，不用改。

### 4. 冒烟

配置保存后在 WSL 中运行一次，确认 WSL 能调起 Windows 版 codex、codex 认证有效（ChatGPT 登录或 `config.toml` 中的供应商，脚本会打印实际使用的模型与供应商参数）、`agent/` 下的 skill 能被发现、输出符合 schema、codex 能回调 `wsl.exe` 并读取 WSL 文件：

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

也可以在 Windows 中双击仓库根目录的脚本（需要 PowerShell 7，即 `pwsh`）：

- `start.cmd`：在后台启动后端，窗口里实时显示本次启动的日志，接口可访问后显示「后端启动成功」，按任意键关闭窗口，后端继续运行；后端已在运行时只提示地址，不重复启动；
- `stop.cmd`：停止后端；有出题、验收、返修正在运行时先列出来，按 Y 强制停止，按 N 取消。

发行版不是 Ubuntu-24.04 时在命令行执行 `start.cmd <发行版名>`、`stop.cmd <发行版名>`。WSL 中也可以直接执行 `bash scripts/stop.sh`（加 `--force` 不检查运行中的任务）。

后端退出会中断正在运行的 skill，重启后该运行记为失败，需重新发起。端口可用环境变量 `PIPELINE_PORT` 修改，运行时目录可用 `PIPELINE_HOME` 修改。

## 使用流程

1. **批量出题**：在「批量出题」页填写数量、子方向等，后台调用 `codex exec '$qa-gen'`，题目入库为「待初始化」。
2. **初始化并准备环境**：核对题目后点一次「初始化并准备环境」，后台依次创建 `~/jianzhi/feedback-tasks/<分支名>/` 孤儿分支仓库、检查工具链、自动下载素材和执行生成脚本、检查外网与做题客户端，全部通过就提交并推送初始快照，直接进入「待做题」。
3. **需要人工处理时**：人工素材没放、脚本或下载失败、工具缺失、外网不通、客户端不干净、依赖显卡要确认时，题目停在「待准备环境」，顶部逐项列出问题和处理方式；处理完点「继续准备」（显卡题点「确认显卡能力并继续」），已就绪的素材不会重跑。
4. **做题**：复制做题命令，在 WSL 中启动 Claude Code，发送 prompt 原文；结束后点「做题完成」。模型无故中断或网络报错时点「重做」。
5. **收集**：定位轨迹、校验单轮与污染、提交并推送产物快照、生成轨迹摘要。
6. **验收**：全新克隆产物，调用 `codex exec '$qa-review'` 实测并写 Feedback。
7. **查验**：编辑评分与 Feedback（口吻与内容检查通过才能点「通过」），或填写意见打回重新生成。
8. **录屏**：「准备录屏环境」得到干净克隆，按录屏步骤录制；页面自动取录屏目录中最新的 mp4，核对时长与分辨率后标记完成，此时按配置的已有最大序号接续编号。
9. **提交表**：列表上方点「提交表」弹窗查看勾选题目（未勾选时为全部已完成题目）的提交表各列，单元格点击即复制，也可整行复制后粘贴到表格。语言/框架列统一为不带版本号、英文逗号分隔的写法（如 `java,python,nodejs`）。
10. **质检返修**：质检不通过的数据直接放弃。待返修的，在已完成题目的「质检与返修」卡片粘贴质检原文（开头为【AI质检｜待返修】），点「AI 返修」，后台调用 `codex exec '$qa-rework'` 按质检原文直接处理：改写 Feedback 等内容，需要时修改产物并推送新的产物快照。处理完，这一轮的卡片里列出提交表要改的列名；新旧内容到提交表弹窗里看，这道题标出「第N轮返修」，改过的格高亮（悬停看原值），也可点「复制改动格」。产物有改动或需要重录时题目回到待录屏，录完标记完成；否则直接回到已完成。序号保持不变。

## 测试

```bash
cd /mnt/e/WorkSpace/jianzhi/feedback-pipeline-app
~/jianzhi/feedback-pipeline/.venv/bin/python -m pytest -q -p no:cacheprovider
```

## 注意

- codex 使用 Windows 默认 `~/.codex` 的认证（ChatGPT 登录，或 CC Switch 写入的第三方 key），与 Codex 客户端共用；不要复制 `auth.json`，不要在 WSL 中运行 Linux 版 codex 指向 Windows 的 `.codex`。
- 供应商地址不可达时，codex 会一直重连而不退出；流水线在连续重连达到配置页的「连续重连上限」（默认 10 次，每次约 8 秒）时结束运行并报「连续重连 N 次仍连不上供应商」，此时检查网络或 CC Switch 当前供应商。
- Windows `~/.codex/skills/` 下的个人 skill 会被 codex 一并加载，流水线不依赖它们；不要把 `agent/` 里的 skill 装到全局目录。
- 不要在仓库根目录放 AGENTS.md，codex 会从仓库根一路加载到 `agent/`，根目录的规则会混入流水线运行。
- 数据库每天备份到 `data/backups/`，保留 14 份。
