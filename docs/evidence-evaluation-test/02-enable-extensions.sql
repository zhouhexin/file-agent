-- 管理员先切换到新库，再执行本文件；不在旧 fileAgent 库中执行。
-- 使用同一事务，目标校验失败后后续语句也不能继续创建扩展。
BEGIN;
DO $$
BEGIN
    IF current_database() <> 'file_agent_evidence_test' THEN
        RAISE EXCEPTION '请先连接新测试库 file_agent_evidence_test';
    END IF;
END $$;

-- vector 是既有迁移的依赖，即使本次关闭向量生成，建表仍需要此扩展。
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
COMMIT;
