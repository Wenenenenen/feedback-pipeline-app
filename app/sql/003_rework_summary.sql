-- 增量脚本：返修改为只由 AI 按质检原文处理，质检记录增加处理说明
-- SQLite 不支持 comment on 语法，字段说明以行内注释形式给出
-- 新环境由 001_init.sql 直接建出含该字段的表，不执行本脚本

ALTER TABLE qa_inspection ADD COLUMN summary TEXT;  -- AI 返修的处理说明
