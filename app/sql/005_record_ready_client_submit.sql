-- 增量脚本：记录录屏环境是否已准备，以及是否已在甲方平台提交（甲方序号）
-- SQLite 不支持 comment on 语法，字段说明以行内注释形式给出
-- 新环境由 001_init.sql 直接建出含这些字段的表，不执行本脚本

ALTER TABLE qa_task ADD COLUMN record_ready_sha VARCHAR(40);    -- 录屏环境已准备的产物快照 SHA（与当前产物快照一致才算就绪）
ALTER TABLE qa_task ADD COLUMN record_ready_time TIMESTAMP;     -- 录屏环境准备完成时间
ALTER TABLE qa_task ADD COLUMN client_seq VARCHAR(64);          -- 甲方平台的序号（为空表示尚未在甲方平台提交）
ALTER TABLE qa_task ADD COLUMN client_submit_time TIMESTAMP;    -- 标记已在甲方平台提交的时间
