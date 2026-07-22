// OperationPlan 卡片只负责展示和确认受控计划，不在浏览器直接修改文件。
import { CheckCircle2, Files, FilePenLine, RotateCcw, Trash2 } from 'lucide-react';
import { useState } from 'react';

import { confirmOperationPlan, getRenameBatchItems } from '../../api/client';
import type { OperationPlanItem, OperationPlanResponse, RenameBatchItem } from '../../types';

type OperationPlanCardProps = {
  token: string;
  plan: OperationPlanResponse;
  onConfirmed: () => Promise<void>;
};

export function OperationPlanCard({ token, plan, onConfirmed }: OperationPlanCardProps) {
  const [confirming, setConfirming] = useState(false);
  const [loadingItems, setLoadingItems] = useState(false);
  const [loadedItems, setLoadedItems] = useState<OperationPlanItem[] | null>(null);
  const [nextCursor, setNextCursor] = useState<number | null>(null);
  const [excludedItemIds, setExcludedItemIds] = useState<Set<string>>(() => new Set());
  const [error, setError] = useState('');
  const waiting = plan.status === 'WAITING_CONFIRMATION' || plan.status === 'PLANNED';
  const uploadedTemporaryRename = plan.operation_type === 'RENAME_UPLOADED_FILES';
  const presentation = operationPresentation(plan.operation_type);
  const pathPrefix = readOptionalString(plan.scope, 'path_prefix');
  const renameBatchId = readOptionalString(plan.scope, 'rename_batch_id');
  const totalItemCount = plan.total_item_count || plan.items.length;
  const visibleItems = loadedItems ?? plan.items;
  const selectable = waiting && Boolean(renameBatchId);
  const selectedCount = Math.max(0, totalItemCount - excludedItemIds.size);

  async function handleConfirm() {
    setConfirming(true);
    setError('');
    try {
      await confirmOperationPlan(token, plan.id, [...excludedItemIds]);
      await onConfirmed();
      setLoadedItems(null);
      setNextCursor(null);
      setExcludedItemIds(new Set());
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
            {presentation.icon}
            {uploadedTemporaryRename ? '上传附件临时重命名计划' : presentation.title}
          </strong>
          <span>{selectable ? `已选择 ${selectedCount} / ${totalItemCount} 个文件` : `${totalItemCount} 个文件`}</span>
        </div>
        <em className={`operation-plan-status operation-plan-status--${plan.status.toLowerCase()}`}>
          {formatPlanStatus(plan.status)}
        </em>
      </header>

      {uploadedTemporaryRename ? (
        <p>本次只修改附件在临时存储中的文件名，不执行分类或写入受管目录。</p>
      ) : null}

      {!uploadedTemporaryRename && plan.reason ? <p>{plan.reason}。受管原件不会改变。</p> : null}

      {!uploadedTemporaryRename && pathPrefix ? (
        <p className="operation-plan-scope">处理范围：{pathPrefix}</p>
      ) : null}

      <div className="operation-plan-items">
        {visibleItems.map((item, index) => (
          <div className="operation-plan-item" key={`${item.document_id}-${index}`}>
            {selectable ? (
              <input
                aria-label={`选择重命名 ${readString(item.before, 'filename')}`}
                checked={!excludedItemIds.has(readRenameBatchItemId(item))}
                onChange={(event) => {
                  const itemId = readRenameBatchItemId(item);
                  if (!itemId) return;
                  setExcludedItemIds((current) => {
                    const next = new Set(current);
                    if (event.target.checked) next.delete(itemId);
                    else next.add(itemId);
                    return next;
                  });
                }}
                type="checkbox"
              />
            ) : (
              <span>{index + 1}</span>
            )}
            <div>
              <del>{readString(item.before, 'filename')}</del>
              <strong>{formatOperationTarget(item)}</strong>
              <small>状态：{formatItemStatus(item.execution_status)}</small>
            </div>
          </div>
        ))}
      </div>

      {waiting && renameBatchId && (nextCursor !== null || totalItemCount > visibleItems.length) ? (
        <button
          className="rename-suggestion-more"
          disabled={loadingItems}
          onClick={async () => {
            setLoadingItems(true);
            try {
              const page = await getRenameBatchItems(
                token,
                renameBatchId,
                'EXECUTABLE',
                loadedItems === null ? 0 : (nextCursor ?? 0),
              );
              const mapped = page.items.map(batchItemToPlanItem);
              setLoadedItems((current) => current === null ? mapped : [...current, ...mapped]);
              setNextCursor(page.next_cursor);
            } catch (exception) {
              setError(exception instanceof Error ? exception.message : '加载文件明细失败');
            } finally {
              setLoadingItems(false);
            }
          }}
          type="button"
        >
          {loadingItems ? '加载中...' : `查看其余 ${Math.max(0, totalItemCount - visibleItems.length)} 个文件`}
        </button>
      ) : null}

      {plan.status === 'EXECUTED' ? (
        <div className="operation-plan-complete"><CheckCircle2 size={16} />{presentation.completedLabel}</div>
      ) : null}
      {error ? <p className="operation-plan-error">{error}</p> : null}
      {waiting ? (
        <button
          className="operation-plan-confirm"
          disabled={confirming || selectedCount === 0}
          onClick={handleConfirm}
          type="button"
        >
          {confirming ? '执行中...' : `${presentation.confirmLabel} ${selectedCount} 个文件`}
        </button>
      ) : null}
    </section>
  );
}

function operationPresentation(operationType: string) {
  // 同一确认卡覆盖多种工作副本动作，但文案不能把删除或恢复误称为重命名。
  if (operationType === 'TRASH_WORKING_COPIES') {
    return {
      title: '移入回收站计划',
      confirmLabel: '确认移入回收站',
      completedLabel: '文件已移入回收站，可继续通过对话恢复',
      icon: <Trash2 size={18} />,
    };
  }
  if (operationType === 'RESTORE_WORKING_COPIES') {
    return {
      title: '恢复文件计划',
      confirmLabel: '确认恢复',
      completedLabel: '文件已恢复',
      icon: <RotateCcw size={18} />,
    };
  }
  if (operationType === 'RESOLVE_FILENAME_CONFLICT') {
    return {
      title: '同名文件处理计划',
      confirmLabel: '确认处理',
      completedLabel: '同名文件处理已完成',
      icon: <Files size={18} />,
    };
  }
  return {
    title: '文件重命名计划',
    confirmLabel: '确认重命名',
    completedLabel: '重命名已执行',
    icon: <FilePenLine size={18} />,
  };
}

function formatOperationTarget(item: OperationPlanItem): string {
  // 目标文案只描述用户可理解的结果，不展示物理回收站或工作副本存储路径。
  if (item.operation === 'TRASH_WORKING_COPIES') return '移入回收站（可恢复）';
  if (item.operation === 'RESTORE_WORKING_COPIES') return `恢复为 ${readString(item.after, 'filename')}`;
  return readString(item.after, 'filename');
}

function batchItemToPlanItem(item: RenameBatchItem): OperationPlanItem {
  return {
    document_id: item.id,
    before: {
      managed_file_id: item.managed_file_id,
      relative_path: item.original_relative_path,
      filename: item.original_filename,
    },
    after: {
      filename: item.proposed_filename ?? '未命名文件',
    },
    rename_metadata: { rename_batch_item_id: item.id },
    execution_status: 'PLANNED',
  };
}

function readRenameBatchItemId(item: OperationPlanItem): string {
  const value = item.rename_metadata.rename_batch_item_id;
  return typeof value === 'string' ? value : '';
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
