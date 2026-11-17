# 出题评测流水线（feedback-pipeline-app）

「CC/Codex 用户反馈标注」作业的出题、做题管理、收集、验收、提交表流水线。部署与使用见 `README.md`；方案见 `docs/2026-11-12-qa-pipeline-design.md`（面向用户），实施记录与进度见 `docs/2026-11-12-qa-pipeline-plan.md`（面向 Agent）。方案或流程有变化时更新这两份，不另起新文档。

## 结构

- `app/`：FastAPI + SQLite 后端，页面在 `app/static/`，建表脚本在 `app/sql/`。全自动：`autopilot.py`（补充题目池与自动做题调度）、`claude_driver.py`（tmux 驱动 Claude Code 做题）、`recorder.py` 与 `record_harness/harness.mjs`（WSL 虚拟屏后台录屏）。写入飞书：`feishu.py`（按 UID 即甲方序号映射与调度）经 `feishu_bridge.mjs` 调用 `plugin/feishu-table-tool`（WSL 中 `lark-cli` 的用户身份）。
- `plugin/feishu-table-tool/`：飞书表格填写插件（Node 库，无 npm 依赖），代码不改；其中指向甲方表的 `config.json`、`examples/`、`VALIDATION.md` 不进仓库。
- `agent/`：Windows 版 Codex 的工作区（`AGENTS.md`、`style.md`、`banned-topics.md`、`.agents/skills/qa-gen`、`.agents/skills/qa-review`、`.agents/skills/qa-rework`、`.agents/skills/qa-record`、`schemas/`），后端通过 `codex exec` 调用；改这里就是改出题与验收的提示词。`agent/runs/` 是运行记录，不进仓库。
- `docs/private/`：作业文档、质检说明等本地参考资料，不进仓库。
- 运行时在 WSL `~/jianzhi/feedback-pipeline/`（虚拟环境、数据库、配置、日志、轨迹副本）；做题、验收、录屏目录分别是 WSL `~/jianzhi/feedback-tasks`、`feedback-verify`、`feedback-record` 下的 `<分支名>/`。

## 约定

- 做题用 WSL 的 Claude Code（干净环境）：手动模式由人启动；自动做题由 `claude_driver` 在独立 tmux 服务器（`tmux -L feedback`）里启动交互式会话并粘贴 prompt 原文，结束时直接关会话、不再输入任何内容（唯一例外是接口报错中断时按配置自动输入「继续」），不改 `~/.claude`；出题与验收用 Windows 的 Codex（默认 ChatGPT 登录，与 Codex 客户端共用，不要动 `~/.codex/auth.json`；CC Switch 或第三方 API 的环境在配置页勾选「沿用 config.toml 的供应商与模型」，由 `codex_runner.model_args` 只读取 config.toml 的模型与供应商转成 `-m`、`-c`，`--ignore-user-config` 保留）。
- 代码常量与 schema 枚举必须一致：`pipeline.DOMAINS` 对应 `agent/schemas/qa-gen.json` 的子方向，`pipeline.REPRO_LEVELS` 对应 `agent/schemas/qa-review.json` 与 `qa-rework.json` 的可复现等级，测试会核对。
- Feedback 写法规则在 `agent/style.md`，后端规则检查在 `app/style_check.py`，两边一起改。
- 仓库根目录不要放 `AGENTS.md`：Codex 会从仓库根一路加载到 `agent/`。
- 仓库是公开的：作业文档、质检说明、提交表等作业方材料只放 `docs/private/`，不要提交。
- 新建顶层工作目录统一加 `feedback-` 前缀。
- 建表、改表遵循全局的基础列规范，同时新增 `app/sql/` 下的增量脚本；`001_init.sql` 要同步成最新结构（新库只执行初始化脚本，增量脚本只给存量库补执行）。

## 常用命令

- 测试（WSL 中）：`cd <仓库的 WSL 路径> && ~/jianzhi/feedback-pipeline/.venv/bin/python -m pytest -q -p no:cacheprovider`
- 重启后端：在 WSL 中执行 `bash scripts/stop.sh`（有运行中的任务时会列出并拒绝停止，不要随意加 `--force`），再在 Windows 中执行 `pwsh -File scripts\start-detached.ps1`（等到接口可访问才返回，失败返回非 0）。不要用会话内的后台任务启动后端，会话结束时后端会跟着退出，正在跑的验收也会中断。
- Codex 冒烟（WSL 中）：`bash scripts/smoke-codex.sh`
