// 阶段五回答只展示最终结论、必要限制和可预览文件框，不展示 Tool、Chunk 或原文定位。
import { useState } from 'react';
import { FileText, Tag } from 'lucide-react';

import { resolveFileSearchClarification } from '../../api/client';
import { formatError } from '../../api/errors';
import type {
  EvidenceAnswerResult,
  FileSelectionResult,
  SendMessageResponse,
} from '../../types';
import { formatFileSize } from './presentation';

type EvidenceAnswerReceiptProps = {
  result: EvidenceAnswerResult;
  onOpenDocument?: (documentId: string, filename: string) => void;
};

export function EvidenceAnswerReceipt({
  result,
  onOpenDocument,
}: EvidenceAnswerReceiptProps) {
  return (
    <section className="evidence-answer-receipt">
      {/* 回答正文不显示 [1] 等内部引用索引；可追溯文件统一由下方文件卡承载。 */}
      <div className="evidence-answer-text">{result.answer}</div>
      {result.limitations.length > 0 ? (
        <div className="evidence-answer-limitations">
          {result.limitations.map((value) => (
            <p key={value}>{value}</p>
          ))}
        </div>
      ) : null}
      <div className="search-results-list">
        {result.files.map((file) => (
          <article className="search-result-card" key={file.document_id}>
            <span className="search-result-icon">
              <FileText size={18} aria-hidden />
            </span>
            <div className="search-result-main">
              <span className="search-result-filename">{file.filename}</span>
              {file.availability !== 'AVAILABLE' ? (
                <span className="evidence-file-unavailable">
                  {file.availability === 'TRASHED'
                    ? '文件已删除，可通过对话恢复后查看'
                    : file.availability_message || '文件当前不可用'}
                </span>
              ) : null}
              {file.category_labels.length > 0 ? (
                <div className="search-result-tags" aria-label="文件分类">
                  {file.category_labels.map((label) => (
                    <span
                      className="category-chip category-chip--compact search-result-category-tag"
                      key={label}
                    >
                      <Tag size={13} aria-hidden />
                      <span>{label}</span>
                    </span>
                  ))}
                </div>
              ) : null}
            </div>
            {onOpenDocument && file.can_open !== false ? (
              <button
                type="button"
                className="search-result-action"
                onClick={() => onOpenDocument(file.document_id, file.filename)}
              >
                查看文件
              </button>
            ) : (
              <span className="search-result-action is-disabled">
                {file.availability === 'TRASHED' ? '文件已删除' : '暂不可查看'}
              </span>
            )}
          </article>
        ))}
      </div>
    </section>
  );
}


export function FileSelectionReceipt({
  result,
  token,
  onOpenDocument,
  onResolved,
}: {
  result: FileSelectionResult;
  token: string;
  onOpenDocument?: (documentId: string, filename: string) => void;
  onResolved?: (response: SendMessageResponse) => void;
}) {
  const [selectedOptionIds, setSelectedOptionIds] = useState<string[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');

  async function submitSelection() {
    if (selectedOptionIds.length === 0) {
      setError('请至少选择一份文件。');
      return;
    }
    setSubmitting(true);
    setError('');
    try {
      const response = await resolveFileSearchClarification(
        token,
        result.clarification_id,
        { option_ids: selectedOptionIds, custom_phrase: null },
      );
      onResolved?.(response);
    } catch (err) {
      setError(formatError(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section className="evidence-answer-receipt">
      <div className="evidence-answer-text">{result.message}</div>
      <div className="search-results-list">
        {result.choices.map((file, index) => (
          <article
            className={
              selectedOptionIds.includes(file.option_id)
                ? 'search-result-card is-selected'
                : 'search-result-card'
            }
            key={`${file.working_copy_id}-${index}`}
          >
            <input
              checked={selectedOptionIds.includes(file.option_id)}
              disabled={submitting}
              name={`file-selection-${result.clarification_id}`}
              onChange={() => setSelectedOptionIds((current) => (
                current.includes(file.option_id)
                  ? current.filter((item) => item !== file.option_id)
                  : [...current, file.option_id]
              ))}
              type="checkbox"
            />
            <span className="search-result-icon">
              <FileText size={18} aria-hidden />
            </span>
            <div className="search-result-main">
              <span className="search-result-filename">
                {index + 1}. {file.filename}
              </span>
              <span className="evidence-file-unavailable">
                {formatFileSize(file.size_bytes)} · 创建于{' '}
                {new Date(file.created_at).toLocaleString('zh-CN')}
              </span>
            </div>
            {onOpenDocument ? (
              <button
                type="button"
                className="search-result-action"
                onClick={() => onOpenDocument(file.document_id, file.filename)}
              >
                查看文件
              </button>
            ) : null}
          </article>
        ))}
      </div>
      {error ? <div className="field-error">{error}</div> : null}
      <button
        type="button"
        className="search-results-more"
        disabled={submitting || selectedOptionIds.length === 0}
        onClick={() => void submitSelection()}
      >
        {submitting ? '正在继续…' : '使用所选文件继续'}
      </button>
    </section>
  );
}
