-- 增量脚本：新增质检记录表，承接甲方质检结果登记与返修
-- SQLite 不支持 comment on 语法，表与字段说明以行内注释形式给出
-- 新环境由 001_init.sql 直接建出最新结构，不执行本脚本；存量环境执行本脚本后再由 003 补字段

-- 质检记录表：甲方每返回一次质检结果记一条，同一题可有多轮
CREATE TABLE IF NOT EXISTS qa_inspection (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,  -- 主键
    task_id             INTEGER NOT NULL,                   -- 关联题目
    round               INTEGER NOT NULL,                   -- 质检轮次（同一题从 1 递增）
    inspect_time        VARCHAR(32),                        -- 质检时间（取自质检原文）
    verdict             VARCHAR(16) NOT NULL,               -- 质检结论：不通过 / 待返修
    raw_text            TEXT NOT NULL,                      -- 质检原文
    problem             TEXT,                               -- 问题（原文中的问题段，无分段时为正文）
    evidence            TEXT,                               -- 证据（原文中的证据段）
    suggestion          TEXT,                               -- 修改建议（原文中的修改建议段）
    state               VARCHAR(16) NOT NULL,               -- 处理状态：返修中 / 已返修 / 已作废
    before_snapshot     TEXT,                               -- 返修前的提交内容快照（JSON）
    rework_items        TEXT,                               -- AI 返修对每条质检问题的处理结论（JSON 数组）
    need_record         SMALLINT NOT NULL DEFAULT 0,        -- 完成返修时是否需要重新录屏
    resolved_time       TIMESTAMP,                          -- 返修完成时间
    creator             VARCHAR(64),                        -- 创建者
    create_time         TIMESTAMP NOT NULL,                 -- 创建时间
    updater             VARCHAR(64),                        -- 更新者
    update_time         TIMESTAMP NOT NULL,                 -- 更新时间
    deleted             SMALLINT NOT NULL DEFAULT 0,        -- 是否删除
    tenant_id           BIGINT NOT NULL DEFAULT 0           -- 租户编号
);
CREATE INDEX IF NOT EXISTS idx_qa_inspection_task ON qa_inspection(task_id);
