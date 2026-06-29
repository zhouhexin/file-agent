import { Trash2 } from 'lucide-react';

import { FileTypeIcon } from './FileTypeIcon';
import type { AttachmentListProps } from './presentation';
import { formatFileSize, formatUploadStatus } from './presentation';

export function AttachmentRail({
  attachments,
  layout = 'rail',
  locked = false,
  onOpen,
  onRemove,
}: AttachmentListProps) {
  // 附件栏同时用于发送前草稿和历史消息；历史消息不提供删除入口。
  if (attachments.length === 0) {
    return null;
  }

  return (
    <div className={layout === 'rail' ? 'attachment-rail' : 'attachment-stack'}>
      {attachments.map((file) => (
        <div className="attachment-rail-card" key={file.document_id}>
          <button
            className="attachment-open-button"
            disabled={!onOpen}
            type="button"
            onClick={() => onOpen?.(file)}
            title="打开附件"
          >
            {file.preview_url ? (
              <img alt={file.filename} className="attachment-preview" src={file.preview_url} />
            ) : (
              <span className="file-type-icon">
                <FileTypeIcon contentType={file.content_type} filename={file.filename} />
              </span>
            )}
            <span className="attachment-card-text">
              <strong>{file.filename}</strong>
              <span>{formatFileSize(file.size_bytes)} · {locked ? '已进入对话' : formatUploadStatus(file)}</span>
            </span>
          </button>
          {!locked && onRemove ? (
            <button
              className="icon-button"
              disabled={file.deleting}
              type="button"
              onClick={() => onRemove(file.document_id)}
              title="删除文件"
            >
              <Trash2 size={16} />
            </button>
          ) : null}
        </div>
      ))}
    </div>
  );
}
