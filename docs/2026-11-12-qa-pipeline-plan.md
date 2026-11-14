# 流水线执行器调整计划（面向 Agent）

## 背景

设计文档 `docs/2026-11-12-qa-pipeline-design.md` 的 AI 环节原定在 WSL 中执行 `claude -p`。WSL 的 Claude Code 已接入甲方模型，只能用于人工做题。调整为：

- 出题、验收、写 Feedback、写录屏步骤：Windows 上的 Codex CLI，沿用本机当前的 ChatGPT 官方登录。
- 做题：WSL 的 Claude Code，保持干净环境，流水线不调用它。

## 已核实的环境事实（2026-11-12）

| 项 | 结果 |
|---|---|
| Windows Codex CLI | npm 安装，0.166.0，原生程序 `E:\nodejs\npm-global\node_modules\@openai\codex\node_modules\@openai\codex-win32-x64\vendor\x86_64-pc-windows-msvc\bin\codex.exe`（WSL 中为 `/mnt/e/...`） |
| 登录 | `codex login status` → Logged in using ChatGPT（Windows `~/.codex/auth.json`，与 Codex 客户端共用） |
| WSL 调 Windows codex | 在 WSL 中直接执行上述 `codex.exe --version` 成功 |
| `codex exec` 参数 | 支持 `--json`、`-o`、`--output-schema`、`--ephemeral`、`--ignore-user-config`（不读 config.toml，认证仍用 CODEX_HOME）、`-C`、`--add-dir`、`-s`、`--skip-git-repo-check`、`-m`、`-c`；prompt 可走 stdin |
| WSL 发行版 / 用户 | Ubuntu-24.04，用户 `wendy`，已有 `~/jianzhi/feedback-pipeline/docs/` |
| WSL 的 node / claude | 只在交互式 shell 中可用（nvm），非交互命令需用 `bash -ic` |
| WSL `~/.claude` | 无 CLAUDE.md、rules、skills；settings.json 含 `CLAUDE_CODE_MAX_CONTEXT_TOKENS`、`DISABLE_COMPACT`、`ANTHROPIC_BETAS`、`ANTHROPIC_MODEL` |
| WSL 网络 | 可访问 npm registry；Windows 本机 localhost 代理未镜像进 WSL（NAT 模式），github 推送需在实施时确认 |

## 方案

### 1. 目录

```
WSL  ~/jianzhi/feedback-pipeline/                后端与数据（只有后端访问）
       app/                             FastAPI 后端与静态页面
       data/pipeline.db                 SQLite
       data/trajectories/  data/logs/
       docs/                            设计文档
Win  E:\WorkSpace\jianzhi\feedback-pipeline-agent\   Codex 工作区（不含数据库）
       AGENTS.md                        流水线专用规则
       .agents/skills/qa-gen/SKILL.md
       .agents/skills/qa-review/SKILL.md
       style.md                         口吻规范
       schemas/qa-gen.json  schemas/qa-review.json
       runs/<运行ID>/                    每次运行的输入与最终输出
WSL  ~/jianzhi/feedback-tasks/<编号>/            做题目录
WSL  ~/jianzhi/feedback-verify/<编号>/           验收克隆
```

skill 不放 `~/.codex/skills`、`~/.agents/skills`，避免进入日常 Codex 客户端；也不放 WSL `~/.claude/skills`，避免污染做题。

### 2. 调用方式（替代 pipectl）

后端在 WSL 中组织输入、通过互操作启动 Windows codex、读取结构化输出后落库：

```
<codex.exe> exec --json --ephemeral --ignore-user-config --skip-git-repo-check \
  -m <模型> -c model_reasoning_effort="<强度>" \
  -s danger-full-access \
  -C E:\WorkSpace\jianzhi\feedback-pipeline-agent \
  --output-schema schemas\qa-review.json -o runs\<运行ID>\result.json \
  "$qa-review"  < 输入（题目字段、内部验收要点、轨迹摘要、产物 diff、人工意见）
```

- 输入：后端拼成文本经 stdin 传入（出题时传入领域、难度、数量、已有题目摘要用于查重）。
- 输出：`--output-schema` 约束最终回复为 JSON，后端校验后写入 SQLite。skill 不直接读写数据库，pipectl 取消。
- 日志：`--json` 事件流逐行推送到页面并存 `data/logs/`。
- `--ignore-user-config` 跳过 config.toml 中的 MCP、插件、默认模型；模型与推理强度由配置页提供（默认 `gpt-6.1-sol` / `xhigh`）。
- `--ephemeral` 不落会话文件，流水线会话不会出现在 Codex 客户端历史中。
- 全局 `~/.codex/AGENTS.md` 仍会加载；工作区 `AGENTS.md` 写明「自动化流水线，只按 schema 输出 JSON，不加称呼」，口吻检查兜底。

### 3. 登录保护

- 只使用 Windows 默认 CODEX_HOME，与客户端共用同一 `auth.json`，由 Codex 自身的锁协调刷新。
- 禁止复制 `auth.json`；禁止在 WSL 中运行 Linux 版 codex 并把 CODEX_HOME 指向 `/mnt/c/.../.codex`。
- 并发默认 2，避免触发 ChatGPT 订阅限流。

### 4. 验收执行

- 后端在 WSL 完成产物快照克隆到 `~/jianzhi/feedback-verify/<编号>/`。
- qa-review skill 中所有命令统一写成：`wsl.exe -d Ubuntu-24.04 --cd /home/wendy/jianzhi/feedback-verify/<编号> -- bash -ic '<命令>'`。
- 读代码用 `\\wsl.localhost\Ubuntu-24.04\home\wendy\jianzhi\feedback-verify\<编号>\`，失败则用 `wsl.exe ... cat`。

### 5. 环境体检（做题侧）

检查 WSL `~/.claude`：无全局 CLAUDE.md、rules、用户 skills、MCP、已启用插件；settings.json 的 env 中 `CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000`、`DISABLE_COMPACT=1`、`ANTHROPIC_BETAS` 含 `context-1m-2025-08-07`。

## 执行步骤

> 状态（2026-11-12）：步骤 1–3 已完成，冒烟测试全部通过；设计文档已补充遗漏项（素材准备、题目环境检查、重做流程、模板导出、口吻规则、验收工具与权限边界等），目录与环节以设计文档为准。
>
> 状态（2026-11-13）：已实施。后端源码 `E:\WorkSpace\jianzhi\feedback-pipeline-app`（42 个测试通过），Codex 工作区 `E:\WorkSpace\jianzhi\feedback-pipeline-agent`（AGENTS.md、style.md、banned-topics.md、qa-gen/qa-review skill、schemas），运行时 WSL `~/jianzhi/feedback-pipeline`。验收工具已安装；目录已统一加 feedback- 前缀；提交信息对齐 feedback-common 现有格式（chore: 初始化仓库 / feat: 模型产物 - <概述>）。端到端进行中：observation-harvest 已推送初始快照 8e3fe63149e40d2c15bc4e9365143fc4ddf5f2f6，等待人工做题后继续收集与验收。

> 状态（2026-11-13 下午）：出题领域收缩为「游戏、图形与多媒体」（pipeline.DOMAINS、qa-gen schema 枚举同步）；prompt 增加刻意口语词表检查；页面改版（统一字段网格、进度轨）；46 个测试通过。observation-harvest 已在库内软删除（做题目录与远端分支保留），重新出题运行 20261113-151856-qa-gen-afff5e，产出 recorder-drift-align（音视频处理，困难）；素材脚本已审读，初始化、素材生成、环境检查通过，初始快照 03e15dbf3c245400b093986ded5c1e7e195316e9 已推送，等待人工做题。做题完成后收集通过（内置 skill 清单补入 artifact-design、artifact-diagramming；修复收集时概述变量被覆盖、重试丢概述；配置只落盘改动项），产物快照 49bc02ba97644ec54407de2fef1f63afda2e2577，验收运行 20261113-162716-qa-review-efa6af 因后端随会话退出而中断；新增 scripts/start-detached.ps1（WMI 独立进程启动后端），重新验收运行 20261113-165107-qa-review-8669c3 完成（评分 3，证据 19 条，录屏 9 步），题目进入待查验；验收输入补充素材清单与生成脚本（47 个测试通过）。随后：环境可复现等级改为三档单选（pipeline.REPRO_LEVELS 与 qa-review schema 枚举一致，通过前必选），详情卡片与大段内容可折叠，命令块右上角一键复制，录屏卡片显示录屏目录（48 个测试通过）。再后：删除 Excel 导出（exporter.py、openpyxl、模板复制），改为提交表弹窗（/api/submission，逐格复制、整行复制）；录屏文件自动取 recording_dir 最新 mp4 并用 ffprobe 给出时长分辨率（/api/recordings）；轨迹文件列取 trajectory_path；完成时按 seq_base 接续编号；题目列表可向左折叠（47 个测试通过）。再后：Feedback 写法按质检通过标准重写 style.md 与 qa-review skill（过程 产物 证据 原分四项、代码锚点与代码原因、打回清单），后端新增 check_feedback_content 内容门禁（给N分与评分一致、代码锚点、质检打回表达），qa-review 自检扩为四项（50 个测试通过）。

> 状态（2026-11-13 晚）：初版提交到 github.com/Wenenenenen/feedback-pipeline-app（main，d76ae06）；Codex 工作区并入仓库 agent/ 子目录（原 E:\WorkSpace\jianzhi\feedback-pipeline-agent 已移走），配置默认路径改为由 config.AGENT_DIR 推出，agent/runs/ 不进仓库；在新位置用临时 smoke-echo skill 冒烟通过（skill 发现、schema 输出、wsl.exe、UNC 读取）。下文旧路径以此为准。

> 状态（2026-11-14）：为新环境 clone 即用做可移植化。设计文档与本计划移入仓库 `docs/`（原 `feedback/_verify/` 下的两份已移走，WSL `~/jianzhi/feedback-pipeline/docs/` 副本已删除，不再同步）；冒烟脚本收进 `scripts/smoke-codex.sh` 与 `scripts/smoke/`（读页面配置，临时放入 smoke-echo 后删除，实测通过）；AGENTS.md 与两个 skill 不再写死发行版名，改由输入「本次运行」给出；start-detached.ps1 由脚本位置推出 start.sh 并支持 -Distro；start.sh 检查仓库必须位于 /mnt/<盘符>/；codex_runner 结束残留进程时 powershell.exe 不在 PATH 也能找到；README 补新环境部署（前置条件、克隆、配置、冒烟）。上文「同步 WSL docs 副本」的步骤作废。

1. 冒烟测试（已通过，脚本在 `_verify/smoke/`）：
   - WSL 中调用 Windows codex：`echo 回复ok | codex.exe exec --json --ephemeral --ignore-user-config --skip-git-repo-check -m gpt-6.1-sol -C E:\...\pipeline-agent -`，确认事件流可逐行读取、运行后 `codex login status` 仍为已登录、Codex 客户端仍在线、`~/.codex/sessions` 无新增。
   - 建一个最小 skill + schema，确认 `$skill名` 能触发、`-o` 输出符合 schema。
   - 在 codex 会话里执行一次 `wsl.exe ... bash -ic 'node -v'` 与一次 UNC 读文件。
   - WSL 中 `git ls-remote` 目标 GitHub 仓库，确认推送通道。
2. 根据冒烟结果修改设计文档（第 2、3 节重写；5.1/5.5 改为 stdin 输入 + schema 输出；5.2 补体检项；第 7 节配置页改为 codex 路径、模型、推理强度、WSL 发行版；第 9 节加入上面的冒烟测试）。文档不写方案调整原因。
3. 同步 WSL `~/jianzhi/feedback-pipeline/docs/` 中的设计文档副本。
