-- 增量脚本：去掉自编的提交表序号，统一使用甲方序号（出题时分配），甲方序号在未删除的题目中唯一
-- SQLite 不支持 comment on 语法，字段说明以行内注释形式给出
-- 新环境由 001_init.sql 直接建出最新结构，不执行本脚本
-- client_seq 字段含义改为：甲方序号（出题时分配，也是提交表的序号）；是否已在甲方平台提交看 client_submit_time

ALTER TABLE qa_task DROP COLUMN seq;
CREATE UNIQUE INDEX IF NOT EXISTS uk_qa_task_client_seq ON qa_task(client_seq) WHERE deleted = 0 AND client_seq IS NOT NULL;
