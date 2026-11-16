-- 增量脚本：自动做题（调度器接管标记、停下待处理的信息、自动录屏脚本缓存）
-- SQLite 不支持 comment on 语法，字段说明以行内注释形式给出
-- 新环境由 001_init.sql 直接建出含这些字段的表，不执行本脚本

ALTER TABLE qa_task ADD COLUMN auto_run SMALLINT NOT NULL DEFAULT 0;  -- 是否由自动做题接管（1 接管，0 人工）
ALTER TABLE qa_task ADD COLUMN auto_pause TEXT;                       -- 自动流程停下待处理的信息（JSON：环节、原因、处理建议、出错运行），为空表示未停下
ALTER TABLE qa_task ADD COLUMN record_script TEXT;                    -- 自动录屏脚本缓存（JSON：产物快照、录屏步骤摘要、脚本、预计时长）
