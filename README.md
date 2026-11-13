# 出题评测流水线

「CC/Codex 用户反馈标注」作业的出题、做题管理、收集、验收、提交表流水线。设计见 `docs/2026-11-12-qa-pipeline-design.md`（WSL 中 `~/jianzhi/feedback-pipeline/docs/`）。

## 目录

| 位置 | 内容 |
|---|---|
| `E:\WorkSpace\jianzhi\feedback-pipeline-app\` | 本仓库：`app/` 后端与页面、`tests/`、`scripts/` |
| `E:\WorkSpace\jianzhi\feedback-pipeline-app\agent\` | Codex 工作区：AGENTS.md、style.md、banned-topics.md、`.agents/skills/`、`schemas/`；`runs/` 为每次运行的输入输出，不进仓库 |
| WSL `~/jianzhi/feedback-pipeline/` | 运行时：`.venv/`、`data/`（数据库、日志、轨迹、备份）、`docs/` |
| WSL `~/jianzhi/feedback-tasks/<分支名>/` | 做题目录 |
| WSL `~/jianzhi/feedback-verify/<分支名>/` | 验收克隆 |
| WSL `~/jianzhi/feedback-record/<分支名>/` | 录屏克隆 |

## 启动

```bash
# 在 WSL 中（前台运行，关闭终端即停止）
bash /mnt/e/WorkSpace/jianzhi/feedback-pipeline-app/scripts/start.sh
```

```powershell
# 在 Windows 中（后台常驻，不随终端或会话退出而结束，日志写入 ~/jianzhi/feedback-pipeline/data/logs/server.log）
pwsh -File E:\WorkSpace\jianzhi\feedback-pipeline-app\scripts\start-detached.ps1
```

运行验收等长任务时使用后台常驻方式启动；后端退出会中断正在运行的 skill，重启后该运行记为失败，需重新发起。

浏览器打开 `http://localhost:8765`。端口可用环境变量 `PIPELINE_PORT` 修改。

首次启动会创建虚拟环境并安装依赖。

验收需要的 ffmpeg、playwright 等工具用下面的脚本安装（需要 sudo 密码）：

```bash
bash /mnt/e/WorkSpace/jianzhi/feedback-pipeline-app/scripts/setup-verify-tools.sh
```

## 使用流程

1. **批量出题**：在「批量出题」页填写数量、领域等，后台调用 `codex exec '$qa-gen'`，题目入库为「待初始化」。
2. **初始化**：创建 `~/jianzhi/feedback-tasks/<分支名>/` 孤儿分支仓库（此时不推送）。
3. **准备环境**：自动下载素材、执行生成脚本或人工放入后确认；执行环境检查（工具链、显卡、外网、做题客户端体检）；全部通过后提交并推送初始快照。
4. **做题**：复制做题命令，在 WSL 中启动 Claude Code，发送 prompt 原文；结束后点「做题完成」。模型无故中断或网络报错时点「重做」。
5. **收集**：定位轨迹、校验单轮与污染、提交并推送产物快照、生成轨迹摘要。
6. **验收**：全新克隆产物，调用 `codex exec '$qa-review'` 实测并写 Feedback。
7. **查验**：编辑评分与 Feedback（口吻检查通过才能点「通过」），或填写意见打回重新生成。
8. **录屏**：「准备录屏环境」得到干净克隆，按录屏步骤录制；页面自动取录屏目录（默认 `E:\GSB-Recordings`）中最新的 mp4，核对时长与分辨率后标记完成，此时按配置的已有最大序号接续编号。
9. **提交表**：列表上方点「提交表」弹窗查看勾选题目（未勾选时为全部已完成题目）的提交表各列，单元格点击即复制，也可整行复制后粘贴到表格。

## 测试

```bash
cd /mnt/e/WorkSpace/jianzhi/feedback-pipeline-app
~/jianzhi/feedback-pipeline/.venv/bin/python -m pytest -q -p no:cacheprovider
```

执行环境冒烟测试脚本见 `E:\WorkSpace\jianzhi\feedback\_verify\smoke\`。

## 注意

- codex 使用 Windows 默认 `~/.codex` 的 ChatGPT 登录，与 Codex 客户端共用；不要复制 `auth.json`，不要在 WSL 中运行 Linux 版 codex 指向 Windows 的 `.codex`。
- GitHub 推送使用 WSL 中已配置的 ssh 凭据。
- 数据库每天备份到 `data/backups/`，保留 14 份。
