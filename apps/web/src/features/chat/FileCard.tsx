import { CheckCircle2, File, FileSpreadsheet, FileText, Loader2, X } from 'lucide-react';
import { formatFileSize } from './presentation';
import type { ChatAttachment } from './presentation';

type FileCardProps = {
  file: ChatAttachment;
  onOpen?: (file: ChatAttachment) => void;
  onRemove?: (documentId: string) => void;
  showStatus?: boolean;
};

export function FileCard({ file, onOpen, onRemove, showStatus = true }: FileCardProps) {
  const missing = file.status === 'MISSING';
  const getFileType = () => {
    const name = file.filename.toLowerCase();
    if (name.endsWith('.docx') || name.endsWith('.doc')) return 'docx';
    if (name.endsWith('.pdf')) return 'pdf';
    if (name.endsWith('.xlsx') || name.endsWith('.xls')) return 'xlsx';
    return 'other';
  };

  const fileType = getFileType();

  const cardClass = missing ? `file-card file-card-${fileType} file-card-missing` : `file-card file-card-${fileType}`;
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
      onClick={() => onOpen?.(file)}
      disabled={missing || (!onOpen && !onRemove)}
      title={missing ? '原始文件已不存在，无法打开附件' : file.filename}
    >
      <FileIconComponent />
      <div className="file-card-text">
        <p className="file-card-filename">
          {file.filename}
        </p>
        <p className="file-card-size">
          {missing ? '文件不存在' : formatFileSize(file.size_bytes)}
        </p>
      </div>
      {showStatus && (
        file.deleting ? (
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
    </button>
  );
}
