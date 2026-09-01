import { AlertCircle, CheckCircle2, FolderUp, Loader2, X } from 'lucide-react';

import type { UploadBatchProgressState } from './batchUpload';

type UploadBatchProgressProps = {
  batch: UploadBatchProgressState;
  onDismiss: () => void;
};

export function UploadBatchProgress({ batch, onDismiss }: UploadBatchProgressProps) {
  // 批次卡只聚合界面进度；每个成功文件仍由附件卡和后续 Agent 回执逐项展示。
  const percent = batch.total > 0
    ? Math.round(((batch.completed + batch.processed) / (batch.total * 2)) * 100)
    : 0;
  const active = ['uploading', 'waiting_review', 'submitting', 'submitted'].includes(batch.status);
  const statusText = batch.status === 'uploading'
    ? `正在上传 ${batch.completed}/${batch.total}`
    : batch.status === 'waiting_review'
      ? '正在查重并自动执行解析、分类和标准化命名'
      : batch.status === 'submitting'
        ? '正在生成逐文件树形回执'
        : batch.status === 'submitted'
          ? '自动整理已完成，正在汇总逐文件结果'
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
        <span>成功 {batch.succeeded}</span>
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
    </section>
  );
}
