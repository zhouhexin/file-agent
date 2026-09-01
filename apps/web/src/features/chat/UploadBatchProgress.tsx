import { AlertCircle, CheckCircle2, FolderUp, Loader2, X } from 'lucide-react';

import type { UploadBatchProgressState } from './batchUpload';
import { ClassificationTreeReceipt } from './ClassificationTreeReceipt';
import type { ChatAttachment } from './presentation';

type UploadBatchProgressProps = {
  batch: UploadBatchProgressState;
  token: string;
  onDismiss: () => void;
  onOpenAttachment: (file: ChatAttachment) => void;
  onOpenDocument: (documentId: string, filename: string) => void;
};

export function UploadBatchProgress({
  batch,
  token,
  onDismiss,
  onOpenAttachment,
  onOpenDocument,
}: UploadBatchProgressProps) {
  // 上传批次直接消费归档状态，不再依赖前端伪造聊天任务来取得树形回执。
  const percent = batch.total > 0
    ? Math.round(((batch.completed + batch.processed) / (batch.total * 2)) * 100)
    : 0;
  const active = ['uploading', 'processing'].includes(batch.status);
  const settledResults = batch.files
    .map((file) => file.result)
    .filter((result) => ['COMPLETED', 'NEEDS_REVIEW', 'FAILED'].includes(result.processing_status || ''));
  const attachments = batch.files.flatMap((file) => file.attachment ? [file.attachment] : []);
  const statusText = batch.status === 'uploading'
    ? `正在上传 ${batch.completed}/${batch.total}`
    : batch.status === 'processing'
      ? '正在查重并自动执行解析、分类和标准化命名'
      : batch.status === 'failed'
        ? '批次处理未全部完成'
        : '本批次自动整理完成';

  return (
    <section className="upload-batch-progress" aria-live="polite">
      <header>
        <span className="upload-batch-title">
          <FolderUp size={17} />
          <strong>{batch.mode === 'folder' ? batch.folderName : '批量文件上传'}</strong>
        </span>
        {!active ? (
          <button type="button" onClick={onDismiss} aria-label="关闭批量上传进度">
            <X size={15} />
          </button>
        ) : null}
      </header>
      <div className="upload-batch-summary">
        {active ? <Loader2 className="upload-batch-spinner" size={15} /> : <CheckCircle2 size={15} />}
        <span>{statusText}</span>
        <span>已处理 {batch.processed}/{batch.total}</span>
        <span>完成 {batch.succeeded}</span>
        <span>待复核 {batch.needsReview}</span>
        <span>失败 {batch.failed}</span>
      </div>
      <div className="upload-batch-track" aria-label={`批次完成进度 ${percent}%`}>
        <span style={{ width: `${percent}%` }} />
      </div>
      {batch.failures.length > 0 ? (
        <details className="upload-batch-failures">
          <summary><AlertCircle size={14} /> 查看 {batch.failures.length} 个失败文件</summary>
          <ul>
            {batch.failures.map((failure) => (
              <li key={`${failure.relativePath}-${failure.message}`}>
                <strong>{failure.relativePath}</strong>
                <span>{failure.message}</span>
              </li>
            ))}
          </ul>
        </details>
      ) : null}
      {settledResults.length > 0 ? (
        <ClassificationTreeReceipt
          attachments={attachments}
          results={settledResults}
          token={token}
          onOpenAttachment={onOpenAttachment}
          onOpenDocument={onOpenDocument}
        />
      ) : null}
    </section>
  );
}
