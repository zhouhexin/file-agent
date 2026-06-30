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
  return (
    <section className="chat-turn">
      <div className="message-row message-row-user">
        <div className="message-content message-content-user">
          {turn.attachments.length > 0 && (
            <AttachmentRail attachments={turn.attachments} locked onOpen={onOpenAttachment} />
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
