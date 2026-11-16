-- 增量脚本：提交表的 Harness 改为「Claude Code CLI」，操作系统改为「Linux」，存量题目一并更新
-- 只改数据不改结构，新环境执行 001_init.sql 时不需要本脚本（新题出题与收集时直接写入新取值）

UPDATE qa_task SET harness = 'Claude Code CLI' WHERE harness = 'Claude Code';
UPDATE qa_task SET os = 'Linux' WHERE os = 'MacOS/Linux';
