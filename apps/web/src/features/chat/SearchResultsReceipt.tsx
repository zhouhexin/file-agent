// 两阶段文件搜索结果卡片：逐文件展示文件名、分类、推荐原因和原文位置。
// 不展示 Skill、Tool、Chunk、SQL 分数或内部路径。
import { useState } from 'react';
import { FileText, MapPin } from 'lucide-react';

import type { ChatAttachment } from './presentation';
import type { FileSearchResult, FileSearchResultFile } from '../../types';

type SearchResultsReceiptProps = {
  result: FileSearchResult;
  attachments: ChatAttachment[];
  onOpenAttachment?: (file: ChatAttachment) => void;
  onOpenDocument?: (documentId: string, filename: string) => void;
};

function formatLocation(
  location: FileSearchResultFile['match_location']
): string {
  if (!location) return '';
  const parts: string[] = [];
  if (location.page_number) {
    parts.push(`第 ${location.page_number} 页`);
  }
  if (location.sheet_name) {
    let s = location.sheet_name;
    if (location.cell_range) s += ` ${location.cell_range}`;
    parts.push(s);
  }
  return parts.join(' · ');
}

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
  const yearLabel = file.year ? `（${file.year}）` : '';
  const locationLabel = formatLocation(file.match_location);

  return (
    <article className="search-result-card">
      <div className="search-result-header">
        <FileText size={18} aria-hidden />
        <span className="search-result-filename">
          {file.filename}
          {yearLabel}
        </span>
        <span className="search-result-category">{categoryLabel}</span>
      </div>

      {file.overview ? (
        <p className="search-result-overview">{file.overview}</p>
      ) : null}

      {file.match_reasons.length > 0 ? (
        <ul className="search-result-reasons">
          {file.match_reasons.map((reason, index) => (
            <li key={`reason-${index}`}>{reason}</li>
          ))}
        </ul>
      ) : null}

      {locationLabel ? (
        <div className="search-result-location">
          <MapPin size={14} aria-hidden />
          <span>{locationLabel}</span>
        </div>
      ) : null}

      {file.evidence_preview ? (
        <blockquote className="search-result-preview">
          {file.evidence_preview}
        </blockquote>
      ) : null}

      {((attachment && onOpenAttachment) || onOpenDocument) ? (
        <div className="search-result-actions">
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
        </div>
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
        {result.partial ? (
          <span className="search-results-partial">
            部分文件原文索引暂不可用
          </span>
        ) : null}
      </header>

      {result.user_message ? (
        <p className="search-results-message">{result.user_message}</p>
      ) : null}

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
          className="search-result-action"
          onClick={() => setVisibleCount((current) => current + 10)}
        >
          查看更多
        </button>
      ) : null}
    </section>
  );
}
