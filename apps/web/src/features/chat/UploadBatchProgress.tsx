import { AlertCircle, CheckCircle2, FolderUp, Loader2 } from 'lucide-react';

import type { UploadBatchProgressState } from './batchUpload';
import { ClassificationTreeReceipt } from './ClassificationTreeReceipt';
import type { ChatAttachment } from './presentation';

type UploadBatchProgressProps = {
  batch: UploadBatchProgressState;
  token: string;
  onOpenAttachment: (file: ChatAttachment) => void;
  onOpenDocument: (documentId: string, filename: string) => void;
};

export function UploadBatchProgress({
  batch,
  token,
  onOpenAttachment,
  onOpenDocument,
}: UploadBatchProgressProps) {
  // 上传批次是智能体的正式回答，直接消费归档状态并永久保留在当前页面主体中。
  const percent = batch.total <= 0
    ? 0
    : batch.status === 'uploading' || batch.status === 'staged'
      ? Math.round((batch.completed / batch.total) * 100)
      : Math.round(((batch.completed + batch.processed) / (batch.total * 2)) * 100);
  const active = ['uploading', 'processing'].includes(batch.status);
  const settledResults = batch.files
    .map((file) => file.result)
    .filter((result) => ['COMPLETED', 'NEEDS_REVIEW', 'FAILED'].includes(result.processing_status || ''));
  const attachments = batch.files.flatMap((file) => file.attachment ? [file.attachment] : []);
  const statusText = batch.status === 'uploading'
    ? `正在上传 ${batch.completed}/${batch.total}`
    : batch.status === 'staged'
      ? `已暂存 ${batch.completed} 个文件，点击发送后开始处理`
    : batch.status === 'processing'
      ? '正在查重并自动执行解析、分类和标准化命名'
      : batch.status === 'failed'
        ? '批次处理未全部完成'
        : '本批次自动整理完成';
  const completed = batch.status === 'completed' || batch.status === 'failed';

  return (
    <section className="upload-batch-progress workbuddy-upload-receipt" aria-live="polite">
      <header className="assistant-identity">
        <span className="assistant-identity-avatar"><FolderUp size={14} aria-hidden /></span>
        <strong>File Agent</strong>
        <span className={`assistant-identity-status assistant-identity-status--${active ? 'processing' : batch.status === 'failed' ? 'failed' : 'completed'}`}>
          {active ? '处理中' : batch.status === 'failed' ? '处理失败' : '已完成'}
        </span>
      </header>
      <p className="upload-batch-intro">
        {batch.status === 'failed'
          ? '文件整理未全部完成，下面列出已处理文件和失败项：'
          : completed
          ? '文件已完成读取、分类和首次标准化命名，我按分类与命名结果整理如下：'
          : statusText}
      </p>
      <div className="upload-batch-rule" />
      <section className="upload-batch-result-block">
        <h2><span className="upload-batch-result-icon">🗂️</span>文件整理 / 分类树 <small>{batch.total} 个文件</small></h2>
        <div className="upload-batch-summary">
          {active ? <Loader2 className="upload-batch-spinner" size={15} /> : <CheckCircle2 size={15} />}
          <span>已处理 {batch.processed}/{batch.total}</span>
          <span>完成 {batch.succeeded}</span>
          <span>待复核 {batch.needsReview}</span>
          <span>失败 {batch.failed}</span>
        </div>
        {active ? (
          <div className="upload-batch-track" aria-label={`批次完成进度 ${percent}%`}>
            <span style={{ width: `${percent}%` }} />
          </div>
        ) : null}
      </section>
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
      {completed ? (
        <p className="upload-batch-change-note">
          工作副本已完成整理；上传原件保持不变。命名依据不足的文件直接保留原名。
        </p>
      ) : null}
    </section>
  );
}
