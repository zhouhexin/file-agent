// OperationPlan 卡片只负责展示和确认受控计划，不在浏览器直接修改文件。
import { AlertTriangle, CheckCircle2, FilePenLine } from 'lucide-react';
import { useState } from 'react';

import { confirmOperationPlan } from '../../api/client';
import type { OperationPlanResponse } from '../../types';

type OperationPlanCardProps = {
  token: string;
  plan: OperationPlanResponse;
  onConfirmed: () => Promise<void>;
};

export function OperationPlanCard({ token, plan, onConfirmed }: OperationPlanCardProps) {
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState('');
  const waiting = plan.status === 'WAITING_CONFIRMATION' || plan.status === 'PLANNED';
  const uploadedTemporaryRename = plan.operation_type === 'RENAME_UPLOADED_FILES';
  const pathPrefix = readOptionalString(plan.scope, 'path_prefix');

  async function handleConfirm() {
    setConfirming(true);
    setError('');
    try {
      await confirmOperationPlan(token, plan.id);
      await onConfirmed();
    } catch (exception) {
      setError(exception instanceof Error ? exception.message : '确认执行失败');
    } finally {
      setConfirming(false);
    }
  }

  return (
    <section className="operation-plan-card">
      <header className="operation-plan-header">
        <div>
          <strong>
            <FilePenLine size={18} />
            {uploadedTemporaryRename ? '上传附件临时重命名计划' : '文件重命名计划'}
          </strong>
          <span>{plan.items.length} 个可执行 · {plan.skipped_items.length} 个待复核</span>
        </div>
        <em className={`operation-plan-status operation-plan-status--${plan.status.toLowerCase()}`}>
          {formatPlanStatus(plan.status)}
        </em>
      </header>

      {uploadedTemporaryRename ? (
        <p>本次只修改附件在临时存储中的文件名，不执行分类或写入受管目录。</p>
      ) : null}

      {!uploadedTemporaryRename && pathPrefix ? (
        <p className="operation-plan-scope">处理范围：{pathPrefix}</p>
      ) : null}

      <div className="operation-plan-items">
        {plan.items.map((item, index) => (
          <div className="operation-plan-item" key={`${item.document_id}-${index}`}>
            <span>{index + 1}</span>
            <div>
              <del>{readString(item.before, 'filename')}</del>
              <strong>{readString(item.after, 'filename')}</strong>
              <small>状态：{formatItemStatus(item.execution_status)}</small>
            </div>
          </div>
        ))}
      </div>

      {plan.skipped_items.length > 0 ? (
        <div className="operation-plan-review">
          <AlertTriangle size={16} />
          <span>待复核文件未进入执行批次，不会随本次确认被改名。</span>
        </div>
      ) : null}

      {plan.status === 'EXECUTED' ? (
        <div className="operation-plan-complete"><CheckCircle2 size={16} />重命名已执行</div>
      ) : null}
      {error ? <p className="operation-plan-error">{error}</p> : null}
      {waiting ? (
        <button className="operation-plan-confirm" disabled={confirming} onClick={handleConfirm} type="button">
          {confirming ? '执行中...' : `确认重命名 ${plan.items.length} 个文件`}
        </button>
      ) : null}
    </section>
  );
}

function readString(payload: Record<string, unknown>, key: string): string {
  const value = payload[key];
  return typeof value === 'string' && value ? value : '未命名文件';
}

function readOptionalString(payload: Record<string, unknown> | undefined, key: string): string {
  const value = payload?.[key];
  return typeof value === 'string' ? value : '';
}

function formatPlanStatus(status: string): string {
  if (status === 'WAITING_CONFIRMATION' || status === 'PLANNED') return '等待确认';
  if (status === 'EXECUTED') return '已执行';
  if (status === 'PARTIAL') return '部分完成';
  if (status === 'FAILED') return '执行失败';
  return status;
}

function formatItemStatus(status: string): string {
  // 逐文件状态来自确认执行后的 OperationPlan，不根据计划总状态猜测。
  if (status === 'PLANNED') return '等待确认';
  if (status === 'COMPLETED') return '已完成';
  if (status === 'FAILED') return '失败';
  return status;
}
