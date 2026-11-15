-- 增量脚本：启动与结束服务从录屏步骤中拆出，放到不录的录屏前准备
-- SQLite 不支持 comment on 语法，字段说明以行内注释形式给出
-- 新环境由 001_init.sql 直接建出含该字段的表，不执行本脚本

ALTER TABLE qa_task ADD COLUMN record_prep TEXT;  -- 录屏前准备与录完收尾的命令，不录（JSON 对象 setup cleanup）
