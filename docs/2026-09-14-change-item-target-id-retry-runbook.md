# ChangeItem target_id 扩容与失败文件重试手册

## 1. 修复范围

本次修复把 `change_items.target_id` 从 `varchar(36)` 扩大为 `varchar(255)`。
`target_id` 是多态审计定位符，既可以是 UUID，也可以是 taxonomy 稳定 ID，
因此不能按 UUID 长度限制。迁移只扩容列，不改写现有值，不修改受管原件。

新迁移：

```text
20260914_0001_expand_change_item_target_id.py
```

## 2. 部署和迁移验证

按现有代码层发布流程更新服务。`deploy/update.ps1` 会重建 `migrate`
一次性容器并自动执行 `alembic upgrade head`。更新完成后执行：

```powershell
Set-Location E:\file-agent
$Compose = ".\deploy\docker-compose.production.yml"
$Env = ".\deploy\.env"
docker compose --env-file $Env -f $Compose exec -T api python -m alembic -c apps/api/alembic.ini current
```

期望当前迁移为：

```text
20260914_0001 (head)
```

再验证真实列长度：

```powershell
$ColumnCheckCode = @"
from sqlalchemy import inspect
from app.core.database import engine
column = next(item for item in inspect(engine).get_columns('change_items') if item['name'] == 'target_id')
print('change_items.target_id=', column['type'])
"@
docker compose --env-file $Env -f $Compose exec -T api python -c $ColumnCheckCode
```

期望输出包含：

```text
change_items.target_id= VARCHAR(255)
```

## 3. 预览可重试对象

先执行 dry-run。它只读数据库，不改变任务状态：

```powershell
docker compose --env-file $Env -f $Compose exec -T api python -m app.scripts.retry_failed_managed_jobs --root-key workdata
```

预览结果中：

- `candidate_count` 是可重试任务数。
- `SOURCE_MISSING` 表示源路径已不存在，脚本会跳过。
- `SOURCE_ANALYSIS_NOT_READY` 表示工作副本任务的源分析尚未就绪，
  脚本不会让物化 Worker 立即再次失败。

## 4. 显式重试并生成独立清单

确认 dry-run 后执行：

```powershell
$RetryOutput = docker compose --env-file $Env -f $Compose exec -T api python -m app.scripts.retry_failed_managed_jobs --root-key workdata --apply --priority 10
$RetryJson = $RetryOutput | Select-Object -Last 1
$RetryResult = $RetryJson | ConvertFrom-Json
$RetryResult | ConvertTo-Json -Depth 8
```

该命令会：

- 只重开 `workdata` 下失败的源分析、分类刷新和已就绪工作副本任务。
- 跳过明确不存在的源文件。
- 把尝试次数归零，优先级提升为 `10`。
- 为每个任务写入带 `retry_batch_id` 的 `filesystem_job_events`。
- 生成独立批次清单，不包含文件正文。

独立清单宿主机路径为：

```powershell
$ManifestName = Split-Path -Leaf $RetryResult.manifest_path
$ManifestPath = Join-Path "E:\file-agent\data\logs" $ManifestName
Get-Item -LiteralPath $ManifestPath
```

## 5. 查看这一批任务状态

```powershell
$RetryManifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding utf8 | ConvertFrom-Json
$JobIdsJson = ConvertTo-Json -InputObject @($RetryManifest.job_ids) -Compress
$RetryStatusCode = @"
import json
from collections import Counter
from app.core.database import SessionLocal
from app.db.models import FilesystemJob
job_ids = json.loads(r'''$JobIdsJson''')
with SessionLocal() as db:
    jobs = db.query(FilesystemJob).filter(FilesystemJob.id.in_(job_ids)).all()
    print('status_counts=', dict(Counter(job.status for job in jobs)))
    print('failed=', [{'job_id': job.id, 'job_type': job.job_type, 'error_message': job.error_message} for job in jobs if job.status == 'FAILED'])
"@
docker compose --env-file $Env -f $Compose exec -T api python -c $RetryStatusCode
```

`PENDING` 或 `RUNNING` 归零后，该批次已进入终态。

## 6. 从主 JSONL 中抽取该批次的独立日志

下面的命令只保留清单中 job_id 在本次重试开始后的日志：

```powershell
$RetryJobIds = @($RetryManifest.job_ids)
$RetryStartedAt = [DateTimeOffset]::Parse($RetryManifest.retry_started_at)
$DedicatedLogPath = Join-Path "E:\file-agent\data\logs" ("failed-managed-retry-" + $RetryManifest.retry_batch_id + ".log")
Get-ChildItem -LiteralPath "E:\file-agent\data\logs" -Filter "file-agent-*.log" -File |
Select-String -SimpleMatch -Pattern $RetryJobIds |
ForEach-Object {
$Line = $_.Line
try {
$Event = $Line | ConvertFrom-Json
if ($Event.ts -and [DateTimeOffset]::Parse($Event.ts) -ge $RetryStartedAt) {
$Line
}
} catch {
}
} | Set-Content -LiteralPath $DedicatedLogPath -Encoding utf8
Get-Item -LiteralPath $DedicatedLogPath
```

生成的两个独立文件分别是：

```text
failed-managed-retry-<retry_batch_id>.json
failed-managed-retry-<retry_batch_id>.log
```

JSON 保存重试文件、相对源路径和 job_id；LOG 只保存本批任务的运行日志。
