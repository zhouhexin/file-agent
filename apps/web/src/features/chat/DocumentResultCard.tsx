// 逐文件任务回执卡只展示用户可理解的整理与检索状态，不暴露内部 Agent、Skill、Tool 或索引载荷。
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

/** 展示一个文件的安全处理回执，并把文件打开动作交给受控上层回调。 */
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
            {result.search_status ? (
              <span
                className="document-result-confidence"
                title={result.evidence_count ? `已建立 ${result.evidence_count} 条可定位证据` : undefined}
              >
                {/* 普通用户只看“是否可检索”，不展示 Chunk、Tool 或 embedding 等内部实现。 */}
                {result.search_status === 'READY' ? '可对话检索' : '检索内容待处理'}
              </span>
            ) : null}
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
          {result.risk_warnings && result.risk_warnings.length > 0 ? (
            <div className="document-result-risk-warnings">
              {result.risk_warnings.map((warning, warningIndex) => (
                <p key={`${warning.code || 'risk'}-${warningIndex}`}>
                  {warning.message || '文件存在需要注意的格式风险。'}
                </p>
              ))}
            </div>
          ) : null}
        </>
      )}
    </article>
  );
}
