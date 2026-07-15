import type { DocumentResult } from '../../types';
import { CategoryChip } from './CategoryChip';
import { FileTypeIcon } from './FileTypeIcon';
import type { ChatAttachment } from './presentation';
import { formatFileSize, getFailureMessage } from './presentation';

type DocumentResultCardProps = {
  token?: string;
  result: DocumentResult;
  index: number;
  attachment?: ChatAttachment;
  onOpenFile?: (file: ChatAttachment) => void;
};

export function DocumentResultCard({
  result,
  index,
  attachment,
  onOpenFile,
  token,
}: DocumentResultCardProps) {
  // 每个文件单独成卡，避免把批量结果挤成一整段文本。
  const failed = result.extraction_status === 'FAILED';
  const filename = result.filename || attachment?.filename || result.document_id;
  const primaryCategory = result.categories[0];

  return (
    <article className={failed ? 'document-result-card document-result-card--failed' : 'document-result-card'}>
      {failed ? (
        <>
          <header className="document-result-header">
            <span className="file-type-icon">
              <FileTypeIcon contentType={attachment?.content_type} filename={filename} />
            </span>
            <div className="document-result-main">
              <button
                className="document-result-title"
                disabled={!attachment || !onOpenFile}
                type="button"
                onClick={() => attachment && onOpenFile?.(attachment)}
                title={attachment ? '打开附件' : undefined}
              >
                {index}. {filename}
              </button>
              <span className="document-result-size">
                {attachment ? formatFileSize(attachment.size_bytes) : '文件不可用'}
              </span>
            </div>
            <span className="document-result-status document-result-status--failed">失败</span>
          </header>
          <div className="document-result-failure">
            <p>失败原因：{getFailureMessage(result.errors)}</p>
          </div>
        </>
      ) : (
        <>
          <header className="document-result-header">
            <span className="file-type-icon">
              <FileTypeIcon contentType={attachment?.content_type} filename={filename} />
            </span>
            <div className="document-result-main">
              <button
                className="document-result-title"
                disabled={!attachment || !onOpenFile}
                type="button"
                onClick={() => attachment && onOpenFile?.(attachment)}
                title={attachment ? '打开附件' : undefined}
              >
                {index}. {filename}
              </button>
              <span className="document-result-size">
                {attachment ? formatFileSize(attachment.size_bytes) : `${result.char_count.toLocaleString()} 字符`}
              </span>
            </div>
            {primaryCategory ? (
              <div className="document-result-inline-category">
                <CategoryChip category={primaryCategory} compact token={token} />
              </div>
            ) : null}
            <span className="document-result-confidence">
              {primaryCategory ? `置信度 ${primaryCategory.confidence.toFixed(2)}` : '未分类'}
            </span>
            <span className="document-result-status">成功</span>
          </header>
          {result.categories.length > 1 ? (
            <div className="document-result-categories">
              <div className="category-chip-list">
                {result.categories.slice(1).map((category) => (
                  <CategoryChip
                    category={category}
                    key={`${category.name}-${category.confidence}`}
                    compact
                    token={token}
                  />
                ))}
              </div>
            </div>
          ) : null}
        </>
      )}
    </article>
  );
}
