// 逐文件任务回执卡只展示用户可理解的整理与检索状态，不暴露内部 Agent、Skill、Tool 或索引载荷。
import type { DocumentResult } from '../../types';
import { CategoryChip } from './CategoryChip';
import { FileTypeIcon } from './FileTypeIcon';
import type { ChatAttachment } from './presentation';
import { formatFileSize, getFailureMessage } from './presentation';

type DocumentResultCardProps = {
  token?: string;
  agentRunId?: string;
  result: DocumentResult;
  index: number;
  attachment?: ChatAttachment;
  onOpenFile?: (file: ChatAttachment) => void;
  onOpenDocument?: (documentId: string, filename: string) => void;
  showIndex?: boolean;
  showPrimaryCategory?: boolean;
};

/** 展示一个文件的安全处理回执，并把文件打开动作交给受控上层回调。 */
export function DocumentResultCard({
  result,
  index,
  attachment,
  onOpenFile,
  onOpenDocument,
  token,
  agentRunId,
  showIndex = true,
  showPrimaryCategory = true,
}: DocumentResultCardProps) {
  // 每个文件单独成卡，避免把批量结果挤成一整段文本。
  const failed = result.extraction_status === 'FAILED';
  const filename = result.filename || attachment?.filename || result.document_id;
  const originalFilename = result.original_filename || attachment?.filename || filename;
  const renamedFilename = result.renamed_filename || filename;
  // 历史生命周期回执可能只记录处理状态和命名建议，没有分类数组。
  const categories = result.categories ?? [];
  const primaryCategory = categories[0];
  const canOpen = Boolean((attachment && onOpenFile) || onOpenDocument);
  const openFile = () => {
    // 已生成工作副本的结果必须走共享文件读取边界；上传附件接口只适用于原始上传文档。
    if (result.working_copy_id && onOpenDocument) {
      onOpenDocument(result.document_id, filename);
      return;
    }
    // 当前轮原始附件优先复用完整附件信息；历史回执只用稳定 document_id 重新鉴权预览。
    if (attachment && onOpenFile) {
      onOpenFile(attachment);
      return;
    }
    onOpenDocument?.(result.document_id, filename);
  };

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
                disabled={!canOpen}
                type="button"
                onClick={openFile}
                title={canOpen ? '预览文件' : undefined}
              >
                {showIndex ? `${index}. ` : ''}{filename}
              </button>
              <span className="document-result-size">
                {attachment ? formatFileSize(attachment.size_bytes) : '文件不可用'}
              </span>
            </div>
          </header>
          <div className="document-result-failure">
            <p>文件原名：{originalFilename}</p>
            <p>整理后名称：{renamedFilename}</p>
            <p>分类结果：未完成</p>
            <p>处理状态：失败</p>
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
                disabled={!canOpen}
                type="button"
                onClick={openFile}
                title={canOpen ? '预览文件' : undefined}
              >
                {showIndex ? `${index}. ` : ''}{filename}
              </button>
              <span className="document-result-size">
                {attachment
                  ? formatFileSize(attachment.size_bytes)
                  : typeof result.char_count === 'number'
                    ? `${result.char_count.toLocaleString()} 字符`
                    : '字符数未统计'}
              </span>
            </div>
            {showPrimaryCategory && primaryCategory ? (
              <div className="document-result-inline-category">
                <CategoryChip
                  category={primaryCategory}
                  compact
                  token={token}
                  agentRunId={agentRunId}
                  relationRole="PRIMARY"
                />
              </div>
            ) : null}
            {showPrimaryCategory && !primaryCategory ? (
              <span className="document-result-confidence">暂无可靠分类</span>
            ) : null}
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
          {categories.length > 1 ? (
            <div className="document-result-categories">
              <div className="category-chip-list">
                {categories.slice(1).map((category) => (
                  <CategoryChip
                    category={category}
                    key={category.suggestion_id || category.category_id || category.name}
                    compact
                    token={token}
                    agentRunId={agentRunId}
                    relationRole="RELATED"
                  />
                ))}
              </div>
            </div>
          ) : null}
          {result.original_filename || result.renamed_filename || result.rename_status ? (
            <div className="document-result-rename-suggestion">
              <p>文件原名：{originalFilename}</p>
              <p>重命名后：{renamedFilename}</p>
              <p>
                处理状态：{result.processing_status === 'NEEDS_REVIEW'
                  ? '待复核'
                  : result.processing_status === 'FAILED'
                    ? '失败'
                    : '已完成'}
              </p>
              <small>
                {result.rename_status === 'NEEDS_REVIEW'
                  ? '命名依据不足，已保留原名并等待复核。'
                  : result.rename_status === 'NO_CHANGE'
                    ? '标准名称与原名一致，无需改名。'
                    : '工作副本已完成标准化命名；不可变原件未修改。'}
              </small>
            </div>
          ) : null}
          {result.review_reasons && result.review_reasons.length > 0 ? (
            <div className="document-result-risk-warnings">
              {result.review_reasons.map((reason, reasonIndex) => (
                <p key={`${reason}-${reasonIndex}`}>待复核原因：{reason}</p>
              ))}
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
          {result.rename_suggestion?.proposed_filename ? (
            <div className="document-result-rename-suggestion">
              <p>建议名称：{result.rename_suggestion.proposed_filename}</p>
              <small>当前文件名未改变。如需改名，请在对话中明确提出“改名”。</small>
            </div>
          ) : null}
        </>
      )}
    </article>
  );
}
