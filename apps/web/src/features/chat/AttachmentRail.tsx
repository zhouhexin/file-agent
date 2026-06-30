import { useState } from 'react';
import { ChevronDown, ChevronUp } from 'lucide-react';
import { FileCard } from './FileCard';
import type { AttachmentListProps } from './presentation';

export function AttachmentRail({
  attachments,
  layout = 'rail',
  locked = false,
  onOpen,
  onRemove,
}: AttachmentListProps) {
  const [expanded, setExpanded] = useState(false);

  // 附件栏同时用于发送前草稿和历史消息；历史消息不提供删除入口。
  if (attachments.length === 0) {
    return null;
  }

  const visibleFiles = expanded ? attachments : attachments.slice(0, 2);
  const showExpandButton = attachments.length > 2;

  const gridClass = layout === 'rail' ? 'files-grid files-grid-2' : 'files-grid';

  return (
    <div className={`files-attachment-container files-attachment-container--${layout}`}>
      <div className={gridClass}>
        {visibleFiles.map((file) => (
          <FileCard
            key={file.document_id}
            file={file}
            onOpen={onOpen}
            onRemove={!locked ? onRemove : undefined}
            showStatus={!locked}
          />
        ))}
      </div>

      {showExpandButton && (
        <button
          onClick={() => setExpanded((v) => !v)}
          className="expand-button"
        >
          {expanded ? (
            <>
              <ChevronUp className="expand-icon" /> 收起
            </>
          ) : (
            <>
              <ChevronDown className="expand-icon" /> 查看全部 {attachments.length} 个文件
            </>
          )}
        </button>
      )}
    </div>
  );
}
