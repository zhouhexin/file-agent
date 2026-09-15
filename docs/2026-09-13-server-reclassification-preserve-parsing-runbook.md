# 服务器重新分类操作手册（保留解析与索引）

适用目标：删除指定受管目录已生效的文件分类和工作副本落位结果，以当前分类规则重新分类；保留受管原始文件、源文件修订、原文解析页、OCR、摘要、Chunk、向量/索引和用户数据。

本操作是受控破坏性操作。它会删除指定根的工作副本物理文件、正式 `document_categories`、分类落位操作与路径记录、组织决策、旧分类运行/建议和相关待执行任务；不会删除受管原文件、已解析正文、`document_pages`、`document_chunks`、`document_index_runs` 或用户账户。

## 1. 前置条件

1. 已完成并验证 PostgreSQL SQL 备份和 `data/uploads` 文件备份。
2. 当前服务已部署包含 `reset_managed_root_working_copies.py` 的版本。
3. 在发布/重分类窗口内，不接受新的上传、导入、移动或分类请求。
4. 操作对象是唯一的受管根，不是整个 `data/uploads` 目录，更不能是外部原始受管目录。

## 2. 查询受管根键（只读）

在服务器 `E:\file-agent` 中执行：

```powershell
$Compose = ".\deploy\docker-compose.production.yml"
$Env = ".\deploy\.env"

docker compose --env-file $Env -f $Compose exec -T api python -c `
  "from app.core.database import SessionLocal; from app.db.models import ManagedRoot; db=SessionLocal(); print([(x.root_key, x.display_name, x.container_path, x.classification_mode, x.enabled, x.read_only) for x in db.query(ManagedRoot).order_by(ManagedRoot.root_key)]); db.close()"
```

记录需要重新分类的 `root_key`。下文以 `workdata` 为例；如输出不同，必须替换为实际根键，不能猜测。

## 3. 停止所有写入者

保持 PostgreSQL 和 Neo4j 运行，停止 API、调度器、观察器和全部 worker：

```powershell
Set-Location E:\file-agent
$Compose = ".\deploy\docker-compose.production.yml"
$Env = ".\deploy\.env"

docker compose --env-file $Env -f $Compose stop `
  api scheduler watcher `
  reconcile-scan-worker lifecycle-worker source-analysis-worker `
  structured-extraction-worker graph-worker
```

确认这些服务已经停止；不要在 API 仍可写入时执行重置。

```powershell
docker compose --env-file $Env -f $Compose ps
```

## 4. 受控重置并重新提交分类

以下命令会保留解析信息，但强制删除旧分类运行/建议，按当前 taxonomy、分类器和规则重新提交源侧分类任务。`--clear-related-active-jobs` 会移除该根原有的 PENDING/RUNNING 相关任务，避免旧规则任务和新规则任务竞争。

```powershell
$RootKey = "workdata"   # 必须替换为第 2 步实际查询到的根键

docker compose --env-file $Env -f $Compose run --rm --no-deps `
  --entrypoint python api `
  -m app.scripts.reset_managed_root_working_copies `
  --root-key $RootKey `
  --force-source-reclassification `
  --clear-related-active-jobs `
  --confirm-reset-working-copies `
  --confirm-writers-stopped
```

成功输出必须包含：

- `working_copies_removed` 与 `physical_files_removed`；
- `classification_refresh_job_ids`；
- `source_preserved` 与 `source_after_reset`，且两者相同；
- `force_source_reclassification: True`。

若输出显示 `source_preserved` 和 `source_after_reset` 不一致，脚本会拒绝提交；不要手工删表或删目录，保留现场排查。

## 5. 恢复服务并等待重新分类

```powershell
docker compose --env-file $Env -f $Compose up -d --no-build
docker compose --env-file $Env -f $Compose ps
docker compose --env-file $Env -f $Compose logs --tail=200 `
  source-analysis-worker lifecycle-worker reconcile-scan-worker
```

其中 `source-analysis-worker` 重新生成当前分类建议，`lifecycle-worker` 消费物化工作副本和落位任务。文件数量较大时，工作副本目录在分析完成前暂时为空是正常状态。

## 6. 完成验证

待队列趋于空闲后，至少检查：

```powershell
$CheckCode = @"
from collections import Counter
from app.core.database import SessionLocal
from app.db.models import ManagedRoot, ManagedFile, WorkingCopy, FilesystemJob
db = SessionLocal()
root = db.query(ManagedRoot).filter_by(root_key='$RootKey').one()
print('managed_files=', db.query(ManagedFile).filter_by(root_id=root.id).count())
print('working_copies=', db.query(WorkingCopy).join(ManagedFile, ManagedFile.id == WorkingCopy.managed_file_id).filter(ManagedFile.root_id == root.id).count())
print('jobs=', Counter((x.queue_name, x.status) for x in db.query(FilesystemJob).filter_by(root_id=root.id)))
db.close()
"@

docker compose --env-file $Env -f $Compose exec -T api python -c $CheckCode
```

还应抽样确认：原文预览仍可打开、分类变为当前规则结果、工作副本物理路径与数据库一致、没有旧分类任务失败或重复执行。

### 6.1 查看 SOURCE_ANALYSIS 失败原因（只读）

任务统计包含历史记录。使用以下命令查看指定根最新 50 个 `SOURCE_ANALYSIS` 失败任务的真实错误摘要、文件修订 ID、尝试次数和完成时间：

```powershell
$FailureReportCode = @"
import json
from app.core.database import SessionLocal
from app.db.models import ManagedRoot, FilesystemJob
db = SessionLocal()
root = db.query(ManagedRoot).filter_by(root_key='$RootKey').one()
jobs = db.query(FilesystemJob).filter(
    FilesystemJob.root_id == root.id,
    FilesystemJob.queue_name == 'SOURCE_ANALYSIS',
    FilesystemJob.status == 'FAILED',
).order_by(FilesystemJob.finished_at.desc()).limit(50).all()
for job in jobs:
    print(json.dumps({
        'job_id': job.id,
        'job_type': job.job_type,
        'finished_at': job.finished_at,
        'attempts': f'{job.attempt_count}/{job.max_attempts}',
        'revision_id': (job.payload_json or {}).get('managed_file_revision_id'),
        'error': job.error_message,
    }, ensure_ascii=False, default=str))
db.close()
"@

docker compose --env-file $Env -f $Compose exec -T api python -c $FailureReportCode
```

将其中一个 `job_id` 代入下列命令，可读取该任务完整事件链和服务器 JSONL 日志；两者均为只读：

```powershell
$JobId = "替换为失败任务 ID"

$JobEventCode = @"
import json
from app.core.database import SessionLocal
from app.db.models import FilesystemJob, FilesystemJobEvent
db = SessionLocal()
job = db.get(FilesystemJob, '$JobId')
print(json.dumps({'job': {'id': job.id, 'payload': job.payload_json, 'result': job.result_json, 'error': job.error_message}}, ensure_ascii=False, default=str))
for event in db.query(FilesystemJobEvent).filter_by(job_id='$JobId').order_by(FilesystemJobEvent.created_at):
    print(json.dumps({'at': event.created_at, 'level': event.level, 'message': event.message, 'details': event.details_json}, ensure_ascii=False, default=str))
db.close()
"@

docker compose --env-file $Env -f $Compose exec -T api python -c $JobEventCode
Select-String -Path "E:\file-agent\data\logs\file-agent-*.log" -Pattern $JobId
```

## 7. 不适用情形

若只调整了工作副本落位策略而没有修改 taxonomy/候选/规则，去掉 `--force-source-reclassification` 即可复用旧分类建议。但当前目标是按最新规则重新分类，必须保留该参数。

不要使用 `reset_file_data_preserve_users.py`；该脚本会删除解析、索引等整个文件域信息，不符合本手册“保留解析”的目标。
