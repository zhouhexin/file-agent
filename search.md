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
受管原始目录中仍为 ACTIVE，但共享工作目录尚无对应主工作副本
```aiignore
WITH shared_workspace AS (
    SELECT id
    FROM workspaces
    WHERE system_key = 'shared-working-directory'
    LIMIT 1
)
SELECT
    mr.root_key,
    mr.display_name AS root_name,
    mf.id AS managed_file_id,
    mf.relative_path AS source_relative_path,
    mf.filename,
    mf.size_bytes,
    mf.modified_at,
    mf.content_sha256,
    wcr.id AS working_copy_root_id,
    wc.id AS working_copy_id,
    wc.status AS working_copy_status,
    wc.sync_status,
    CASE
        WHEN wcr.id IS NULL THEN 'WORKING_COPY_ROOT_MISSING'
        WHEN wc.id IS NULL THEN 'WORKING_COPY_MISSING'
        WHEN wc.status <> 'ACTIVE' THEN 'WORKING_COPY_NOT_ACTIVE'
        ELSE 'UNKNOWN'
    END AS sync_problem
FROM managed_files mf
JOIN managed_roots mr
    ON mr.id = mf.root_id
CROSS JOIN shared_workspace sw
LEFT JOIN working_copy_roots wcr
    ON wcr.managed_root_id = mr.id
   AND wcr.workspace_id = sw.id
LEFT JOIN working_copies wc
    ON wc.working_copy_root_id = wcr.id
   AND wc.managed_file_id = mf.id
   AND wc.is_primary_import = TRUE
WHERE mf.status = 'ACTIVE'
  AND (
      wcr.id IS NULL
      OR wc.id IS NULL
      OR wc.status <> 'ACTIVE'
  )
ORDER BY mr.root_key, mf.relative_path;
```

完全没有工作副本记录
```aiignore
WITH shared_workspace AS (
    SELECT id
    FROM workspaces
    WHERE system_key = 'shared-working-directory'
    LIMIT 1
)
SELECT
    mr.root_key,
    mr.display_name AS root_name,
    mf.id AS managed_file_id,
    mf.relative_path AS source_relative_path,
    mf.filename,
    mf.size_bytes,
    mf.modified_at,
    mf.content_sha256,
    wcr.id AS working_copy_root_id,
    wc.id AS working_copy_id,
    wc.status AS working_copy_status,
    wc.sync_status,
    CASE
        WHEN wcr.id IS NULL THEN 'WORKING_COPY_ROOT_MISSING'
        WHEN wc.id IS NULL THEN 'WORKING_COPY_MISSING'
        WHEN wc.status <> 'ACTIVE' THEN 'WORKING_COPY_NOT_ACTIVE'
        ELSE 'UNKNOWN'
    END AS sync_problem
FROM managed_files mf
JOIN managed_roots mr
    ON mr.id = mf.root_id
CROSS JOIN shared_workspace sw
LEFT JOIN working_copy_roots wcr
    ON wcr.managed_root_id = mr.id
   AND wcr.workspace_id = sw.id
LEFT JOIN working_copies wc
    ON wc.working_copy_root_id = wcr.id
   AND wc.managed_file_id = mf.id
   AND wc.is_primary_import = TRUE
WHERE mf.status = 'ACTIVE'
  AND (
      wcr.id IS NULL
      OR wc.id IS NULL
      OR wc.status <> 'ACTIVE'
  )
ORDER BY mr.root_key, mf.relative_path;

```