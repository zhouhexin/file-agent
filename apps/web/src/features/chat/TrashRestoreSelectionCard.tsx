// 回收站精确文件名选择卡只提交用户选中的条目，不根据版本、哈希或时间自动决定。
import { useState } from 'react';
import { RotateCcw } from 'lucide-react';

import { confirmOperationPlan, createTrashRestorePlan } from '../../api/client';
import { formatError } from '../../api/errors';
import type { TrashRestoreCandidate, TrashRestoreResult } from '../../types';
import { formatFileSize } from './presentation';

type TrashRestoreSelectionCardProps = {
  token: string;
  result: TrashRestoreResult;
};

export function TrashRestoreSelectionCard({ token, result }: TrashRestoreSelectionCardProps) {
  // 即使只有一条或多条候选完全一致，也不预选，必须保留用户明确选择证据。
  const [selectedTrashEntryId, setSelectedTrashEntryId] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [completedFilename, setCompletedFilename] = useState('');
  const [dismissed, setDismissed] = useState(false);
  const [error, setError] = useState('');

  async function restoreSelected() {
    const selected = result.candidates.find(
      (candidate) => candidate.trash_entry_id === selectedTrashEntryId,
    );
    if (!selected) {
      setError('请先选择需要恢复的文件。');
      return;
    }
    setSubmitting(true);
    setError('');
    try {
      // 点击“恢复所选文件”是本次明确确认；后端仍先持久化 OperationPlan，
      // 再走独立确认接口执行，不能由浏览器直接移动文件。
      const plan = await createTrashRestorePlan(
        token,
        selected.trash_entry_id,
        result.conversation_id,
      );
      await confirmOperationPlan(token, plan.id);
      setCompletedFilename(selected.filename);
    } catch (err) {
      setError(formatError(err));
    } finally {
      setSubmitting(false);
    }
  }

  if (completedFilename) {
    return (
      <section className="trash-restore-card trash-restore-card--completed">
        <RotateCcw size={18} aria-hidden />
        <strong>已恢复“{completedFilename}”</strong>
      </section>
    );
  }
  if (dismissed) {
    return <p className="agent-chat-response">已保留文件的删除状态，本次未恢复。</p>;
  }

  return (
    <section className="trash-restore-card" aria-label="选择需要恢复的已删除文件">
      <header>
        <RotateCcw size={18} aria-hidden />
        <div>
          <strong>找到了已删除的文件</strong>
          <span>{result.message}</span>
        </div>
      </header>

      <div className="trash-restore-candidates">
        {result.candidates.map((candidate) => (
          <TrashRestoreChoice
            candidate={candidate}
            checked={selectedTrashEntryId === candidate.trash_entry_id}
            disabled={submitting}
            key={candidate.trash_entry_id}
            onSelect={() => setSelectedTrashEntryId(candidate.trash_entry_id)}
          />
        ))}
      </div>

      <footer>
        <button disabled={submitting || !selectedTrashEntryId} onClick={() => void restoreSelected()} type="button">
          {submitting ? '正在恢复…' : '恢复所选文件'}
        </button>
        <button className="secondary" disabled={submitting} onClick={() => setDismissed(true)} type="button">
          暂不恢复
        </button>
      </footer>
      {error ? <p className="duplicate-review-error">{error}</p> : null}
    </section>
  );
}

function TrashRestoreChoice({
  candidate,
  checked,
  disabled,
  onSelect,
}: {
  candidate: TrashRestoreCandidate;
  checked: boolean;
  disabled: boolean;
  onSelect: () => void;
}) {
  // 展示可区分信息，但不展示数据库 ID、哈希或物理路径。

  return (
    <label className={checked ? 'trash-restore-choice is-selected' : 'trash-restore-choice'}>
      <input checked={checked} disabled={disabled} name="trash-restore-candidate" onChange={onSelect} type="radio" />
      <div>
        <strong>文件 {candidate.display_index}：{candidate.filename}</strong>
        <span>{formatFileSize(candidate.size_bytes)} · 版本 {candidate.version_number}</span>
        <small>删除时间：{formatTimestamp(candidate.deleted_at)}</small>
        <small>创建时间：{formatTimestamp(candidate.created_at)}</small>
      </div>
    </label>
  );
}

function formatTimestamp(value: string): string {
  // 把后端时间转换为本地可读格式，非法值保持占位而不猜测。

  const timestamp = new Date(value);
  return Number.isNaN(timestamp.getTime()) ? '未知' : timestamp.toLocaleString('zh-CN');
}
