// 文件搜索结果只展示普通用户需要的文件名、分类标签和打开入口。
// 推荐原因、摘要预览、索引降级、Skill、Tool、Chunk、SQL 分数和内部路径都不进入聊天页面。
import { useState } from 'react';
import { FileText, Tag } from 'lucide-react';

import type { ChatAttachment } from './presentation';
import type { FileSearchResult, FileSearchResultFile } from '../../types';

type SearchResultsReceiptProps = {
  result: FileSearchResult;
  attachments: ChatAttachment[];
  onOpenAttachment?: (file: ChatAttachment) => void;
  onOpenDocument?: (documentId: string, filename: string) => void;
};

function SearchResultCard({
  file,
  attachment,
  onOpenAttachment,
  onOpenDocument,
}: {
  file: FileSearchResultFile;
  attachment: ChatAttachment | null;
  onOpenAttachment?: (file: ChatAttachment) => void;
  onOpenDocument?: (documentId: string, filename: string) => void;
}) {
  const categoryLabel =
    file.category_path && file.category_path.length > 0
      ? file.category_path.join(' / ')
      : '未分类';

  return (
    <article className="search-result-card">
      <span className="search-result-icon">
        <FileText size={18} aria-hidden />
      </span>
      <div className="search-result-main">
        <span className="search-result-filename">
          {file.filename}
        </span>
        <div className="search-result-tags" aria-label="文件分类">
          <span className="category-chip category-chip--compact search-result-category-tag">
            <Tag size={13} aria-hidden />
            <span>{categoryLabel}</span>
          </span>
        </div>
      </div>

      {((attachment && onOpenAttachment) || onOpenDocument) ? (
        <button
          type="button"
          className="search-result-action"
          onClick={() => {
            // 本轮附件优先复用已有预览元数据；全局检索结果只传稳定 document_id。
            if (attachment && onOpenAttachment) {
              onOpenAttachment(attachment);
              return;
            }
            onOpenDocument?.(file.document_id, file.filename);
          }}
        >
          查看文件
        </button>
      ) : null}
    </article>
  );
}

export function SearchResultsReceipt({
  result,
  attachments,
  onOpenAttachment,
  onOpenDocument,
}: SearchResultsReceiptProps) {
  // 前端只控制展示批次；后端仍负责总数、权限和结果上限。
  const [visibleCount, setVisibleCount] = useState(10);
  if (result.files.length === 0) {
    return (
      <section className="search-results-receipt">
        <div className="search-results-empty">
          {result.user_message ||
            '未找到相关文件。请尝试补充主题、年份、单位或文档类型。'}
        </div>
      </section>
    );
  }

  const visibleFiles = result.files.slice(0, visibleCount);

  return (
    <section className="search-results-receipt">
      <header className="search-results-summary">
        <strong>找到 {result.total_returned} 个相关文件</strong>
      </header>

      <div className="search-results-list">
        {visibleFiles.map((file) => (
          <SearchResultCard
            key={file.document_id}
            file={file}
            attachment={
              attachments.find(
                (attachmentItem) =>
                  attachmentItem.document_id === file.document_id
              ) ?? null
            }
            onOpenAttachment={onOpenAttachment}
            onOpenDocument={onOpenDocument}
          />
        ))}
      </div>
      {visibleCount < result.files.length ? (
        <button
          type="button"
          className="search-results-more"
          onClick={() => setVisibleCount((current) => current + 10)}
        >
          查看更多
        </button>
      ) : null}
    </section>
  );
}
