import { useEffect, useState } from 'react';

import { getChangeSet } from '../../api/client';
import type { AgentRun, ChangeItem } from '../../types';
import { DocumentResultCard } from './DocumentResultCard';
import type { ChatAttachment } from './presentation';
import { findAttachmentByDocumentId, hasFileMutation } from './presentation';

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
    if (!token || !agentRun?.changeset_id) {
      setChangeItems(null);
      return;
    }
    getChangeSet(token, agentRun.changeset_id)
      .then((changeset) => {
        if (!cancelled) {
          setChangeItems(changeset.items);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setChangeItems(null);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [agentRun?.changeset_id, token]);

  if (state === 'running') {
    return (
      <section className="agent-run-receipt">
        <div className="agent-run-summary">
          <strong>正在处理文件</strong>
          <span>Agent 正在解析附件并生成分类建议</span>
        </div>
      </section>
    );
  }

  if (state === 'failed') {
    return (
      <section className="agent-run-receipt">
        <div className="agent-run-summary agent-run-summary--failed">
          <strong>处理失败</strong>
          <span>请检查附件或稍后重新发送。</span>
        </div>
      </section>
    );
  }

  if (!agentRun) {
    return null;
  }

  const mutationSummary = hasFileMutation(changeItems) ? '包含文件操作' : '本次仅生成分析结果';

  return (
    <section className="agent-run-receipt">
      <div className="agent-run-summary">
        <div>
          <strong>已处理 {results.length} 个文件</strong>
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
