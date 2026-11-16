-- 流水线数据库初始化脚本（SQLite）
-- SQLite 不支持 comment on 语法，表与字段说明以行内注释形式给出
-- 基础列：id 自增主键（等价 serial8）；creator、create_time、updater、update_time、deleted、tenant_id 为审计列

-- 题目表：一道题从出题到导出的全部字段
CREATE TABLE IF NOT EXISTS qa_task (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,  -- 主键
    seq                 INTEGER,                            -- 提交表序号（首次导出时分配）
    branch              VARCHAR(64) NOT NULL,               -- 分支名（项目短名，库内唯一）
    status              VARCHAR(16) NOT NULL,               -- 当前状态
    failed_from         VARCHAR(16),                        -- 失败前所处状态（用于重试）
    gen_run_id          VARCHAR(64),                        -- 出题运行 ID
    user_prompt         TEXT NOT NULL,                      -- User Prompt 原文
    domain              VARCHAR(32),                        -- 所属领域
    subdomain           VARCHAR(32),                        -- 子方向
    task_type           VARCHAR(16),                        -- 任务类型
    difficulty          VARCHAR(8),                         -- 任务难度
    lang_framework      VARCHAR(256),                       -- 语言/框架
    acceptance_points   TEXT,                               -- 内部验收要点（JSON 数组）
    materials           TEXT,                               -- 素材清单（JSON 数组，含获取状态）
    env_requirements    TEXT,                               -- 环境需求（JSON）
    similarity_note     TEXT,                               -- 与已有题目的差异说明
    harness             VARCHAR(32),                        -- Harness
    harness_version     VARCHAR(32),                        -- Harness 版本
    os                  VARCHAR(32),                        -- 操作系统
    reproducibility     VARCHAR(64),                        -- 环境可复现等级
    init_sha            VARCHAR(40),                        -- 初始环境快照 SHA
    ignored_before      TEXT,                               -- 做题前被忽略文件清单（JSON 数组）
    client_check        TEXT,                               -- 做题客户端体检结果（JSON）
    env_check           TEXT,                               -- 工具链与显卡检查结果（JSON）
    net_check           TEXT,                               -- 外网连通性检查结果（JSON）
    expected_model      VARCHAR(128),                       -- 体检时记录的 ANTHROPIC_MODEL
    voided_sessions     TEXT,                               -- 已作废的 SessionID（JSON 数组）
    redo_count          INTEGER NOT NULL DEFAULT 0,         -- 重做次数
    session_id          VARCHAR(64),                        -- SessionID
    prompt_id           VARCHAR(64),                        -- TurnID/PromptID
    continue_count      INTEGER NOT NULL DEFAULT 0,         -- 「继续」次数
    model_name          VARCHAR(128),                       -- 轨迹中的模型
    done_time           VARCHAR(32),                        -- 做题完成时间（ISO 字符串，本地时间）
    trajectory_path     VARCHAR(512),                       -- 待上传轨迹的本地路径
    trajectory_link     VARCHAR(512),                       -- 轨迹文件链接
    traj_summary        TEXT,                               -- 轨迹摘要（JSON）
    artifact_sha        VARCHAR(40),                        -- 产物快照 SHA
    recording_link      VARCHAR(512),                       -- 运行录屏链接
    evidence            TEXT,                               -- 证据清单（JSON 数组）
    delivery_score      INTEGER,                            -- 交付完整性 1-5
    score_reason        TEXT,                               -- 评分理由
    feedback            TEXT,                               -- Feedback 文本
    record_steps        TEXT,                               -- 录屏步骤（JSON 数组）
    record_prep         TEXT,                               -- 录屏前准备与录完收尾的命令，不录（JSON 对象 setup cleanup）
    record_ready_sha    VARCHAR(40),                        -- 录屏环境已准备的产物快照 SHA（与当前产物快照一致才算就绪）
    record_ready_time   TIMESTAMP,                          -- 录屏环境准备完成时间
    client_seq          VARCHAR(64),                        -- 甲方平台的序号（为空表示尚未在甲方平台提交）
    client_submit_time  TIMESTAMP,                          -- 标记已在甲方平台提交的时间
    review_self_check   TEXT,                               -- 验收自检结果（JSON）
    review_comment      TEXT,                               -- 打回意见
    remark              TEXT,                               -- 备注
    last_error          TEXT,                               -- 最近一次失败原因
    creator             VARCHAR(64),                        -- 创建者
    create_time         TIMESTAMP NOT NULL,                 -- 创建时间
    updater             VARCHAR(64),                        -- 更新者
    update_time         TIMESTAMP NOT NULL,                 -- 更新时间
    deleted             SMALLINT NOT NULL DEFAULT 0,        -- 是否删除
    tenant_id           BIGINT NOT NULL DEFAULT 0           -- 租户编号
);
CREATE UNIQUE INDEX IF NOT EXISTS uk_qa_task_branch ON qa_task(branch) WHERE deleted = 0;

-- 运行记录表：每次自动环节（AI 或脚本）的一次执行
CREATE TABLE IF NOT EXISTS qa_run (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,  -- 主键
    run_key             VARCHAR(64) NOT NULL,               -- 运行 ID（目录名与日志名）
    task_id             INTEGER,                            -- 关联题目（出题运行为空）
    kind                VARCHAR(32) NOT NULL,               -- 环节类型
    status              VARCHAR(16) NOT NULL,               -- running / success / failed
    log_path            VARCHAR(512),                       -- 日志文件路径
    error               TEXT,                               -- 失败原因
    started_at          TIMESTAMP,                          -- 开始时间
    finished_at         TIMESTAMP,                          -- 结束时间
    creator             VARCHAR(64),                        -- 创建者
    create_time         TIMESTAMP NOT NULL,                 -- 创建时间
    updater             VARCHAR(64),                        -- 更新者
    update_time         TIMESTAMP NOT NULL,                 -- 更新时间
    deleted             SMALLINT NOT NULL DEFAULT 0,        -- 是否删除
    tenant_id           BIGINT NOT NULL DEFAULT 0           -- 租户编号
);
CREATE UNIQUE INDEX IF NOT EXISTS uk_qa_run_key ON qa_run(run_key);

-- 质检记录表：甲方每返回一次待返修结果记一条，同一题可有多轮（存量环境由 002、003 增量脚本补齐）
CREATE TABLE IF NOT EXISTS qa_inspection (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,  -- 主键
    task_id             INTEGER NOT NULL,                   -- 关联题目
    round               INTEGER NOT NULL,                   -- 质检轮次（同一题从 1 递增）
    inspect_time        VARCHAR(32),                        -- 质检时间（取自质检原文）
    verdict             VARCHAR(16) NOT NULL,               -- 质检结论（只登记待返修，不通过的数据直接放弃）
    raw_text            TEXT NOT NULL,                      -- 质检原文
    problem             TEXT,                               -- 问题（原文中的问题段，无分段时为正文）
    evidence            TEXT,                               -- 证据（原文中的证据段）
    suggestion          TEXT,                               -- 修改建议（原文中的修改建议段）
    state               VARCHAR(16) NOT NULL,               -- 处理状态：返修中 / 已返修
    before_snapshot     TEXT,                               -- 返修前的提交内容快照（JSON）
    rework_items        TEXT,                               -- 早期版本的逐条处理结论（JSON 数组，现已不再写入）
    need_record         SMALLINT NOT NULL DEFAULT 0,        -- 返修后是否需要重新录屏
    summary             TEXT,                               -- AI 返修的处理说明
    resolved_time       TIMESTAMP,                          -- 返修完成时间
    creator             VARCHAR(64),                        -- 创建者
    create_time         TIMESTAMP NOT NULL,                 -- 创建时间
    updater             VARCHAR(64),                        -- 更新者
    update_time         TIMESTAMP NOT NULL,                 -- 更新时间
    deleted             SMALLINT NOT NULL DEFAULT 0,        -- 是否删除
    tenant_id           BIGINT NOT NULL DEFAULT 0           -- 租户编号
);
CREATE INDEX IF NOT EXISTS idx_qa_inspection_task ON qa_inspection(task_id);

-- 数据库版本表：记录已执行的初始化与增量脚本
CREATE TABLE IF NOT EXISTS schema_version (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,  -- 主键
    script              VARCHAR(128) NOT NULL,              -- 脚本文件名
    creator             VARCHAR(64),                        -- 创建者
    create_time         TIMESTAMP NOT NULL,                 -- 创建时间
    updater             VARCHAR(64),                        -- 更新者
    update_time         TIMESTAMP NOT NULL,                 -- 更新时间
    deleted             SMALLINT NOT NULL DEFAULT 0,        -- 是否删除
    tenant_id           BIGINT NOT NULL DEFAULT 0           -- 租户编号
);
