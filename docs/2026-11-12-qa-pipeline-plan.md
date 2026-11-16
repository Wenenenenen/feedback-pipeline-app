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
| 后端里调 `bash -ic` | 后端由 `bash -ic` 启动、持有控制终端，子进程 `bash -ic` 必须 `start_new_session=True`：否则它抢占终端前台进程组且单条命令时直接 exec 不归还，下一个 `bash -ic` 向整个进程组发 SIGTTIN，后端被停住（页面 Failed to fetch）；工具链检查在做题目录执行 |
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
- 配置 `codex_follow_toml` 打开时（CC Switch 或第三方 API 的环境），`codex_runner.model_args` 读取 `codex_config_toml`（留空时用 powershell.exe 取 Windows 的 `CODEX_HOME` 或 `%USERPROFILE%\.codex`，经 `config.wsl_path` 转换，进程内缓存）：`-m` 取 `model`，`model_provider` 与 `[model_providers.<名称>]` 逐项转成 `-c`；`build_cmd` 与 `scripts/smoke-codex.sh` 共用该函数。
- `--ephemeral` 不落会话文件，流水线会话不会出现在 Codex 客户端历史中。
- 全局 `~/.codex/AGENTS.md` 仍会加载；工作区 `AGENTS.md` 写明「自动化流水线，只按 schema 输出 JSON，不加称呼」，口吻检查兜底。

### 3. 登录保护

- 只使用 Windows 默认 CODEX_HOME，与客户端共用同一 `auth.json`，由 Codex 自身的锁协调刷新。
- 禁止复制 `auth.json`；禁止在 WSL 中运行 Linux 版 codex 并把 CODEX_HOME 指向 `/mnt/c/.../.codex`。
- 并发默认 2，避免触发 ChatGPT 订阅限流。
- 沿用 config.toml 时只读该文件，key 仍由 codex 从 `auth.json` 取（CC Switch 写入），流水线不读 `auth.json`。

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
>
> 状态（2026-11-14 下午）：新增质检登记与返修（设计文档 5.10）。状态加「待返修」「作废」；新表 `qa_inspection`（`001_init.sql` 同步建表，存量环境由 `002_inspection.sql` 补建，已在运行库执行）；`pipeline` 新增 parse_inspection、register_inspection、cancel_inspection、start_rework、start_recommit、finish_rework，complete 时结束返修中的质检；新 skill `qa-rework` 与 `schemas/qa-rework.json`，处理结论枚举与 `pipeline.REWORK_HANDLINGS` 一致（测试核对）；`gitops` 抽出 `_forced_ignored`（并去重）、新增 pending_changes，commit_artifact 支持自定义提交标题；提交表默认含作废题并附质检原文；style.md、qa-review 与 style_check 增加工具链噪声规则。57 个测试通过；临时实例截图核对了待返修、作废详情与提交表。`qa-rework` 尚未用真实质检跑过 codex。
>
> 状态（2026-11-14 晚）：返修简化为只由 AI 按质检原文直接处理（设计文档 5.10 已改写），上一条中的作废状态、撤销登记、人工补交产物、完成返修、处理结论枚举（REWORK_HANDLINGS）全部去掉。质检原文开头为【AI质检｜待返修】（兼容旧写法），不通过的直接拒绝不登记；`start_rework_from_text` 登记后立即跑 `qa-rework`，同时克隆验收目录与修复目录（`<verify_root>/<分支>-rework`），修复目录有改动时用 commit_artifact（标题 `fix: 返修`）推送新快照；Feedback 规则检查不过则失败，可重试（RETRY 加 S_REWORK）；产物改动或 need_rerecord 时回到待录屏，complete 时结束本轮质检；`rework_changes` 按提交表列对比快照与当前内容，页面列出要改的格。`qa-rework` schema 改为 summary、need_rerecord 加提交字段。新增 `003_rework_summary.sql`（qa_inspection 加 summary，已在运行库执行，#11 的旧返修记录保留）；migrate 对新库只执行初始化脚本、增量脚本只登记。语言/框架：`submission.normalize_lang` 去版本号与中文说明、逗号分隔，用于 qa-gen 入库、编辑保存、提交表展示，qa-gen skill 补格式要求。59 个测试通过；临时实例截图核对了返修后待录屏与已完成两种卡片。新版 `qa-rework` 尚未用真实质检跑过 codex。
>
> 状态（2026-11-14 晚，续）：提交表弹窗标出返修：`/api/submission` 每行附 `rework`（最近一轮的轮次、状态、改过的格到原值），页面高亮改过的格、悬停看原值、可「复制改动格」。59 个测试通过，临时实例截图核对过。
>
> 状态（2026-11-14 晚，再续）：题目详情的质检卡片改为每轮一张可折叠卡片（`details.insp`，默认只展开最近一轮，状态记在 localStorage），「提交表要改的格」移入各轮卡片且只列列名；接口去掉 `rework_changes`，改由 `pipeline.inspections_view` 给每轮附 `changed_columns`（前一轮对比下一轮登记时的快照，最后一轮对比当前内容）。60 个测试通过。
>
> 状态（2026-11-15）：① 准备环境自动化：`start_prepare` 一次跑完初始化、工具链、素材（下载与生成脚本自动执行，已就绪只核对）、显卡、外网、客户端体检，无待处理项时直接提交初始快照进待做题；有待处理项停在待准备环境，`env_blockers` 每项带处理方式；`confirm_gpu` 确认后自动继续；去掉 start_init、start_fetch_materials、start_run_script、confirm_material、start_envcheck、start_finalize_env 及对应接口和按钮；RETRY 中待初始化、待准备环境都走 start_prepare。② qa-review 精简：Feedback 150 到 300 字、上限 400（style_check 新增篇幅门禁，#11 旧 Feedback 692 字会在页面标红），只写一个过程问题和一到两个产物问题；录屏 3 到 5 步、只录 Feedback 写到的内容。③ qa-gen prompt：开头在「我在做一个／我想做个／帮我做一个」中轮换，不写环境套话；check_prompt 新增环境套话与开头说法两项提示（只标红不卡流程）。64 个测试通过。

> 状态（2026-11-15，续）：① Feedback 去掉固定开头：style.md 不再要求第一人称「我让模型做了什么」，5 分示例改为「模型在src/sync.py里…」；5 分写做好了什么，1 到 4 分只写没做好的，做对的部分不写；推荐写法改为内容清单、顺序可调。`check_feedback_content` 新增固定开头（`我让模型/它/ai/claude` 起笔）门禁，FEEDBACK_BANNED 加「还需修复」。② 录屏拆成不录的准备与真正录制的步骤：qa-review、qa-rework schema 新增 `record_prep`（setup、cleanup），qa_task 加 `record_prep` 列（`001_init.sql` 同步，增量 `004_record_prep.sql`）；录制部分 1 到 3 步、直接从产物页面上的问题开始，证据放在页面正常操作上，不在录屏里跑 heredoc、临时脚本或控制台注入，特殊输入文件在 setup 里生成后用页面导入按钮选；`_record_lines` 增加录屏目录的 Windows 访问路径；`record_issues` 新增录制部分含启动结束服务、临时脚本、控制台注入三项提示，并检查 setup、cleanup 的包装与路径；返修快照与输入带上 `record_prep`；录屏卡片显示录屏前准备与录完收尾。78 个测试通过。旧题的录屏步骤未改写，新规则从下次验收或返修起生效。

> 状态（2026-11-14 夜）：① 收集补交被忽略文件时中文路径被 git 转义导致 `git add` 失败：`gitops.git` 统一加 `-c core.quotepath=off`，列被忽略文件改用 `ls-files -z`，旧任务 `ignored_before` 里的转义路径经 `_unquote` 还原后比较。② 录屏步骤：验收与返修输入新增「录屏环境」（`_record_lines`：录屏目录、命令在录屏目录终端直接执行、不加 wsl.exe 包装），qa-review 与 qa-rework skill 补写法要求（不点精确像素坐标，需要时给控制台脚本）；`pipeline.record_issues` 在录屏卡片标出带包装、用验收或做题目录路径、要求点精确像素的步骤（只提示不卡流程）。alpha-layer-inspector 的录屏步骤已手工改写并实测（控制台脚本先把画布按原始尺寸显示再派发点击）。③ 轨迹文件在页面与提交表按 UNC 路径展示（`config.unc`），提交表轨迹文件与运行录屏两格点击复制所在文件夹（`cell.copy`，`config.win_parent`）；提交表弹窗高度固定。④ 非 `/api/` 响应加 `Cache-Control: no-cache`，避免前端改动被浏览器缓存。68 个测试通过。

> 状态（2026-11-14 夜，续）：`start.cmd` 改为调用 `start-detached.ps1` 后台启动，`start-detached.ps1` 启动后用 `scripts/wait-ready.sh` 跟踪本次新增的 server.log 并轮询 `/api/meta`，就绪输出启动成功标识（返回 0），进程退出返回 1，超时返回 2；新增 `stop.cmd` 与 `scripts/stop.sh`（有运行中的任务时列出并返回 3，`stop.cmd` 用 choice 确认后加 `--force`；SIGTERM 15 秒不退出再 SIGKILL）。已实测停止、未运行时停止、启动、已在运行、超时、列出运行中任务几条路径。WMI 创建进程时传 `Win32_ProcessStartup.ShowWindow = 0`，控制台一开始就隐藏，不再弹出空的 wsl.exe 窗口（DETACHED_PROCESS 会让内层 wsl.exe 自建可见控制台，CREATE_NO_WINDOW 被 WMI 以返回码 21 拒绝）；已用 UI Automation 核对启动前后 Windows Terminal 选项卡不变。

> 状态（2026-11-14 夜，再续）：支持用 CC Switch 或第三方 API 配置的 codex。配置新增 `codex_follow_toml`（默认关闭）与 `codex_config_toml`（留空自动定位）；`codex_runner.model_args` 打开时读取 config.toml 的 model、model_provider 与对应 provider 表转成 `-m`、`-c`，嵌套表展开，内置供应商不要求 provider 表，读不到、不合法、缺 provider 表时启动前报错；启动日志对键名像密钥的 `-c` 值打码（`mask_args`）；`failure_reason` 只看 stderr 与 codex 自身错误事件，新增认证失败识别，401/429 按整数匹配，限流提示去掉「ChatGPT」；冒烟脚本改用 `model_args` 并打印实际参数；配置页加勾选框与路径。75 个测试通过。实测：默认配置冒烟通过；临时运行目录下打开跟随（`model_provider = "openai"`）冒烟通过；provider 表指向不可达地址时 codex 持续重连，证明 `-c` 透传生效。随后新增 `codex_max_reconnects`（默认 10，0 不限制）：`run_skill` 统计连续的 `Reconnecting` 错误事件（`_is_reconnect_event`，其他事件重新计数），达到上限即 kill 并 `_kill_windows`；用假进程测试覆盖触发、重新计数与不限制，77 个测试通过；真实 codex 指向不可达地址、上限 3 时约 27 秒结束且无残留 codex.exe。真实 CC Switch 环境尚未实测。

1. 冒烟测试（已通过，脚本在 `_verify/smoke/`）：
   - WSL 中调用 Windows codex：`echo 回复ok | codex.exe exec --json --ephemeral --ignore-user-config --skip-git-repo-check -m gpt-6.1-sol -C E:\...\pipeline-agent -`，确认事件流可逐行读取、运行后 `codex login status` 仍为已登录、Codex 客户端仍在线、`~/.codex/sessions` 无新增。
   - 建一个最小 skill + schema，确认 `$skill名` 能触发、`-o` 输出符合 schema。
   - 在 codex 会话里执行一次 `wsl.exe ... bash -ic 'node -v'` 与一次 UNC 读文件。
   - WSL 中 `git ls-remote` 目标 GitHub 仓库，确认推送通道。
2. 根据冒烟结果修改设计文档（第 2、3 节重写；5.1/5.5 改为 stdin 输入 + schema 输出；5.2 补体检项；第 7 节配置页改为 codex 路径、模型、推理强度、WSL 发行版；第 9 节加入上面的冒烟测试）。文档不写方案调整原因。
3. 同步 WSL `~/jianzhi/feedback-pipeline/docs/` 中的设计文档副本。

> 状态（2026-11-15，再续）：运行日志抽屉改为多标签：`logTabs` 按 run_key 记录标题、读取位置、已读内容与状态，`pollLogs` 同时轮询所有运行中的标签，任一运行结束时刷新列表与详情；标签可切换、单独关闭，「关闭全部」收起抽屉，「停止」作用于当前标签。顶部导航新增「日志」按钮（`openLogsManually`）：把所有运行中的运行补成标签（标题为分支名加运行类型），没有运行中的也没有已开标签时打开最近一次运行。两个验收同时运行时实测两个标签各自更新、内容不串，手动打开能补出两个运行中的标签。78 个测试通过。

> 状态（2026-11-15，三续）：提交表新增导出 Excel：`submission.to_xlsx` 用 openpyxl 按 COLUMNS 写表头与各行（序号、交付完整性按数字，其余按文本，http 链接加超链接，冻结首行，长文本换行），`/api/submission/xlsx` 与 `/api/submission` 共用 `_submission_tasks` 选题范围，没有题目时返回 400；弹窗加「导出 Excel」按钮，文件名「提交表-时间戳.xlsx」。requirements 补 openpyxl==3.1.5（运行环境已装）。79 个测试通过；重启后端后在页面实测导出 3 道已完成题目，内容核对一致。

> 状态（2026-11-16）：① 录屏环境就绪标记：qa_task 加 `record_ready_sha`、`record_ready_time`（`001_init.sql` 同步，增量 `005_record_ready_client_submit.sql`），`start_prepare_record` 开始时清标记、克隆成功后写入；`pipeline.record_ready` 要求标记的快照等于当前 `artifact_sha` 且录屏目录存在，`view` 输出 `record_ready`。页面就绪后按钮变「重新准备录屏环境」并先确认，详情顶部与录屏卡片显示就绪状态。② 甲方提交标记：qa_task 加 `client_seq`、`client_submit_time`（同一增量脚本），新动作 `client_submit`（`pipeline.mark_client_submit`，需已有 seq，去掉前导 #，留空取消，改序号保留首次时间）；`/api/tasks` 与 `/api/submission` 带出 `client_seq`；提交表弹窗标题行「标记已提交 / 改」只刷新该行，列表序号下方与详情顶部显示「甲方 #序号」；不进 COLUMNS 与 xlsx。③ 收集不再弹窗询问概述，提交信息走后端默认 `feat: 模型产物 - <分支名>`（后端 summary 参数保留）。83 个测试通过。

> 状态（2026-11-16，续）：在 `auto` 分支（worktree `feedback-pipeline-app-auto`）新增全自动（设计文档 5.11）。① 补充题目池：`autopilot.GenLoop` 循环调用 `start_gen`（每轮 `pool_gen_count`，默认 10，子方向、难度、偏好、补充要求为空），停止在本轮结束后生效，失败或连续两轮空即终止；`/api/auto/gen`。② 自动做题：`autopilot.DoLoop` 每 5 秒 `tick`，做题线 1 道、后段积压上限 `auto_backlog_max`、录屏 1 道；失败、待准备环境有待处理项、自动查验不通过时写 `auto_pause`（环节、原因、`hints_for` 给出的建议、出错运行）；`auto_retry`（可 `regen_script`）、`auto_release`、`auto_join` 三个动作，人工执行原有动作时 `clear_pause`，题目有任务运行时禁止只改状态的人工动作；`/api/auto`、`/api/auto/do`。qa_task 加 `auto_run`、`auto_pause`、`record_script`（`001_init.sql` 同步，增量 `006_auto.sql`）。③ `claude_driver`：`tmux -L feedback` 会话 `fbdo-<id>`，`env -i` 白名单环境加 `bash -lic`；`pick_option` 按选项文字处理信任目录与 bypass 确认框，底栏 `bypass permissions on` 判就绪；括号粘贴加回车，等首条输入一致的新会话文件；`trajectory.turn_state` 以主链末条 assistant 后的 `system/turn_duration` 判一轮结束，并识别接口报错与 AskUserQuestion；开始前有未作废会话时调 `pipeline.void_and_reset`（由 `start_redo` 抽出）。④ `pipeline.approve_errors`、`auto_check_issues`（通过门禁加录屏步骤与自检）、`prepare_record_dir`（由 `start_prepare_record` 抽出）。⑤ 自动录屏：`recorder.VirtualScreen`（Xvfb 抽象套接字、专用 PulseAudio 空设备、录屏前准备的进程组一并清理），`login_env` 取新开终端的环境；新 skill `qa-record` 与 `schemas/qa-record.json`，用 `dryrun.sh` 在同一虚拟屏试跑；`record_harness/harness.mjs`（有界面 chromium、终端标签、可见指针、ffmpeg x11grab 加 pulse）；试跑后重新克隆并重新准备再正式录，ffprobe 校验 1280x720 与时长后移到 `recording_dir` 并 `complete`；`jobs.ai_slot` 只在生成脚本时占 AI 并发名额，线程池扩到 16。⑥ `envcheck.auto_tools_check` 与 `record_tools_missing`；`setup-verify-tools.sh` 加 tmux、xvfb、pulseaudio、fonts-noto-cjk；`stop.sh` 在开关开着时拒绝停止。⑦ 页面：补充题目池卡片、工具栏开关与概况、列表「自动」「需处理」、详情停下红框、观看命令、录屏脚本折叠、七个新配置项。107 个测试通过。冒烟：虚拟屏上执行器跑页面点击加终端命令并正式录制，得到 1280x720 mp4、画面核对正确、服务进程被清理；Claude 驱动在临时目录启动到就绪（自动确认信任目录）后关闭。尚未实测：pulseaudio 未安装所以声音未录过；真实题目的粘贴、做完判定与收集；`qa-record` 未用真实 codex 跑过。

> 状态（2026-11-16，再续）：录屏画面改为接近 Windows 本机。去掉页面内画的红色指针与浏览器里的终端标签：新增 `app/wincursor.py` 按像素图生成 Windows 样式 Xcursor 主题（主题名 default，因为 Chromium 只读 `XCURSOR_PATH` 与 GTK 的 `gtk-cursor-theme-name`，执行器另给浏览器一份 `XDG_CONFIG_HOME` 下的 GTK 设置）；`recorder.windows_fonts` 把 `/mnt/c/Windows/Fonts` 中的 Consolas、Segoe UI、微软雅黑等链接进来并写 fonts.conf；Xvfb 加 `-dpi 96`，有 xsetroot 时设底色与根窗口指针；专用 PulseAudio 去掉会话总线（与已有实例抢 D-Bus 名称会启动失败）。执行器改用 xterm（Campbell 配色，`faceName` 带微软雅黑补中文字形，`--rcfile` 加载 `~/.bashrc` 并用提示符钩子写退出码）与 xdotool（平滑移动、真实点击、滚轮、键入），新增 `moveTo`、`scroll`、`showBrowser`、`exec`，`term.run` 只返回退出码，截图改为整块屏幕；qa-record skill 同步。`envcheck` 与 `setup-verify-tools.sh` 加 xterm、xdotool、x11-xserver-utils。108 个测试通过；冒烟（用解压到 `~/.local/fb-x11` 的 xterm、xdotool）核对了按钮上的 Windows 箭头、链接上的手形、终端中文与配色、音轨（aac，440Hz 提示音）。后端已从本工作区启动，迁移到 006。

> 状态（2026-11-16，三续）：实测。真实库 #19 constrained-lod-forge 自动走到做题：准备环境停在显卡人工确认（确认后继续）、推送初始快照、tmux 启动 Claude Code、自动确认信任目录、粘贴 prompt 且新会话首条输入一致；随后人工停止（会话被关闭），#19 已作废该会话恢复为待做题并转人工，做完判定与收集未实测。另起隔离测试实例（真实库副本、端口 8799、验收与录屏目录在 /tmp）把已完成的 #13 loudness-delivery-desk 退回待查验：自动查验通过；自动录屏首次因 `config.json` 的 Codex 工作区指向主仓库（无 qa-record）失败停下，改测试实例配置后「重试」成功——qa-record 约 3.5 分钟生成并试跑脚本，重新克隆准备后正式录制 28.2 秒 1280x720 带音轨，画面核对 Windows 指针、滚动、导入文件与播放按钮操作正常，标记完成。修复：`complete` 清掉之前失败留下的 last_error；浏览器关闭翻译界面。109 个测试通过。注意：本工作区合并前，真实库配置里的 Codex 工作区仍指向主仓库 `agent/`，自动录屏会因找不到 qa-record 失败。

> 状态（2026-11-16，四续）：全自动已合并到 main（2c906cc），后端改从 main 工作区启动。新增「做完当前题后停止」：`DoLoop` 三种运行方式 running、finishing、stopping，`finish()` 在运行中切换、未开启时检查工具后以 finishing 开启（只推进已接管的题），finishing 时 `start()` 恢复取题；`tick(pick=False)` 只推进不取题，`unfinished()` 为空且无在跑任务时关闭；`/api/auto/do` 加 finish；页面运行中显示「做完当前题后停止」「停止做题」，finishing 时显示「继续取题」；`stop.sh` 识别 finishing。114 个测试通过。

> 状态（2026-11-16，五续）：序号改为甲方序号。① 去掉自编序号：`qa_task.seq` 删除（`001_init.sql` 同步，增量 `007_client_seq.sql` 删列并建 `uk_qa_task_client_seq` 部分唯一索引），`config.seq_base`、`db.max_seq` 与完成时编号一并去掉；`client_seq` 改为出题时分配的甲方序号，即提交表「序号」列；`due_flag` 改看 `client_submit_time`。② 批量出题必填甲方序号：`pipeline.parse_client_seqs` 解析逗号、顿号、空白分隔与区间（1 到 10 个，去前导零，不能重复），`/api/gen` 未填时 400，`start_gen` 校验占用后数量取序号个数，入库按顺序分配，出题期间被占用的不分配，没用上的写日志；页面表单「数量」换成「甲方序号」并实时提示将出几道题。③ 补充题目池暂停：`/api/auto/gen` 的 start 一律 400，页面点击只提示暂停使用，stop 保留。④ 甲方提交标记改为开关：`client_submit` 动作收 `submitted`，需有甲方序号且已完成或待返修（已标记的可取消）；新动作 `client_seq`（`pipeline.set_client_seq`）修改或补填，唯一校验、只能一个、已提交的不能清空。⑤ 排序：`/api/tasks` 按甲方序号数值降序、无序号的排最后；提交表按甲方序号升序。⑥ 提交表折叠行加 User Prompt 第一句（第一个非空行到首个句末标点，超过 80 字截断，展开后隐藏）。运行库已手工写入 #19 到 #26 的甲方序号 12100 到 12107；vfr-cut-conformer、rig-pose-blender 仍无甲方序号，需在详情补填。118 个测试通过；重启后端后实测：迁移已执行（seq 列删除、唯一索引建立），列表按甲方序号排列，补充题目池开始被拒，已占用序号出题被拒，提交表折叠行显示 Prompt 第一句。

> 状态（2026-11-16，六续）：提交表从弹窗改为单独页面。顶部菜单新增「提交表」（`#tab-submission`，位于题目之后）；`showTab` 统一切换标签，切到提交表时 `loadSubmission` 重新读取；工具栏吸顶，范围切换「全部已完成 / 列表勾选的」（`subScope`，没有勾选时不可选并退回全部），另有刷新、全部展开折叠、复制表头、导出 Excel；题目列表的「提交表」按钮跳到该页（有勾选时看勾选的）。去掉弹窗的 `.modal` 样式、关闭按钮与 Esc 关闭。
