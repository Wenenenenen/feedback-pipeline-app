# 出题评测流水线（feedback-pipeline-app）

「CC/Codex 用户反馈标注」作业的出题、做题管理、收集、验收、提交表流水线。部署与使用见 `README.md`；方案见 `docs/2026-11-12-qa-pipeline-design.md`（面向用户），实施记录与进度见 `docs/2026-11-12-qa-pipeline-plan.md`（面向 Agent）。方案或流程有变化时更新这两份，不另起新文档。

## 结构

- `app/`：FastAPI + SQLite 后端，页面在 `app/static/`，建表脚本在 `app/sql/`。
- `agent/`：Windows 版 Codex 的工作区（`AGENTS.md`、`style.md`、`banned-topics.md`、`.agents/skills/qa-gen`、`.agents/skills/qa-review`、`.agents/skills/qa-rework`、`schemas/`），后端通过 `codex exec` 调用；改这里就是改出题与验收的提示词。`agent/runs/` 是运行记录，不进仓库。
- `docs/private/`：作业文档、质检说明等本地参考资料，不进仓库。
- 运行时在 WSL `~/jianzhi/feedback-pipeline/`（虚拟环境、数据库、配置、日志、轨迹副本）；做题、验收、录屏目录分别是 WSL `~/jianzhi/feedback-tasks`、`feedback-verify`、`feedback-record` 下的 `<分支名>/`。

## 约定

- 做题用 WSL 的 Claude Code（干净环境，流水线不调用）；出题与验收用 Windows 的 Codex（ChatGPT 登录，与 Codex 客户端共用，不要动 `~/.codex/auth.json`）。
- 代码常量与 schema 枚举必须一致：`pipeline.DOMAINS` 对应 `agent/schemas/qa-gen.json` 的子方向，`pipeline.REPRO_LEVELS` 对应 `agent/schemas/qa-review.json` 与 `qa-rework.json` 的可复现等级，测试会核对。
- Feedback 写法规则在 `agent/style.md`，后端规则检查在 `app/style_check.py`，两边一起改。
- 仓库根目录不要放 `AGENTS.md`：Codex 会从仓库根一路加载到 `agent/`。
- 仓库是公开的：作业文档、质检说明、提交表等作业方材料只放 `docs/private/`，不要提交。
- 新建顶层工作目录统一加 `feedback-` 前缀。
- 建表、改表遵循全局的基础列规范，同时新增 `app/sql/` 下的增量脚本；`001_init.sql` 要同步成最新结构（新库只执行初始化脚本，增量脚本只给存量库补执行）。

## 常用命令

- 测试（WSL 中）：`cd <仓库的 WSL 路径> && ~/jianzhi/feedback-pipeline/.venv/bin/python -m pytest -q -p no:cacheprovider`
- 重启后端：先确认 `/api/runs` 没有运行中的任务，在 WSL 中结束 `uvicorn app.main:app` 进程，再在 Windows 中执行 `pwsh -File scripts\start-detached.ps1`。不要用会话内的后台任务启动后端，会话结束时后端会跟着退出，正在跑的验收也会中断。
- Codex 冒烟（WSL 中）：`bash scripts/smoke-codex.sh`
