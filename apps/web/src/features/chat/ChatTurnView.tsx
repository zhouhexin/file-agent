// 单轮对话视图负责串联用户消息、附件和 AgentRun 回执，不直接读取文件内容。
import { useCallback, useState } from 'react';
import { Bot } from 'lucide-react';
import { AgentRunReceipt } from './AgentRunReceipt';
import { AttachmentRail } from './AttachmentRail';
import { DuplicateUploadReviewLoader } from './DuplicateUploadReviewCard';
import type { ChatAttachment, ChatTurn } from './presentation';
import type { ManagedFileResult, SendMessageResponse } from '../../types';

type ChatTurnViewProps = {
  token: string;
  turn: ChatTurn;
  onOpenAttachment: (file: ChatAttachment) => void;
  onRestoreAttachment: (file: ChatAttachment) => void;
  onOpenDocument: (documentId: string, filename: string) => void;
  onOpenManagedFile: (file: ManagedFileResult) => void;
  onFollowupResult?: (response: SendMessageResponse) => void;
  onOperationConfirmed?: () => Promise<void>;
};

export function ChatTurnView({
  token,
  turn,
  onOpenAttachment,
  onRestoreAttachment,
  onOpenDocument,
  onOpenManagedFile,
  onFollowupResult,
  onOperationConfirmed,
}: ChatTurnViewProps) {
  const [resolvedDuplicateHidden, setResolvedDuplicateHidden] = useState(false);
  const hideResolvedDuplicate = useCallback(() => setResolvedDuplicateHidden(true), []);
  // 文件任务按"附件上下文 -> 用户指令 -> 助手结果"展示，减少阅读跳跃。
  if (turn.role === 'assistant') {
    const duplicateMetadata = turn.metadata?.find((item) => item.type === 'duplicate_upload_review');
    const uploadVersionId = String(duplicateMetadata?.upload_document_version_id ?? '');
    if (uploadVersionId && resolvedDuplicateHidden) {
      // 用户完成选择后整轮确认卡退出页面，避免留下空头像或内部状态占位。
      return null;
    }
    return (
      <section className="chat-turn chat-turn-system">
        <div className="message-row message-row-assistant">
          <div className="avatar avatar-assistant"><Bot size={15} /></div>
          <div className="message-content">
            {uploadVersionId ? (
              <DuplicateUploadReviewLoader
                token={token}
                uploadVersionId={uploadVersionId}
                onResolved={hideResolvedDuplicate}
              />
            ) : turn.response ? (
              <AgentRunReceipt
                taskResult={turn.response.task_result}
                token={token}
                onOpenDocument={onOpenDocument}
                onFollowupResult={onFollowupResult}
                onOperationConfirmed={onOperationConfirmed}
              />
            ) : (
              <p className="agent-chat-response">{turn.userText}</p>
            )}
          </div>
        </div>
      </section>
    );
  }
  const shouldShowUserAttachments = turn.attachments.length > 0 && !isInferredContextFileRequest(turn.userText);

  return (
    <section className="chat-turn">
      <div className="message-row message-row-user">
        <div className="message-content message-content-user">
          {shouldShowUserAttachments && (
            <AttachmentRail
              attachments={turn.attachments}
              layout="stack"
              locked
              onOpen={onOpenAttachment}
              onRestore={onRestoreAttachment}
            />
          )}
          <div className="user-message-bubble">
            {turn.userText}
          </div>
        </div>
      </div>

      <div className="message-row message-row-assistant">
        <div className="avatar avatar-assistant">
          <Bot size={15} />
        </div>

        <div className="message-content">
          {turn.status === 'sending' ? <AgentRunReceipt state="running" /> : null}
          {turn.status === 'failed' ? <AgentRunReceipt state="failed" /> : null}
          {turn.response ? (
            <AgentRunReceipt
              taskResult={turn.response.task_result}
              attachments={turn.attachments}
              token={token}
              onOpenAttachment={onOpenAttachment}
              onOpenDocument={onOpenDocument}
              onOpenManagedFile={onOpenManagedFile}
              onFollowupResult={onFollowupResult}
              onOperationConfirmed={onOperationConfirmed}
            />
          ) : null}
        </div>
      </div>
    </section>
  );
}

function isInferredContextFileRequest(text: string): boolean {
  // 后端会为“之前/上面上传的文件”自动补齐上下文附件；这类附件用于 Agent 执行，不作为本轮上传文件展示。
  const historyReferenceWords = [
    '上面',
    '上文',
    '前面',
    '刚才',
    '刚刚',
    '刚上传',
    '刚才上传',
    '刚才发',
    '刚发',
    '之前',
    '已上传',
    '上传的',
    '所有上传',
  ];
  const fileTaskWords = ['文件', '附件', '文章', '读取', '总结', '讲解', '内容', '分析', '分类', '归类', '重新', '删除', '删掉', '回收站', '恢复'];
  return historyReferenceWords.some((word) => text.includes(word))
    && fileTaskWords.some((word) => text.includes(word));
}
