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
  // root_key 与 relative_path 是后端已校验的逻辑位置；不拼接或推断服务器路径。
  const logicalPath = [file.root_key, file.relative_path]
    .filter((value): value is string => Boolean(value))
    .join(' / ');

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
          {file.relevance_tier === 'POSSIBLE' ? (
            <span className="search-result-tier search-result-tier--possible">
              可能相关
            </span>
          ) : file.relevance_tier === 'SUPPORTED' ? (
            <span className="search-result-tier">已验证相关</span>
          ) : null}
        </div>
        {logicalPath ? (
          <span className="search-result-relative-path">位置：{logicalPath}</span>
        ) : null}
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
  const completeness = result.search_completeness;
  const completenessClassName = completeness
    ? `search-completeness search-completeness--${completeness.status.toLowerCase()}`
    : '';
  if (result.files.length === 0) {
    return (
      <section className="search-results-receipt">
        <div className="search-results-empty">
          {result.user_message ||
            '未找到相关文件。请尝试补充主题、年份、单位或文档类型。'}
        </div>
        {completeness ? (
          <p className={completenessClassName}>{completeness.message}</p>
        ) : null}
      </section>
    );
  }

  const showAllResults = Boolean(result.show_all_results);
  const supportedFiles = result.files.filter(
    (file) => file.relevance_tier !== 'POSSIBLE'
  );
  const possibleFiles = result.files.filter(
    (file) => file.relevance_tier === 'POSSIBLE'
  );
  const visibleFiles = showAllResults
    ? result.files
    : result.files.slice(0, visibleCount);
  const visibleSupportedFiles = visibleFiles.filter(
    (file) => file.relevance_tier !== 'POSSIBLE'
  );
  const visiblePossibleFiles = visibleFiles.filter(
    (file) => file.relevance_tier === 'POSSIBLE'
  );

  const renderFiles = (files: FileSearchResultFile[]) =>
    files.map((file) => (
      <SearchResultCard
        key={file.working_copy_id ?? `${file.document_id}-${file.document_version_id}`}
        file={file}
        attachment={
          attachments.find(
            (attachmentItem) => attachmentItem.document_id === file.document_id
          ) ?? null
        }
        onOpenAttachment={onOpenAttachment}
        onOpenDocument={onOpenDocument}
      />
    ));

  return (
    <section className="search-results-receipt">
      <header className="search-results-summary">
        <strong>
          {result.supported_count !== undefined || result.possible_count !== undefined
            ? `找到 ${result.supported_count ?? supportedFiles.length} 个已验证相关文件${
                (result.possible_count ?? possibleFiles.length) > 0
                  ? `，另有 ${result.possible_count ?? possibleFiles.length} 个可能相关文件`
                  : ''
              }`
            : `找到 ${result.total_returned} 个相关文件`}
        </strong>
      </header>
      {completeness ? (
        <p className={completenessClassName}>
          {completeness.message}
        </p>
      ) : null}

      {visibleSupportedFiles.length > 0 ? (
        <div className="search-results-list">
          {possibleFiles.length > 0 ? (
            <p className="search-results-group-title">已验证相关</p>
          ) : null}
          {renderFiles(visibleSupportedFiles)}
        </div>
      ) : null}
      {visiblePossibleFiles.length > 0 ? (
        <div className="search-results-list">
          <p className="search-results-group-title">可能相关</p>
          <p className="search-results-group-hint">
            这些文件只命中部分主题线索，尚未作为问题结论的依据。
          </p>
          {renderFiles(visiblePossibleFiles)}
        </div>
      ) : null}
      {!showAllResults && visibleCount < result.files.length ? (
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
