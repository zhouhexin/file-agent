// AgentRun 回执组件展示结构化执行结果，文件打开仍交由上层受控回调处理。
import { useEffect, useState } from 'react';
import { CheckCircle2, FileText, Folder } from 'lucide-react';

import { getChangeSet } from '../../api/client';
import type { AgentRun, ChangeItem, ManagedFileResult, ToolInvocation } from '../../types';
import { DocumentResultCard } from './DocumentResultCard';
import type { ChatAttachment } from './presentation';
import { findAttachmentByDocumentId, formatFileSize, hasFileMutation } from './presentation';

const CHANGESET_ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const changeSetItemsCache = new Map<string, Promise<ChangeItem[]>>();

type AgentRunReceiptProps = {
  token?: string;
  state?: 'running' | 'failed';
  agentRun?: AgentRun;
  attachments?: ChatAttachment[];
  onOpenAttachment?: (file: ChatAttachment) => void;
  onOpenManagedFile?: (file: ManagedFileResult) => void;
};

export function AgentRunReceipt({
  token,
  state,
  agentRun,
  attachments = [],
  onOpenAttachment,
  onOpenManagedFile,
}: AgentRunReceiptProps) {
  // ChangeSet 只用于判断是否存在真实文件操作，不替代 AgentRun 的结构化结果展示。
  const [changeItems, setChangeItems] = useState<ChangeItem[] | null>(null);
  const results = agentRun?.document_results ?? [];
  const successCount = results.filter((item) => item.extraction_status === 'COMPLETED').length;
  const failedCount = results.filter((item) => item.extraction_status === 'FAILED').length;

  useEffect(() => {
    if (!agentRun) {
      return;
    }
    console.debug('[FileAgent] AgentRun 审计信息', {
      agent_run_id: agentRun.agent_run_id,
      status: agentRun.status,
      intent: agentRun.intent,
      tool_invocations: agentRun.tool_invocations.map((tool) => ({
        id: tool.id,
        tool_name: tool.tool_name,
        status: tool.status,
      })),
    });
  }, [agentRun]);

  useEffect(() => {
    let cancelled = false;
    const changesetId = agentRun?.changeset_id ?? '';
    if (!token || results.length === 0 || !isPersistedChangeSetId(changesetId)) {
      setChangeItems(null);
      return;
    }
    const timeoutId = window.setTimeout(() => {
      getCachedChangeSetItems(token, changesetId)
        .then((items) => {
          if (!cancelled) {
            setChangeItems(items);
          }
        })
        .catch(() => {
          if (!cancelled) {
            setChangeItems(null);
          }
        });
    }, 0);
    return () => {
      cancelled = true;
      window.clearTimeout(timeoutId);
    };
  }, [agentRun?.changeset_id, results.length, token]);

  if (state === 'running') {
    return (
      <section className="agent-run-receipt">
        <div className="agent-run-summary">
          <strong>正在处理</strong>
          <span>Agent 正在解析...</span>
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

  if (!agentRun) {
    return null;
  }

  const managedFileResult = getManagedFileResult(agentRun.tool_invocations);

  if (agentRun.intent === 'SUMMARIZE_DOCUMENTS' || agentRun.intent === 'ANSWER_DOCUMENTS') {
    return agentRun.final_response ? (
      <p className="agent-chat-response">{agentRun.final_response}</p>
    ) : null;
  }

  if (results.length === 0) {
    if (managedFileResult && managedFileResult.files.length > 0) {
      return (
        <ManagedFileTreeReceipt
          files={managedFileResult.files}
          rootKey={managedFileResult.rootKey}
          onOpenManagedFile={onOpenManagedFile}
        />
      );
    }
    // 纯聊天、分类汇总、历史分类读取等任务没有逐文件处理结果时，只展示 Agent 文本回复。
    return agentRun.final_response ? (
      <p className="agent-chat-response">{agentRun.final_response}</p>
    ) : null;
  }

  const mutationSummary = hasFileMutation(changeItems) ? '包含文件操作' : '本次仅生成分析结果';

  return (
    <section className="agent-run-receipt">
      <div className="agent-run-summary">
        <div>
          <strong>
            <CheckCircle2 size={18} />
            已处理 {results.length} 个文件
          </strong>
          <span>成功 {successCount} 个 · 失败 {failedCount} 个</span>
        </div>
        <em>{mutationSummary}</em>
      </div>

      {results.length > 0 ? (
        <div className="document-result-list">
          {results.map((result, index) => (
            <DocumentResultCard
              attachment={findAttachmentByDocumentId(attachments, result.document_id)}
              index={index + 1}
              key={`${result.document_id}-${index}`}
              result={result}
              onOpenFile={onOpenAttachment}
            />
          ))}
        </div>
      ) : agentRun.final_response ? (
        <p className="agent-final-response">{agentRun.final_response}</p>
      ) : null}
    </section>
  );
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

function getManagedFileResult(toolInvocations: ToolInvocation[]): { rootKey: string; files: ManagedFileResult[] } | null {
  // managed-file-list 的结构化输出用于前端交互树；纯文本 final_response 保留给历史兼容。
  const invocation = toolInvocations.find((item) => item.tool_name === 'managed-file-list');
  const output = invocation?.output_json;
  if (!output || output.ok !== true || !Array.isArray(output.files)) {
    return null;
  }
  const files = output.files.filter(isManagedFileResult);
  const query = isRecord(output.query) ? output.query : {};
  const rootKey = String(query.root_key || files[0]?.root_key || '受管目录');
  return { rootKey, files };
}

function isManagedFileResult(value: unknown): value is ManagedFileResult {
  // 运行时保护 API 数据，避免坏结构导致聊天页崩溃。
  if (!isRecord(value)) {
    return false;
  }
  return typeof value.root_key === 'string'
    && typeof value.relative_path === 'string'
    && typeof value.filename === 'string'
    && typeof value.extension === 'string'
    && typeof value.size_bytes === 'number'
    && typeof value.status === 'string';
}

function isRecord(value: unknown): value is Record<string, unknown> {
  // API 输出进入组件前先做最小结构判断，避免任意 JSON 破坏回执渲染。
  return Boolean(value && typeof value === 'object' && !Array.isArray(value));
}

function isPersistedChangeSetId(changesetId: string): boolean {
  // 后端真实 ChangeSet 使用 UUID；changeset-memory 等旧占位值不能触发详情请求。
  return CHANGESET_ID_PATTERN.test(changesetId);
}

function getCachedChangeSetItems(token: string, changesetId: string): Promise<ChangeItem[]> {
  // 历史消息中可能重复引用同一个 ChangeSet，缓存 Promise 可减少进入聊天页时的重复请求。
  const cached = changeSetItemsCache.get(changesetId);
  if (cached) {
    return cached;
  }
  const promise = getChangeSet(token, changesetId)
    .then((changeset) => changeset.items)
    .catch((error) => {
      changeSetItemsCache.delete(changesetId);
      throw error;
    });
  changeSetItemsCache.set(changesetId, promise);
  return promise;
}
