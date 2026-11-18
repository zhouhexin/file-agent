-- 请由具有 CREATEDB 权限的管理员，在自动提交模式下执行。
-- 仅创建新数据库；同名库已存在时停止，禁止删除或清空旧库。
CREATE DATABASE file_agent_evidence_test_20261029
    OWNER fileagent_user
    TEMPLATE template0
    ENCODING 'UTF8';
