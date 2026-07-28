// 分类歧义卡只展示文件名和分类标签，并只提交后端签发的选项 ID。
import { CheckCircle2, Tags } from 'lucide-react';
import { useEffect, useState } from 'react';

import {
  getClassificationClarification,
  resolveClassificationClarification,
} from '../../api/client';
import { formatError } from '../../api/errors';
import type { ClassificationClarificationResult } from '../../types';

type ClassificationClarificationCardProps = {
  token: string;
  result: ClassificationClarificationResult;
};

export function ClassificationClarificationCard({
  token,
  result,
}: ClassificationClarificationCardProps) {
  const [status, setStatus] = useState(result.status);
  const [selectedOptionId, setSelectedOptionId] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    let active = true;
    void getClassificationClarification(token, result.id)
      .then((latest) => {
        if (active) setStatus(latest.status);
      })
      .catch(() => {
        // 历史卡状态刷新失败时仍可展示原始选择，提交时由后端再次校验。
      });
    return () => {
      active = false;
    };
  }, [result.id, token]);

  async function submitSelection() {
    if (!selectedOptionId) {
      setError('请先选择一个文件分类。');
      return;
    }
    setSubmitting(true);
    setError('');
    try {
      await resolveClassificationClarification(token, result.id, selectedOptionId);
      setStatus('RESOLVED');
    } catch (err) {
      setError(formatError(err));
    } finally {
      setSubmitting(false);
    }
  }

  if (status === 'RESOLVED') {
    return (
      <section className="file-search-clarification-card is-resolved">
        <CheckCircle2 size={18} aria-hidden />
        <span>分类决定已保存，文件位置未改变。</span>
      </section>
    );
  }
  if (status === 'SUPERSEDED' || status === 'EXPIRED') {
    return (
      <section className="file-search-clarification-card is-resolved">
        <Tags size={18} aria-hidden />
        <span>{status === 'EXPIRED' ? '该分类选择已过期，请重新提出分类要求。' : '该分类选择已被后续请求替代。'}</span>
      </section>
    );
  }

  return (
    <section className="file-search-clarification-card" aria-label="选择文件分类">
      <header>
        <Tags size={18} aria-hidden />
        <div>
          <strong>请选择具体文件分类</strong>
          <span>{result.prompt}</span>
        </div>
      </header>
      <div className="file-search-clarification-options">
        {result.options.map((option) => (
          <label
            className={selectedOptionId === option.id ? 'file-search-clarification-option is-selected' : 'file-search-clarification-option'}
            key={option.id}
          >
            <input
              checked={selectedOptionId === option.id}
              disabled={submitting}
              name={`classification-clarification-${result.id}`}
              onChange={() => setSelectedOptionId(option.id)}
              type="radio"
            />
            <div>
              <strong>{option.filename}</strong>
              <span>{option.category_label}</span>
            </div>
          </label>
        ))}
      </div>
      <footer>
        <button
          disabled={submitting || !selectedOptionId}
          onClick={() => void submitSelection()}
          type="button"
        >
          {submitting ? '正在保存…' : '确认选择'}
        </button>
        {error ? <span className="category-feedback__error">{error}</span> : null}
      </footer>
    </section>
  );
}
