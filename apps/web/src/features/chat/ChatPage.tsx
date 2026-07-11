// 聊天工作台是文件智能体主入口，文件打开动作必须经过后端受控接口。
import { ChangeEvent, FormEvent, useCallback, useEffect, useRef, useState } from 'react';
import { BookOpen, LogOut, MessageSquare, Paperclip, Send, User as UserIcon } from 'lucide-react';

import {
  ApiError,
  deleteUploadedFile,
  fetchManagedFileBlob,
  fetchUploadedFileBlob,
  getConversationDetail,
  sendAgentMessage,
  uploadFile,
} from '../../api/client';
import { formatError } from '../../api/errors';
import type { ConversationHistoryMessage, ManagedFileResult, User } from '../../types';
import { AttachmentRail } from './AttachmentRail';
import { ChatTurnView } from './ChatTurnView';
import { canPreviewFileInfo, canPreviewInBrowser } from './presentation';
import type { ChatAttachment, ChatTurn } from './presentation';

function getWebConversationId(userId: string): string {
  // conversations.id 当前限制为 36 位；保留用户隔离，同时避免超过数据库字段长度。
  return `chat-${userId.replace(/-/g, '').slice(0, 31)}`;
}

function getLegacyWebConversationId(): string {
  // 兼容早期版本统一写入的 Web 会话，避免升级后用户看不到历史消息。
  return 'web-chat';
}

function createClientId(): string {
  // 旧浏览器或非安全上下文可能没有 crypto.randomUUID，这里提供本地临时 ID 兜底。
  const browserCrypto = globalThis.crypto;
  if (browserCrypto?.randomUUID) {
    return browserCrypto.randomUUID();
  }
  if (browserCrypto?.getRandomValues) {
    const bytes = browserCrypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }
  return `turn-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

type ChatPageProps = {
  token: string;
  user: User;
  onLogout: () => void;
  onOpenOnboarding: () => void;
  initialDraft?: string;
};

const HISTORY_PAGE_SIZE = 10;

function historyMessagesToTurns(messages: ConversationHistoryMessage[]): ChatTurn[] {
  // 后端已保证分页消息按时间正序返回，前端只负责转换为聊天展示结构。
  return messages.map((historyMessage) => ({
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
  }));
}

export function ChatPage({
  token,
  user,
  onLogout,
  onOpenOnboarding,
  initialDraft,
}: ChatPageProps) {
  // ChatPage 管理对话工作台状态；具体展示交给 features/chat 下的展示组件。
  const [message, setMessage] = useState('');
  const [draftAttachments, setDraftAttachments] = useState<ChatAttachment[]>([]);
  const [chatTurns, setChatTurns] = useState<ChatTurn[]>([]);
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [loadingMoreHistory, setLoadingMoreHistory] = useState(false);
  const [hasMoreHistory, setHasMoreHistory] = useState(false);
  const previewUrls = useRef<Set<string>>(new Set());
  const messageListRef = useRef<HTMLDivElement | null>(null);
  const hasTurns = chatTurns.length > 0;
  const primaryConversationId = getWebConversationId(user.id);
  const [conversationId, setConversationId] = useState(primaryConversationId);

  const scrollMessageListToBottom = useCallback(() => {
    requestAnimationFrame(() => {
      const messageList = messageListRef.current;
      if (messageList) {
        messageList.scrollTop = messageList.scrollHeight;
      }
    });
  }, []);

  useEffect(() => {
    // 引导页跳转过来时携带示例问题，直接填入输入框。
    if (initialDraft) {
      setMessage(initialDraft);
    }
  }, [initialDraft]);

  useEffect(() => {
    // 页面卸载时统一释放仍在展示的图片预览 object URL。
    return () => {
      previewUrls.current.forEach((url) => {
        URL.revokeObjectURL(url);
      });
    };
  }, []);

  useEffect(() => {
    // 工作台启动时恢复当前用户自己的 Web 会话；新 ID 没有历史时兼容读取旧版 web-chat。
    let cancelled = false;
    setHistoryLoading(true);
    setHasMoreHistory(false);
    setConversationId(primaryConversationId);
    getConversationDetail(token, primaryConversationId, { limit: HISTORY_PAGE_SIZE })
      .catch((err) => {
        if (err instanceof ApiError && err.status === 404) {
          return getConversationDetail(token, getLegacyWebConversationId(), { limit: HISTORY_PAGE_SIZE })
            .then((conversation) => {
              setConversationId(conversation.id);
              return conversation;
            })
            .catch((legacyErr) => {
              if (legacyErr instanceof ApiError && [403, 404].includes(legacyErr.status)) {
                return null;
              }
              throw legacyErr;
            });
        }
        throw err;
      })
      .then((conversation) => {
        if (cancelled) {
          return;
        }
        if (!conversation) {
          setChatTurns([]);
          setHasMoreHistory(false);
          return;
        }
        setChatTurns(historyMessagesToTurns(conversation.messages));
        setHasMoreHistory(conversation.pagination.has_more);
        scrollMessageListToBottom();
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
  }, [primaryConversationId, scrollMessageListToBottom, token]);

  const loadOlderHistory = useCallback(async () => {
    const beforeMessageId = chatTurns[0]?.id;
    const messageList = messageListRef.current;
    if (!beforeMessageId || !hasMoreHistory || loadingMoreHistory || historyLoading) {
      return;
    }

    const previousHeight = messageList?.scrollHeight ?? 0;
    const previousTop = messageList?.scrollTop ?? 0;
    setLoadingMoreHistory(true);
    setError('');
    try {
      const conversation = await getConversationDetail(token, conversationId, {
        limit: HISTORY_PAGE_SIZE,
        beforeMessageId,
      });
      const olderTurns = historyMessagesToTurns(conversation.messages);
      setChatTurns((current) => {
        const existingIds = new Set(current.map((turn) => turn.id));
        return [
          ...olderTurns.filter((turn) => !existingIds.has(turn.id)),
          ...current,
        ];
      });
      setHasMoreHistory(conversation.pagination.has_more);
    } catch (err) {
      setError(formatError(err));
    } finally {
      setLoadingMoreHistory(false);
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          const nextMessageList = messageListRef.current;
          if (nextMessageList) {
            nextMessageList.scrollTop = nextMessageList.scrollHeight - previousHeight + previousTop;
          }
        });
      });
    }
  }, [chatTurns, conversationId, hasMoreHistory, historyLoading, loadingMoreHistory, token]);

  function handleMessageListScroll() {
    const messageList = messageListRef.current;
    if (!messageList || messageList.scrollTop > 80) {
      return;
    }
    void loadOlderHistory();
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError('');
    setSubmitting(true);
    const currentMessage = message.trim();
    const attachmentsForTurn = draftAttachments;
    const turnId = createClientId();

    setChatTurns((current) => [
      ...current,
      {
        id: turnId,
        userText: currentMessage,
        attachments: attachmentsForTurn,
        status: 'sending',
      },
    ]);
    scrollMessageListToBottom();
    setMessage('');
    setDraftAttachments([]);

    try {
      const result = await sendAgentMessage(
        token,
        conversationId,
        currentMessage,
        attachmentsForTurn.map((file) => file.document_id),
      );
      setChatTurns((current) => current.map((turn) => (
        turn.id === turnId ? { ...turn, response: result, status: 'completed' } : turn
      )));
      scrollMessageListToBottom();
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
    if (file.status === 'MISSING') {
      setError('原始文件已不存在，无法打开附件。');
      return;
    }
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
      setError(err instanceof ApiError && err.status === 404
        ? '原始文件已不存在，无法打开附件。'
        : formatError(err));
    }
  }

  async function openManagedFile(file: ManagedFileResult) {
    // 受管文件复用 Blob 预览流程；后端只接受 root_key + relative_path，不暴露真实路径。
    setError('');
    if (file.status === 'MISSING') {
      setError('文件已不存在，无法预览。');
      return;
    }
    try {
      const blob = await fetchManagedFileBlob(token, file.root_key, file.relative_path);
      const objectUrl = URL.createObjectURL(blob);
      previewUrls.current.add(objectUrl);
      if (canPreviewFileInfo(file.filename, blob.type || 'application/octet-stream')) {
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
        </div>
      </header>

      <section className={hasTurns ? 'workspace conversation-mode' : 'workspace empty-mode'}>
        <aside className="chat-sidebar" aria-label="聊天功能菜单">
          <button
            className="sidebar-menu-item"
            type="button"
            onClick={onOpenOnboarding}
          >
            <BookOpen size={16} />
            <span>功能介绍</span>
          </button>
          {/*<button*/}
          {/*  className="sidebar-menu-item"*/}
          {/*  type="button"*/}
          {/*  onClick={onLogout}*/}
          {/*>*/}
          {/*  <LogOut size={18} />*/}
          {/*  <span>退出登录</span>*/}
          {/*</button>*/}
        </aside>
        <div className="chat-column">
          {historyLoading && !hasTurns ? (
            <div className="chat-initial-loading" aria-label="正在加载对话">
              <div className="chat-loading-spinner" />
            </div>
          ) : !hasTurns ? (
            <div className="empty-chat-heading">
              <h2>有什么我能帮你的吗？</h2>
              <p>上传图片或文件后，直接用自然语言描述你要完成的工作。</p>
            </div>
          ) : (
            <div
              ref={messageListRef}
              className="message-list"
              onScroll={handleMessageListScroll}
            >
              {loadingMoreHistory ? (
                <div className="chat-history-loading" aria-live="polite">
                  <span className="chat-loading-spinner chat-loading-spinner-small" />
                  <span>正在加载更早的消息</span>
                </div>
              ) : null}
              {chatTurns.map((turn) => (
                <ChatTurnView
                  key={turn.id}
                  token={token}
                  turn={turn}
                  onOpenAttachment={openAttachment}
                  onOpenManagedFile={openManagedFile}
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
              disabled={historyLoading}
              placeholder={historyLoading ? '正在加载对话...' : ''}
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
