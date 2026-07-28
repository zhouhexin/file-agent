import { Check, Pencil, Tag, X } from 'lucide-react';
import { useEffect, useState } from 'react';

import { getClassificationTaxonomyOptions, submitClassificationFeedback } from '../../api/client';
import type { ClassificationTaxonomyOption, DocumentCategory } from '../../types';
import { formatConfidence } from './presentation';

type CategoryChipProps = {
  category: DocumentCategory;
  compact?: boolean;
  token?: string;
  agentRunId?: string;
  relationRole?: 'PRIMARY' | 'RELATED';
};

export function CategoryChip({
  category,
  compact = false,
  token,
  agentRunId,
  relationRole = 'RELATED',
}: CategoryChipProps) {
  // 分类展开区同时承载证据和明确反馈，沉默不会被后端当作正样本。
  const [expanded, setExpanded] = useState(false);
  const [feedbackState, setFeedbackState] = useState<'idle' | 'accepted' | 'rejected' | 'corrected'>('idle');
  const [submitting, setSubmitting] = useState(false);
  const [correctionVisible, setCorrectionVisible] = useState(false);
  const [correctionCategoryId, setCorrectionCategoryId] = useState('');
  const [taxonomyOptions, setTaxonomyOptions] = useState<ClassificationTaxonomyOption[]>([]);
  const [feedbackError, setFeedbackError] = useState('');
  const evidenceText = category.evidence.length > 0 ? category.evidence.join('、') : '暂无明确关键词依据';
  const feedbackEnabled = Boolean(token && category.suggestion_id);

  useEffect(() => {
    if (!token || !correctionVisible || taxonomyOptions.length > 0) return;
    let cancelled = false;
    getClassificationTaxonomyOptions(token)
      .then((result) => {
        if (!cancelled) setTaxonomyOptions(result.options);
      })
      .catch((error) => {
        if (!cancelled) {
          setFeedbackError(error instanceof Error ? error.message : '分类目录加载失败');
        }
      });
    return () => {
      cancelled = true;
    };
  }, [correctionVisible, taxonomyOptions.length, token]);

  const submitFeedback = async (
    action: 'ACCEPT' | 'REJECT' | 'CORRECT',
    correctedCategoryId?: string,
  ) => {
    if (!token || !category.suggestion_id || submitting) return;
    setSubmitting(true);
    setFeedbackError('');
    try {
      await submitClassificationFeedback(token, category.suggestion_id, {
        action,
        relation_role: relationRole,
        ...(agentRunId ? { agent_run_id: agentRunId } : {}),
        ...(correctedCategoryId ? { corrected_category_id: correctedCategoryId } : {}),
      });
      setFeedbackState(action === 'ACCEPT' ? 'accepted' : action === 'REJECT' ? 'rejected' : 'corrected');
      setCorrectionVisible(false);
    } catch (error) {
      setFeedbackError(error instanceof Error ? error.message : '反馈保存失败');
    } finally {
      setSubmitting(false);
    }
  };

  const submitCorrection = () => {
    if (!correctionCategoryId) {
      setFeedbackError('请选择更正后的分类');
      return;
    }
    void submitFeedback('CORRECT', correctionCategoryId);
  };

  return (
    <div className="category-chip-wrap">
      <button
        className={compact ? 'category-chip category-chip--compact' : 'category-chip'}
        type="button"
        aria-expanded={expanded}
        onClick={() => setExpanded((current) => !current)}
      >
        <Tag size={14} />
        <span>{category.name}</span>
        {!compact ? <em className="category-chip__confidence">{formatConfidence(category.confidence)}</em> : null}
      </button>
      {expanded ? (
        <div className="result-evidence">
          <p>证据关键词：{evidenceText}</p>
          <p>分类状态：{category.status || 'SUGGESTED'}</p>
          <p>分类来源：{category.source || 'rule'}</p>
          {feedbackEnabled ? (
            <div className="category-feedback">
              <div className="category-feedback__actions">
                <button type="button" disabled={submitting} onClick={() => void submitFeedback('ACCEPT')}>
                  <Check size={14} />正确
                </button>
                <button type="button" disabled={submitting} onClick={() => void submitFeedback('REJECT')}>
                  <X size={14} />错误
                </button>
                <button type="button" disabled={submitting} onClick={() => setCorrectionVisible((value) => !value)}>
                  <Pencil size={14} />更正
                </button>
              </div>
              {correctionVisible ? (
                <div className="category-feedback__correction">
                  <select
                    aria-label="更正后的分类路径"
                    value={correctionCategoryId}
                    onChange={(event) => setCorrectionCategoryId(event.target.value)}
                  >
                    <option value="">请选择分类</option>
                    {taxonomyOptions.map((option) => (
                      <option key={option.category_id} value={option.category_id}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                  <button type="button" disabled={submitting} onClick={submitCorrection}>提交</button>
                </div>
              ) : null}
              {feedbackState !== 'idle' ? (
                <p className="category-feedback__saved">
                  {feedbackState === 'rejected' ? '分类已拒绝，文件位置未变' : '分类已确认，文件位置未变'}
                </p>
              ) : null}
              {feedbackError ? <p className="category-feedback__error">{feedbackError}</p> : null}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
