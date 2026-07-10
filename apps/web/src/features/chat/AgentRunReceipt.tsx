import { useEffect, useState } from 'react';
import { CheckCircle2 } from 'lucide-react';

import { getChangeSet } from '../../api/client';
import type { AgentRun, ChangeItem } from '../../types';
import { DocumentResultCard } from './DocumentResultCard';
import type { ChatAttachment } from './presentation';
import { findAttachmentByDocumentId, hasFileMutation } from './presentation';

const CHANGESET_ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const changeSetItemsCache = new Map<string, Promise<ChangeItem[]>>();

type AgentRunReceiptProps = {
  token?: string;
  state?: 'running' | 'failed';
  agentRun?: AgentRun;
  attachments?: ChatAttachment[];
  onOpenAttachment?: (file: ChatAttachment) => void;
};

export function AgentRunReceipt({
  token,
  state,
  agentRun,
  attachments = [],
  onOpenAttachment,
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

  if (agentRun.intent === 'SUMMARIZE_DOCUMENTS' || agentRun.intent === 'ANSWER_DOCUMENTS') {
    return agentRun.final_response ? (
      <p className="agent-chat-response">{agentRun.final_response}</p>
    ) : null;
  }

  if (results.length === 0) {
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
