import { Check, Pencil, Tag, X } from 'lucide-react';
import { useState } from 'react';

import { submitClassificationFeedback } from '../../api/client';
import type { DocumentCategory } from '../../types';
import { formatConfidence } from './presentation';

type CategoryChipProps = {
  category: DocumentCategory;
  compact?: boolean;
  token?: string;
};

export function CategoryChip({ category, compact = false, token }: CategoryChipProps) {
  // 分类展开区同时承载证据和明确反馈，沉默不会被后端当作正样本。
  const [expanded, setExpanded] = useState(false);
  const [feedbackState, setFeedbackState] = useState<'idle' | 'accepted' | 'rejected' | 'corrected'>('idle');
  const [submitting, setSubmitting] = useState(false);
  const [correctionVisible, setCorrectionVisible] = useState(false);
  const [correctionPath, setCorrectionPath] = useState('');
  const [feedbackError, setFeedbackError] = useState('');
  const evidenceText = category.evidence.length > 0 ? category.evidence.join('、') : '暂无明确关键词依据';
  const feedbackEnabled = Boolean(token && category.suggestion_id);

  const submitFeedback = async (
    action: 'ACCEPT' | 'REJECT' | 'CORRECT',
    correctedCategoryPath?: string[],
  ) => {
    if (!token || !category.suggestion_id || submitting) return;
    setSubmitting(true);
    setFeedbackError('');
    try {
      await submitClassificationFeedback(token, category.suggestion_id, {
        action,
        ...(correctedCategoryPath ? { corrected_category_path: correctedCategoryPath } : {}),
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
    // 路径使用“/”分隔，由后端校验是否属于当前 taxonomy。
    const path = correctionPath.split('/').map((item) => item.trim()).filter(Boolean);
    if (path.length === 0) {
      setFeedbackError('请输入完整分类路径');
      return;
    }
    void submitFeedback('CORRECT', path);
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
                  <input
                    aria-label="更正后的分类路径"
                    placeholder="学校/人事师资/考核聘任"
                    value={correctionPath}
                    onChange={(event) => setCorrectionPath(event.target.value)}
                  />
                  <button type="button" disabled={submitting} onClick={submitCorrection}>提交</button>
                </div>
              ) : null}
              {feedbackState !== 'idle' ? <p className="category-feedback__saved">反馈已记录</p> : null}
              {feedbackError ? <p className="category-feedback__error">{feedbackError}</p> : null}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
