// AgentRun 回执组件展示结构化执行结果，文件打开仍交由上层受控回调处理。
import { useEffect, useState } from 'react';
import { AlertCircle, CheckCircle2, FileText, Folder } from 'lucide-react';

import { getOperationPlan } from '../../api/client';
import type { ManagedFileResult, OperationPlanResponse, TaskResult } from '../../types';
import { DocumentResultCard } from './DocumentResultCard';
import { OperationPlanCard } from './OperationPlanCard';
import { RenameSuggestionReceipt } from './RenameSuggestionReceipt';
import { SearchResultsReceipt } from './SearchResultsReceipt';
import type { ChatAttachment } from './presentation';
import { findAttachmentByDocumentId, formatFileSize } from './presentation';

const CHANGESET_ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

type AgentRunReceiptProps = {
  token?: string;
  state?: 'running' | 'failed';
  taskResult?: TaskResult;
  attachments?: ChatAttachment[];
  onOpenAttachment?: (file: ChatAttachment) => void;
  onOpenDocument?: (documentId: string, filename: string) => void;
  onOpenManagedFile?: (file: ManagedFileResult) => void;
};

export function AgentRunReceipt({
  token,
  state,
  taskResult,
  attachments = [],
  onOpenAttachment,
  onOpenDocument,
  onOpenManagedFile,
}: AgentRunReceiptProps) {
  const [operationPlan, setOperationPlan] = useState<OperationPlanResponse | null>(null);
  const results = taskResult?.document_results ?? [];

  useEffect(() => {
    let cancelled = false;
    const planId = taskResult?.operation_plan_id ?? '';
    if (!token || !isPersistedOperationPlanId(planId)) {
      setOperationPlan(null);
      return;
    }
    getOperationPlan(token, planId)
      .then((plan) => {
        if (!cancelled) setOperationPlan(plan);
      })
      .catch(() => {
        if (!cancelled) setOperationPlan(null);
      });
    return () => {
      cancelled = true;
    };
  }, [taskResult?.operation_plan_id, token]);

  if (state === 'running') {
    // 不暴露内部 Agent、Skill 和 Tool 状态，但不能把正在处理的轮次渲染成空白。
    return (
      <section className="agent-run-receipt" aria-live="polite">
        <div className="agent-run-summary">
          <strong>正在处理你的请求…</strong>
        </div>
      </section>
    );
  }

  if (state === 'failed') {
    return (
      <section className="agent-run-receipt">
        <div className="agent-run-summary agent-run-summary--failed">
          <strong>处理失败</strong>
          <span>请稍后重新发送。</span>
        </div>
      </section>
    );
  }

  if (!taskResult) {
    return null;
  }

  // 历史恢复和实时接口都可能直接携带失败状态。不能只依赖 ChatTurn 的本地状态，
  // 否则后端已失败的任务会被渲染成没有任何内容的空白区域。
  if (taskResult.task_status === 'failed') {
    return (
      <section className="agent-run-receipt">
        <div className="agent-run-summary agent-run-summary--failed">
          <strong>本次请求未能完成</strong>
          <span>{taskResult.final_response || '请稍后重试；如问题持续出现，请查看服务端日志。'}</span>
        </div>
      </section>
    );
  }

  if (
    taskResult.task_status === 'processing'
    && !taskResult.final_response
    && taskResult.document_results.length === 0
    && !taskResult.managed_file_result
    && !taskResult.file_search_result
  ) {
    // 异步任务刚入队时尚无最终回执；明确反馈等待状态，不能留下一块无内容的消息区域。
    return (
      <section className="agent-run-receipt" aria-live="polite">
        <div className="agent-run-summary">
          <strong>文件正在后台处理，完成后会自动更新这里。</strong>
        </div>
      </section>
    );
  }

  const managedFileResult = taskResult.managed_file_result;
  const renamePlanResult = taskResult.rename_plan_result;
  const pendingDecisions = taskResult.pending_decisions ?? [];

  if (taskResult.response_type === 'rename_plan') {
    return (
      <>
        {operationPlan && token ? (
          <OperationPlanCard
            plan={operationPlan}
            token={token}
            onConfirmed={async () => {
              setOperationPlan(await getOperationPlan(token, operationPlan.id));
            }}
          />
        ) : null}
        {renamePlanResult ? (
          <RenameSuggestionReceipt token={token} result={renamePlanResult} onOpenManagedFile={onOpenManagedFile} />
        ) : taskResult.final_response ? (
          <p className="agent-chat-response">{taskResult.final_response}</p>
        ) : null}
      </>
    );
  }

  if (
    taskResult.response_type === 'file_search_results' &&
    taskResult.file_search_result
  ) {
    return (
      <SearchResultsReceipt
        result={taskResult.file_search_result}
        attachments={attachments}
        onOpenAttachment={onOpenAttachment}
        onOpenDocument={onOpenDocument}
      />
    );
  }
  if (operationPlan && token) {
    return (
      <OperationPlanCard
        plan={operationPlan}
        token={token}
        onConfirmed={async () => {
          setOperationPlan(await getOperationPlan(token, operationPlan.id));
        }}
      />
    );
  }

  if (taskResult.response_type === 'text') {
    return taskResult.final_response ? (
      <p className="agent-chat-response">{taskResult.final_response}</p>
    ) : null;
  }

  if (results.length === 0) {
    if (managedFileResult && managedFileResult.files.length > 0) {
      return (
        <ManagedFileTreeReceipt
          files={managedFileResult.files}
          rootKey={managedFileResult.root_key}
          onOpenManagedFile={onOpenManagedFile}
        />
      );
    }
    // 纯聊天、分类汇总、历史分类读取等任务没有逐文件处理结果时，只展示 Agent 文本回复。
    return taskResult.final_response ? (
      <p className="agent-chat-response">{taskResult.final_response}</p>
    ) : null;
  }

  return (
    <section className="agent-run-receipt">
      <div className="agent-run-summary">
        <div>
          <strong>
            {pendingDecisions.length > 0 ? <AlertCircle size={18} /> : <CheckCircle2 size={18} />}
            已处理 {taskResult.processed_count || results.length} 个文件
          </strong>
          {pendingDecisions.length > 0 ? <span>{pendingDecisions.length} 个文件需要确认</span> : null}
        </div>
      </div>

      {pendingDecisions.length > 0 ? <PendingDecisionList decisions={pendingDecisions} /> : null}

      {/* 逐文件卡说明处理明细，final_response 仍是用户可读的总体回执，不能因存在卡片而被隐藏。 */}
      {taskResult.final_response ? (
        <p className="agent-final-response">{taskResult.final_response}</p>
      ) : null}

      {results.length > 0 ? (
        <div className="document-result-list">
          {results.map((result, index) => (
            <DocumentResultCard
              attachment={findAttachmentByDocumentId(attachments, result.document_id)}
              index={index + 1}
              key={`${result.document_id}-${index}`}
              result={result}
              token={token}
              onOpenFile={onOpenAttachment}
            />
          ))}
        </div>
      ) : null}
    </section>
  );
}

function PendingDecisionList({ decisions }: { decisions: Array<Record<string, unknown>> }) {
  // 待确认卡只给出用户决策语义，不展示内部计划、Tool 或物理路径。
  return (
    <div className="task-pending-decisions">
      {decisions.map((decision, index) => {
        const message = String(decision.message || '这个文件需要你确认后才能继续整理。');
        const choices = Array.isArray(decision.allowed_decisions)
          ? decision.allowed_decisions.map((item) => decisionLabel(String(item)))
          : [];
        return (
          <article className="task-pending-decision" key={`${String(decision.working_copy_id || '')}-${index}`}>
            <strong>需要确认</strong>
            <p>{message}</p>
            {choices.length > 0 ? <span>可选处理方式：{choices.join('、')}</span> : null}
            {decision.reason === 'FILENAME_CONFLICT' ? (
              <small>请直接回复“同时保留”“保留已有文件”“用新文件替换已有文件”或“删除已有文件”。</small>
            ) : null}
          </article>
        );
      })}
    </div>
  );
}

function decisionLabel(value: string): string {
  // 后端使用稳定枚举审计，普通用户只看到自然语言选项。
  const labels: Record<string, string> = {
    KEEP_BOTH: '同时保留',
    KEEP_EXISTING: '保留已有文件',
    REPLACE_EXISTING_WORKING_COPY: '用新文件替换已有工作副本',
    DELETE_EXISTING_WORKING_COPY: '删除已有工作副本',
    CONFIRM_CURRENT_NAME: '保留当前文件名',
    PROVIDE_NEW_NAME: '告诉我新的文件名',
    REQUEST_RENAME_PLAN: '查看重命名计划',
    KEEP_CURRENT_NAME: '保留当前文件名',
    UPLOAD_READABLE_COPY: '上传可读取版本',
  };
  return labels[value] || value;
}

type ManagedFileTreeNode = {
  // 目录树只用于前端展示，不代表服务端真实文件系统路径。
  directories: Map<string, ManagedFileTreeNode>;
  files: ManagedFileResult[];
};

type ManagedFileTreeReceiptProps = {
  // rootKey 是后端授权后的逻辑目录标识，不能被当作本地路径使用。
  rootKey: string;
  files: ManagedFileResult[];
  onOpenManagedFile?: (file: ManagedFileResult) => void;
};

function ManagedFileTreeReceipt({
  rootKey,
  files,
  onOpenManagedFile,
}: ManagedFileTreeReceiptProps) {
  // 受管文件结果按目录树展示，点击文件时复用 ChatPage 的 Blob 预览/下载流程。
  const tree = buildManagedFileTree(files);
  return (
    <section className="managed-file-tree-card">
      <div className="managed-file-tree-summary">
        <strong>{rootKey} 下共有 {files.length} 个文件</strong>
      </div>
      <div className="managed-file-tree">
        <ManagedFileTreeNodeView
          depth={0}
          node={tree}
          onOpenManagedFile={onOpenManagedFile}
        />
      </div>
    </section>
  );
}

function ManagedFileTreeNodeView({
  node,
  depth,
  onOpenManagedFile,
}: {
  node: ManagedFileTreeNode;
  depth: number;
  onOpenManagedFile?: (file: ManagedFileResult) => void;
}) {
  // 子节点排序只影响展示稳定性，不改变后端返回的检索语义。
  const directories = Array.from(node.directories.entries()).sort(([left], [right]) => left.localeCompare(right));
  const files = [...node.files].sort((left, right) => left.filename.localeCompare(right.filename));
  return (
    <>
      {directories.map(([name, child]) => (
        <div className="managed-file-tree-group" key={`${depth}-${name}`}>
          <div className="managed-file-tree-row managed-file-tree-folder" style={{ paddingLeft: `${depth * 18}px` }}>
            <Folder size={15} />
            <span>{name}</span>
          </div>
          <ManagedFileTreeNodeView
            depth={depth + 1}
            node={child}
            onOpenManagedFile={onOpenManagedFile}
          />
        </div>
      ))}
      {files.map((file) => (
        <button
          className="managed-file-tree-row managed-file-tree-file"
          disabled={!onOpenManagedFile || file.status === 'MISSING'}
          key={`${file.root_key}-${file.relative_path}`}
          onClick={() => onOpenManagedFile?.(file)}
          style={{ paddingLeft: `${depth * 18}px` }}
          type="button"
        >
          <FileText size={15} />
          <span>{file.filename}</span>
          <em>{formatFileSize(file.size_bytes)}</em>
        </button>
      ))}
    </>
  );
}

function buildManagedFileTree(files: ManagedFileResult[]): ManagedFileTreeNode {
  // 后端只返回相对路径；前端用它构造展示树，不推断真实服务器路径。
  const root: ManagedFileTreeNode = { directories: new Map(), files: [] };
  for (const file of files) {
    const parts = file.relative_path.split('/').filter(Boolean);
    let cursor = root;
    for (const part of parts.slice(0, -1)) {
      let child = cursor.directories.get(part);
      if (!child) {
        child = { directories: new Map(), files: [] };
        cursor.directories.set(part, child);
      }
      cursor = child;
    }
    cursor.files.push(file);
  }
  return root;
}

function isPersistedOperationPlanId(planId: string): boolean {
  // 仅真实 UUID 才访问详情接口，避免 operation-plan-pending 等占位值触发 404。
  return CHANGESET_ID_PATTERN.test(planId);
}
