import type { DocumentResult } from '../../types';
import { CategoryChip } from './CategoryChip';
import { FileTypeIcon } from './FileTypeIcon';
import type { ChatAttachment } from './presentation';
import { formatFileSize, getFailureMessage } from './presentation';

type DocumentResultCardProps = {
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
}: DocumentResultCardProps) {
  // 每个文件单独成卡，避免把批量结果挤成一整段文本。
  const failed = result.extraction_status === 'FAILED';
  const filename = result.filename || attachment?.filename || result.document_id;

  return (
    <article className={failed ? 'document-result-card document-result-card--failed' : 'document-result-card'}>
      <header className="document-result-header">
        <span className="file-type-icon">
          <FileTypeIcon contentType={attachment?.content_type} filename={filename} />
        </span>
        <button
          className="document-result-title"
          disabled={!attachment || !onOpenFile}
          type="button"
          onClick={() => attachment && onOpenFile?.(attachment)}
          title={attachment ? '打开附件' : undefined}
        >
          {index}. {filename}
        </button>
      </header>

      {failed ? (
        <div className="document-result-failure">
          <strong>解析失败</strong>
          <p>失败原因：{getFailureMessage(result.errors)}</p>
          <p>建议：检查文件是否损坏、图片是否清晰，之后重新解析。</p>
        </div>
      ) : (
        <>
          <p className="document-result-meta">
            解析成功 · {result.page_count} 页/Sheet · {result.char_count.toLocaleString()} 字符
            {attachment ? ` · ${formatFileSize(attachment.size_bytes)}` : ''}
          </p>
          <div className="document-result-categories">
            <span>分类建议：</span>
            <div className="category-chip-list">
              {result.categories.map((category) => (
                <CategoryChip category={category} key={`${category.name}-${category.confidence}`} />
              ))}
            </div>
          </div>
        </>
      )}
    </article>
  );
}
