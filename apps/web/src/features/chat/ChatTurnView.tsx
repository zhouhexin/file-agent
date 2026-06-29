import { AgentRunReceipt } from './AgentRunReceipt';
import { AttachmentRail } from './AttachmentRail';
import type { ChatAttachment, ChatTurn } from './presentation';

type ChatTurnViewProps = {
  token: string;
  turn: ChatTurn;
  onOpenAttachment: (file: ChatAttachment) => void;
};

export function ChatTurnView({ token, turn, onOpenAttachment }: ChatTurnViewProps) {
  // 文件任务按“附件 -> 指令 -> 处理回执”展示，比普通聊天气泡更适合批量文件处理。
  return (
    <section className="chat-turn">
      <AttachmentRail attachments={turn.attachments} locked onOpen={onOpenAttachment} />

      <div className="task-bubble">
        {turn.userText}
      </div>

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
    </section>
  );
}
