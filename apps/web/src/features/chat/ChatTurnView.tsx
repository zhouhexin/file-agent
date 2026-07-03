import { Bot } from 'lucide-react';
import { AgentRunReceipt } from './AgentRunReceipt';
import { AttachmentRail } from './AttachmentRail';
import type { ChatAttachment, ChatTurn } from './presentation';

type ChatTurnViewProps = {
  token: string;
  turn: ChatTurn;
  onOpenAttachment: (file: ChatAttachment) => void;
};

export function ChatTurnView({ token, turn, onOpenAttachment }: ChatTurnViewProps) {
  // 文件任务按"附件上下文 -> 用户指令 -> 助手结果"展示，减少阅读跳跃。
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
              agentRun={turn.response.agent_run}
              attachments={turn.attachments}
              token={token}
              onOpenAttachment={onOpenAttachment}
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
  const fileTaskWords = ['文件', '附件', '文章', '读取', '总结', '讲解', '内容', '分析', '分类', '归类', '重新'];
  return historyReferenceWords.some((word) => text.includes(word))
    && fileTaskWords.some((word) => text.includes(word));
}
