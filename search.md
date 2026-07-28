#### 查找文件是否已同步到工作目录并且状态为active
```bash
SELECT
    wc.id AS working_copy_id,
    wcr.root_key,
    wc.filename,
    wc.relative_path,
    wc.status AS working_copy_status,
    wc.sync_status,
    wc.current_version_id,
    wc.updated_at,
    idx.status AS index_status,
    idx.chunk_count,
    idx.error_code,
    idx.error_message
FROM working_copies wc
JOIN working_copy_roots wcr ON wcr.id = wc.working_copy_root_id
LEFT JOIN LATERAL (
    SELECT status, chunk_count, error_code, error_message
    FROM document_index_runs
    WHERE document_version_id = wc.current_version_id
    ORDER BY updated_at DESC
    LIMIT 1
) idx ON TRUE
WHERE wc.filename ILIKE '%2025%工作总结%'
ORDER BY wc.updated_at DESC;
```