// 阶段五回答只展示最终结论、必要限制和可预览文件框，不展示 Tool、Chunk 或原文定位。
import { useEffect, useState } from 'react';
import { FileText, Tag } from 'lucide-react';

import {
  ApiError,
  getFileSearchClarification,
  resolveFileSearchClarification,
} from '../../api/client';
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
  const [currentStatus, setCurrentStatus] = useState('WAITING_SELECTION');
  const [error, setError] = useState('');

  useEffect(() => {
    let active = true;
    // 历史同名文件卡可能已在另一轮请求或标签页中处理；展示前读取持久化状态，
    // 避免让用户再次提交已经失效的 option_id。
    void getFileSearchClarification(token, result.clarification_id)
      .then((latest) => {
        if (active) {
          setCurrentStatus(latest.status);
        }
      })
      .catch(() => {
        // 状态刷新失败不阻断历史回执；提交时后端仍会再次校验权限和有效期。
      });
    return () => {
      active = false;
    };
  }, [result.clarification_id, token]);

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
      setCurrentStatus('RESOLVED');
      onResolved?.(response);
    } catch (err) {
      setError(formatError(err));
      if (err instanceof ApiError && err.status === 409) {
        // 409 表示卡片可能已被后续请求替代或已经处理；立即刷新状态并关闭旧入口，
        // 不能继续让重复点击制造无意义冲突。
        void getFileSearchClarification(token, result.clarification_id)
          .then((latest) => setCurrentStatus(latest.status))
          .catch(() => {
            // 保留后端原始业务错误；即使刷新失败也不把它误报成注册冲突。
          });
      }
    } finally {
      setSubmitting(false);
    }
  }

  if (currentStatus === 'RESOLVED') {
    return (
      <section className="file-search-clarification-card is-resolved">
        <FileText size={18} aria-hidden />
        <span>该文件选择已处理，后续结果会显示在新的消息回执中。</span>
      </section>
    );
  }

  if (currentStatus === 'SUPERSEDED' || currentStatus === 'EXPIRED') {
    return (
      <section className="file-search-clarification-card is-resolved">
        <FileText size={18} aria-hidden />
        <span>
          {currentStatus === 'EXPIRED'
            ? '该文件选择已过期，请重新描述需要读取的文件。'
            : '该文件选择已被后续请求替代，请使用最新的选择卡。'}
        </span>
      </section>
    );
  }

  return (
    <section className="evidence-answer-receipt">
      <div className="evidence-answer-text">{result.message}</div>
      <div className="search-results-list">
        {result.choices.map((file, index) => {
          // 历史消息选择卡没有分类与目录字段；缺失时仍应允许用户完成选择。
          const categoryLabels = Array.isArray(file.suggested_category_labels)
            ? file.suggested_category_labels
            : [];
          return (
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
                disabled={submitting || currentStatus !== 'WAITING_SELECTION'}
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
                <span className="file-selection-directory">
                  所在目录：{file.directory_path || '工作目录根目录'}
                </span>
                {categoryLabels.length > 0 ? (
                  <div className="search-result-tags" aria-label="建议分类">
                    {categoryLabels.map((label) => (
                      <span
                        className="category-chip category-chip--compact search-result-category-tag"
                        key={label}
                      >
                        <Tag size={13} aria-hidden />
                        <span>{label}</span>
                      </span>
                    ))}
                  </div>
                ) : (
                  <span className="file-selection-category-empty">建议分类：暂未生成</span>
                )}
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
          );
        })}
      </div>
      {error ? <div className="field-error">{error}</div> : null}
      <button
        type="button"
        className="search-results-more"
        disabled={
          submitting
          || currentStatus !== 'WAITING_SELECTION'
          || selectedOptionIds.length === 0
        }
        onClick={() => void submitSelection()}
      >
        {submitting ? '正在继续…' : '使用所选文件继续'}
      </button>
    </section>
  );
}
