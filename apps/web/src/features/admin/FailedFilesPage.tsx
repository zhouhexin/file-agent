// 失败文件页只展示最终失败的导入/分析任务，供 ops/admin 快速定位积压根因。
import { useCallback, useEffect, useState } from 'react';
import { ArrowLeft, RefreshCw } from 'lucide-react';

import { getFailedFileJobs } from '../../api/client';
import { formatError } from '../../api/errors';
import type { FailedFileJob } from '../../types';
import './failed-files.css';

type FailedFilesPageProps = {
  token: string;
  onBack: () => void;
};

function formatTime(value: string | null): string {
  // 后端统一返回带时区时间；浏览器按管理员本地时区展示。
  return value ? new Date(value).toLocaleString() : '—';
}

function stageLabel(jobType: string): string {
  // 页面使用业务阶段名称，不要求管理员记忆内部任务枚举。
  return jobType === 'IMPORT_WORKING_COPIES' ? '复制导入' : '解析与索引';
}

export function FailedFilesPage({ token, onBack }: FailedFilesPageProps) {
  const [rows, setRows] = useState<FailedFileJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const loadRows = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      setRows(await getFailedFileJobs(token));
    } catch (err) {
      setError(formatError(err));
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    void loadRows();
  }, [loadRows]);

  return (
    <main className="failed-files-page">
      <header className="failed-files-header">
        <div>
          <h1>失败文件</h1>
          <p>仅显示已停止自动重试的导入与分析任务。</p>
        </div>
        <div className="failed-files-actions">
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

      {error ? <p className="failed-files-error">{error}</p> : null}
      <section className="failed-files-table-wrap" aria-busy={loading}>
        <table className="failed-files-table">
          <thead>
            <tr>
              <th>文件</th>
              <th>失败阶段</th>
              <th>尝试次数</th>
              <th>错误信息</th>
              <th>错误编号</th>
              <th>失败时间</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={6}>正在加载...</td></tr>
            ) : rows.length === 0 ? (
              <tr><td colSpan={6}>当前没有最终失败的文件。</td></tr>
            ) : rows.map((row) => (
              <tr key={row.job_id}>
                <td>
                  <strong>{row.filename}</strong>
                  <span>{[row.root_key, row.relative_path].filter(Boolean).join(' / ') || '路径未登记'}</span>
                </td>
                <td>{stageLabel(row.job_type)}</td>
                <td>{row.attempt_count} / {row.max_attempts}</td>
                <td>{row.error_message || '未记录公开错误摘要'}</td>
                <td>{row.error_reference || '—'}</td>
                <td>{formatTime(row.finished_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </main>
  );
}
