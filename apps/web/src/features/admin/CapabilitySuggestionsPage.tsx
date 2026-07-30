// 能力建议页供 ops/admin 评审 Catalog 无法满足的用户目标，不提供自动发布 Tool/Skill 的入口。
import { useCallback, useEffect, useState } from 'react';
import { ArrowLeft, RefreshCw } from 'lucide-react';

import {
  getCapabilitySuggestions,
  reviewCapabilitySuggestion,
} from '../../api/client';
import { formatError } from '../../api/errors';
import type { CapabilitySuggestion, User } from '../../types';
import './capability-suggestions.css';

type CapabilitySuggestionsPageProps = {
  token: string;
  user: User;
  onBack: () => void;
};

function formatTime(value: string | null): string {
  // 后端时间统一带时区，页面按管理员浏览器时区展示。
  return value ? new Date(value).toLocaleString() : '—';
}

export function CapabilitySuggestionsPage({
  token,
  user,
  onBack,
}: CapabilitySuggestionsPageProps) {
  const [rows, setRows] = useState<CapabilitySuggestion[]>([]);
  const [status, setStatus] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const loadRows = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      setRows(await getCapabilitySuggestions(token, status));
    } catch (err) {
      setError(formatError(err));
    } finally {
      setLoading(false);
    }
  }, [status, token]);

  useEffect(() => {
    void loadRows();
  }, [loadRows]);

  async function review(
    suggestion: CapabilitySuggestion,
    nextStatus: 'UNDER_REVIEW' | 'ACCEPTED' | 'REJECTED',
  ) {
    // 页面只提交受控状态；接受建议也不会触发代码或全局 Skill 发布。
    const note = window.prompt('评审备注（可选）', suggestion.review_note) ?? '';
    try {
      await reviewCapabilitySuggestion(
        token,
        suggestion.id,
        nextStatus,
        note,
      );
      await loadRows();
    } catch (err) {
      setError(formatError(err));
    }
  }

  return (
    <main className="capability-suggestions-page">
      <header className="capability-suggestions-header">
        <div>
          <h1>能力建议</h1>
          <p>这里只评审能力缺口，不会自动创建或启用 Tool、Skill。</p>
        </div>
        <div className="capability-suggestions-actions">
          <button type="button" onClick={onBack}>
            <ArrowLeft size={16} />
            返回聊天
          </button>
          <select value={status} onChange={(event) => setStatus(event.target.value)}>
            <option value="">全部状态</option>
            <option value="NEW">待处理</option>
            <option value="UNDER_REVIEW">评审中</option>
            <option value="ACCEPTED">已接受</option>
            <option value="REJECTED">已拒绝</option>
            <option value="IMPLEMENTED">已实现</option>
          </select>
          <button type="button" onClick={() => void loadRows()} disabled={loading}>
            <RefreshCw size={16} />
            刷新
          </button>
        </div>
      </header>

      {error ? <p className="capability-suggestions-error">{error}</p> : null}
      <section className="capability-suggestions-list" aria-busy={loading}>
        {loading ? <p>正在加载...</p> : null}
        {!loading && rows.length === 0 ? <p>当前没有能力建议。</p> : null}
        {rows.map((row) => (
          <article key={row.id} className="capability-suggestion-card">
            <div className="capability-suggestion-title">
              <div>
                <span>{row.suggestion_kind}</span>
                <h2>{row.title}</h2>
              </div>
              <strong>{row.status}</strong>
            </div>
            <p>{row.missing_capability}</p>
            <p className="capability-suggestion-reason">{row.reason}</p>
            <dl>
              <div><dt>出现次数</dt><dd>{row.occurrence_count}</dd></div>
              <div><dt>置信度</dt><dd>{Math.round(row.confidence * 100)}%</dd></div>
              <div><dt>关联能力</dt><dd>{row.related_skill_ids_json.join('、') || '—'}</dd></div>
              <div><dt>最近出现</dt><dd>{formatTime(row.updated_at)}</dd></div>
            </dl>
            <div className="capability-suggestion-buttons">
              {user.role === 'admin' || !['ACCEPTED', 'IMPLEMENTED'].includes(row.status) ? (
                <button type="button" onClick={() => void review(row, 'UNDER_REVIEW')}>
                  开始评审
                </button>
              ) : null}
              {user.role === 'admin' ? (
                <button type="button" onClick={() => void review(row, 'ACCEPTED')}>
                  接受
                </button>
              ) : null}
              {user.role === 'admin' || !['ACCEPTED', 'IMPLEMENTED'].includes(row.status) ? (
                <button type="button" onClick={() => void review(row, 'REJECTED')}>
                  拒绝
                </button>
              ) : null}
            </div>
          </article>
        ))}
      </section>
    </main>
  );
}
