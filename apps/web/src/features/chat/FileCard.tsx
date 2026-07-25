import { CheckCircle2, File, FileSpreadsheet, FileText, Loader2, RotateCcw, X } from 'lucide-react';
import { formatFileSize } from './presentation';
import type { ChatAttachment } from './presentation';

type FileCardProps = {
  file: ChatAttachment;
  onOpen?: (file: ChatAttachment) => void;
  onRemove?: (documentId: string) => void;
  onRestore?: (file: ChatAttachment) => void;
  showStatus?: boolean;
};

export function FileCard({
  file,
  onOpen,
  onRemove,
  onRestore,
  showStatus = true,
}: FileCardProps) {
  const trashed = file.file_availability === 'TRASHED';
  const missing = file.status === 'MISSING' || file.file_availability === 'MISSING';
  const availabilityProcessing = file.file_availability === 'PROCESSING';
  const currentStateUnavailable = Boolean(
    file.file_availability && file.file_availability !== 'AVAILABLE',
  );
  const canOpen = file.can_open ?? !currentStateUnavailable;
  const canRestore = Boolean(trashed && file.can_restore && onRestore);
  const waitingForDuplicateDecision = file.duplicate_review_status === 'WAITING_CONFIRMATION';
  const lifecyclePending = Boolean(
    file.upload_document_version_id
      && !file.working_copy_id
      && !['EXISTING_FILE_SELECTED', 'CANCELLED'].includes(file.archive_status ?? ''),
  );
  const getFileType = () => {
    const name = file.filename.toLowerCase();
    if (name.endsWith('.docx') || name.endsWith('.doc')) return 'docx';
    if (name.endsWith('.pdf')) return 'pdf';
    if (name.endsWith('.xlsx') || name.endsWith('.xls')) return 'xlsx';
    return 'other';
  };

  const fileType = getFileType();

  const unavailable = missing || trashed || availabilityProcessing;
  const cardClass = unavailable
    ? `file-card file-card-${fileType} file-card-unavailable`
    : `file-card file-card-${fileType}`;
  const statusClass = file.deleting ? 'file-card-status file-card-status-loading' : 'file-card-status file-card-status-done';

  const FileIconComponent = () => {
    if (fileType === 'docx') return <FileText className="file-card-icon file-card-icon-docx" />;
    if (fileType === 'pdf') return <File className="file-card-icon file-card-icon-pdf" />;
    if (fileType === 'xlsx') return <FileSpreadsheet className="file-card-icon file-card-icon-xlsx" />;
    return <File className="file-card-icon file-card-icon-other" />;
  };

  return (
    <button
      type="button"
      className={cardClass}
      onClick={() => {
        if (canOpen) onOpen?.(file);
      }}
      disabled={(!canOpen && !canRestore && !onRemove) || (!onOpen && !onRemove && !canRestore)}
      title={file.availability_message || (missing ? '文件已不存在，无法打开附件' : file.filename)}
    >
      <FileIconComponent />
      <div className="file-card-text">
        <p className="file-card-filename">
          {file.filename}
        </p>
        <p className="file-card-size">
          {missing
            ? (file.availability_message || '文件不存在')
            : trashed
              ? '已删除（在回收站，可恢复）'
              : availabilityProcessing
                ? (file.availability_message || '文件正在后台处理')
            : waitingForDuplicateDecision
              ? '等待重复文件确认'
              : lifecyclePending
                ? `${formatFileSize(file.size_bytes)} · 后台处理中`
                : formatFileSize(file.size_bytes)}
        </p>
      </div>
      {showStatus && (
        file.deleting || lifecyclePending ? (
          <Loader2 className={statusClass} />
        ) : (
          <CheckCircle2 className={statusClass} />
        )
      )}
      {onRemove ? (
        <span
          className="file-card-remove"
          onClick={(event) => {
            // 删除草稿附件时阻止触发文件预览。
            event.stopPropagation();
            onRemove(file.document_id);
          }}
          role="button"
          tabIndex={0}
          title="移除附件"
          onKeyDown={(event) => {
            if (event.key === 'Enter' || event.key === ' ') {
              event.preventDefault();
              event.stopPropagation();
              onRemove(file.document_id);
            }
          }}
        >
          <X size={14} />
        </span>
      ) : null}
      {canRestore ? (
        <span
          className="file-card-restore"
          onClick={(event) => {
            // 历史附件恢复必须重新进入对话和 OperationPlan，不能由卡片直接移动文件。
            event.stopPropagation();
            onRestore?.(file);
          }}
          role="button"
          tabIndex={0}
          title="恢复文件"
          onKeyDown={(event) => {
            if (event.key === 'Enter' || event.key === ' ') {
              event.preventDefault();
              event.stopPropagation();
              onRestore?.(file);
            }
          }}
        >
          <RotateCcw size={14} />
          恢复
        </span>
      ) : null}
    </button>
  );
}
