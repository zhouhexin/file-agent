// Agent 任务诊断页面向 ops/admin 展示中文处理时间线，不直接呈现原始日志 JSON。
import { useCallback, useEffect, useState } from 'react';
import { ArrowLeft, RefreshCw } from 'lucide-react';

import { getAdminAgentRuns, getAgentRunDiagnostics } from '../../api/client';
import { formatError } from '../../api/errors';
import type { AdminAgentRun, AgentRunDiagnostics } from '../../types';
import './agent-runs.css';

type AgentRunsPageProps = {
  token: string;
  onBack: () => void;
};

function formatTime(value: string): string {
  // 浏览器按当前管理员本地时区展示后端 timestamptz。
  return new Date(value).toLocaleString();
}

function statusLabel(status: string): string {
  // 保留后端稳定枚举用于筛选，页面使用自然语言降低运维理解成本。
  const labels: Record<string, string> = {
    RECEIVED: '已接收',
    PLANNING: '正在理解',
    RUNNING_TOOL: '正在处理',
    WAITING_FOR_ASYNC_JOB: '等待后台处理',
    WAITING_FOR_CONFIRMATION: '等待用户确认',
    SUMMARIZING: '正在汇总',
    COMPLETED: '已完成',
    FAILED: '失败',
    NEEDS_REVIEW: '需要复核',
  };
  return labels[status] || status;
}

export function AgentRunsPage({ token, onBack }: AgentRunsPageProps) {
  const [rows, setRows] = useState<AdminAgentRun[]>([]);
  const [selected, setSelected] = useState<AgentRunDiagnostics | null>(null);
  const [status, setStatus] = useState('');
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState('');

  const loadRows = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const nextRows = await getAdminAgentRuns(token, status);
      setRows(nextRows);
      if (
        selected
        && !nextRows.some((row) => row.id === selected.run.id)
      ) {
        setSelected(null);
      }
    } catch (err) {
      setError(formatError(err));
    } finally {
      setLoading(false);
    }
  }, [selected, status, token]);

  useEffect(() => {
    void loadRows();
    // 这里只在筛选或 token 改变时刷新；选中详情不应触发重复列表请求。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, token]);

  async function openDiagnostics(agentRunId: string) {
    setDetailLoading(true);
    setError('');
    try {
      setSelected(await getAgentRunDiagnostics(token, agentRunId));
    } catch (err) {
      setError(formatError(err));
    } finally {
      setDetailLoading(false);
    }
  }

  return (
    <main className="agent-runs-page">
      <header className="agent-runs-header">
        <div>
          <h1>任务诊断</h1>
          <p>查看自然语言任务停在哪个阶段，以及运维人员可以采取的下一步。</p>
        </div>
        <div className="agent-runs-actions">
          <button type="button" onClick={onBack}>
            <ArrowLeft size={16} />
            返回聊天
          </button>
          <button type="button" onClick={() => void loadRows()} disabled={loading}>
            <RefreshCw size={16} />
            刷新
          </button>
        </div>
      </header>

      <div className="agent-runs-filter">
        <label htmlFor="agent-run-status">任务状态</label>
        <select id="agent-run-status" value={status} onChange={(event) => setStatus(event.target.value)}>
          <option value="">全部</option>
          <option value="WAITING_FOR_ASYNC_JOB">等待后台处理</option>
          <option value="FAILED">失败</option>
          <option value="NEEDS_REVIEW">需要复核</option>
          <option value="COMPLETED">已完成</option>
        </select>
      </div>

      {error ? <p className="agent-runs-error">{error}</p> : null}
      <div className="agent-runs-layout">
        <section className="agent-runs-list" aria-busy={loading}>
          {loading ? <p>正在加载任务...</p> : null}
          {!loading && rows.length === 0 ? <p>当前筛选条件下没有任务。</p> : null}
          {rows.map((row) => (
            <button
              className={selected?.run.id === row.id ? 'agent-run-row is-active' : 'agent-run-row'}
              key={row.id}
              onClick={() => void openDiagnostics(row.id)}
              type="button"
            >
              <span>
                <strong>{row.intent || '普通自然语言任务'}</strong>
                <em>{statusLabel(row.status)}</em>
              </span>
              <small>{formatTime(row.updated_at)}</small>
              <code>{row.id}</code>
            </button>
          ))}
        </section>

        <section className="agent-run-diagnostics" aria-busy={detailLoading}>
          {detailLoading ? <p>正在生成诊断时间线...</p> : null}
          {!detailLoading && !selected ? <p>选择左侧任务查看处理过程。</p> : null}
          {!detailLoading && selected ? (
            <>
              <header>
                <h2>{statusLabel(selected.run.status)}</h2>
                <p>{selected.summary}</p>
              </header>
              {selected.recommended_actions.length > 0 ? (
                <aside>
                  <strong>建议处理</strong>
                  <ul>
                    {selected.recommended_actions.map((item) => <li key={item}>{item}</li>)}
                  </ul>
                </aside>
              ) : null}
              <ol className="diagnostic-timeline">
                {selected.events.map((event, index) => (
                  <li key={`${event.occurred_at}-${event.event_title}-${index}`}>
                    <div>
                      <strong>{event.event_title}</strong>
                      {event.status ? <span>{statusLabel(event.status)}</span> : null}
                    </div>
                    <p>{event.operator_message}</p>
                    {event.cause_code ? <code>错误编号：{event.cause_code}</code> : null}
                    {event.recommended_action ? <em>建议：{event.recommended_action}</em> : null}
                    <small>{formatTime(event.occurred_at)}</small>
                  </li>
                ))}
              </ol>
            </>
          ) : null}
        </section>
      </div>
    </main>
  );
}
