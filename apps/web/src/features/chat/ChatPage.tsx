import { ChangeEvent, FormEvent, useEffect, useRef, useState } from 'react';
import { LogOut, MessageSquare, Paperclip, Send, User as UserIcon } from 'lucide-react';

import {
  ApiError,
  deleteUploadedFile,
  fetchUploadedFileBlob,
  getConversationDetail,
  sendAgentMessage,
  uploadFile,
} from '../../api/client';
import { formatError } from '../../api/errors';
import type { User } from '../../types';
import { AttachmentRail } from './AttachmentRail';
import { ChatTurnView } from './ChatTurnView';
import { canPreviewInBrowser } from './presentation';
import type { ChatAttachment, ChatTurn } from './presentation';

const WEB_CONVERSATION_ID = 'web-chat';

type ChatPageProps = {
  token: string;
  user: User;
  onLogout: () => void;
};

export function ChatPage({ token, user, onLogout }: ChatPageProps) {
  // ChatPage 管理对话工作台状态；具体展示交给 features/chat 下的展示组件。
  const [message, setMessage] = useState('');
  const [draftAttachments, setDraftAttachments] = useState<ChatAttachment[]>([]);
  const [chatTurns, setChatTurns] = useState<ChatTurn[]>([]);
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(true);
  const previewUrls = useRef<Set<string>>(new Set());
  const hasTurns = chatTurns.length > 0;

  useEffect(() => {
    // 页面卸载时统一释放仍在展示的图片预览 object URL。
    return () => {
      previewUrls.current.forEach((url) => {
        URL.revokeObjectURL(url);
      });
    };
  }, []);

  useEffect(() => {
    // 工作台启动时恢复固定调试会话的历史记录；404 表示该用户还没有历史会话。
    let cancelled = false;
    setHistoryLoading(true);
    getConversationDetail(token, WEB_CONVERSATION_ID)
      .then((conversation) => {
        if (cancelled) {
          return;
        }
        setChatTurns(conversation.messages.map((historyMessage) => ({
          id: historyMessage.id,
          userText: historyMessage.content,
          attachments: historyMessage.attachments,
          response: historyMessage.agent_run
            ? {
                message: {
                  id: historyMessage.id,
                  conversation_id: historyMessage.conversation_id,
                  user_id: historyMessage.user_id,
                  role: historyMessage.role,
                  content: historyMessage.content,
                  attachments: historyMessage.attachments.map((file) => ({ document_id: file.document_id })),
                },
                agent_run: historyMessage.agent_run,
              }
            : undefined,
          status: 'completed',
        })));
      })
      .catch((err) => {
        if (cancelled) {
          return;
        }
        if (err instanceof ApiError && err.status === 404) {
          return;
        }
        setError(formatError(err));
      })
      .finally(() => {
        if (!cancelled) {
          setHistoryLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError('');
    setSubmitting(true);
    const currentMessage = message.trim();
    const attachmentsForTurn = draftAttachments;
    const turnId = crypto.randomUUID();

    setChatTurns((current) => [
      ...current,
      {
        id: turnId,
        userText: currentMessage,
        attachments: attachmentsForTurn,
        status: 'sending',
      },
    ]);
    setMessage('');
    setDraftAttachments([]);

    try {
      const result = await sendAgentMessage(
        token,
        WEB_CONVERSATION_ID,
        currentMessage,
        attachmentsForTurn.map((file) => file.document_id),
      );
      setChatTurns((current) => current.map((turn) => (
        turn.id === turnId ? { ...turn, response: result, status: 'completed' } : turn
      )));
    } catch (err) {
      setChatTurns((current) => current.map((turn) => (
        turn.id === turnId ? { ...turn, status: 'failed' } : turn
      )));
      setError(formatError(err));
    } finally {
      setSubmitting(false);
    }
  }

  async function handleFileChange(event: ChangeEvent<HTMLInputElement>) {
    // 选择文件后立即上传，发送消息时只引用后端返回的 document_id。
    const files = Array.from(event.target.files ?? []);
    if (files.length === 0) {
      return;
    }

    setError('');
    setUploading(true);
    try {
      const results: ChatAttachment[] = [];
      for (const file of files) {
        const uploadedFile = await uploadFile(token, file);
        const previewUrl = file.type.startsWith('image/') ? URL.createObjectURL(file) : undefined;
        if (previewUrl) {
          previewUrls.current.add(previewUrl);
        }
        results.push({
          ...uploadedFile,
          preview_url: previewUrl,
        });
      }
      setDraftAttachments((current) => [...current, ...results]);
    } catch (err) {
      setError(formatError(err));
    } finally {
      setUploading(false);
      event.target.value = '';
    }
  }

  async function removeDraftAttachment(documentId: string) {
    // 发送前删除会同步删除后端文件；发送后的附件不走这个入口。
    setError('');
    setDraftAttachments((current) => current.map((file) => (
      file.document_id === documentId ? { ...file, deleting: true } : file
    )));

    try {
      await deleteUploadedFile(token, documentId);
      setDraftAttachments((current) => {
        const removedFile = current.find((file) => file.document_id === documentId);
        if (removedFile?.preview_url) {
          URL.revokeObjectURL(removedFile.preview_url);
          previewUrls.current.delete(removedFile.preview_url);
        }
        return current.filter((file) => file.document_id !== documentId);
      });
    } catch (err) {
      setDraftAttachments((current) => current.map((file) => (
        file.document_id === documentId ? { ...file, deleting: false } : file
      )));
      setError(formatError(err));
    }
  }

  async function openAttachment(file: ChatAttachment) {
    // 附件内容通过鉴权接口取回 Blob，再交给浏览器预览或下载。
    setError('');
    try {
      const blob = await fetchUploadedFileBlob(token, file.document_id);
      const objectUrl = URL.createObjectURL(blob);
      previewUrls.current.add(objectUrl);
      if (canPreviewInBrowser(file)) {
        window.open(objectUrl, '_blank', 'noopener,noreferrer');
      } else {
        const link = document.createElement('a');
        link.href = objectUrl;
        link.download = file.filename;
        document.body.appendChild(link);
        link.click();
        link.remove();
      }
      window.setTimeout(() => {
        URL.revokeObjectURL(objectUrl);
        previewUrls.current.delete(objectUrl);
      }, 60_000);
    } catch (err) {
      setError(formatError(err));
    }
  }

  return (
    <main className="app-shell">
      <header className="topbar">
        <div className="topbar-title">
          <MessageSquare size={22} />
          <span>File Agent</span>
        </div>
        <div className="user-box">
          <UserIcon size={18} />
          <span>{user.display_name || user.username}</span>
          <button className="icon-button" type="button" onClick={onLogout} title="退出登录">
            <LogOut size={18} />
          </button>
        </div>
      </header>

      <section className={hasTurns ? 'workspace conversation-mode' : 'workspace empty-mode'}>
        <div className="chat-column">
          {!hasTurns ? (
            <div className="empty-chat-heading">
              <h2>有什么我能帮你的吗？</h2>
              <p>上传图片或文件后，直接用自然语言描述你要完成的工作。</p>
            </div>
          ) : (
            <div className="message-list">
              {chatTurns.map((turn) => (
                <ChatTurnView
                  key={turn.id}
                  token={token}
                  turn={turn}
                  onOpenAttachment={openAttachment}
                />
              ))}
            </div>
          )}

          <form className={hasTurns ? 'composer docked-composer' : 'composer center-composer'} onSubmit={submit}>
            <AttachmentRail
              attachments={draftAttachments}
              layout="rail"
              onOpen={openAttachment}
              onRemove={removeDraftAttachment}
            />
            <textarea
              value={message}
              onChange={(event) => setMessage(event.target.value)}
              rows={1}
              required
            />
            <div className="composer-actions">
              <label className="file-picker">
                <Paperclip size={18} />
                <span>{uploading ? '上传中...' : '选择文件'}</span>
                <input
                  accept="image/*,.pdf,.doc,.docx,.xls,.xlsx,.txt,.md,.csv"
                  disabled={uploading}
                  multiple
                  type="file"
                  onChange={handleFileChange}
                />
              </label>
              <button className="primary-button send-button" disabled={submitting || uploading || historyLoading} type="submit">
                <Send size={18} />
                {submitting ? '发送中...' : historyLoading ? '加载中...' : '发送'}
              </button>
            </div>
          </form>

          {error ? <p className="form-message error">{error}</p> : null}
        </div>
      </section>
    </main>
  );
}
