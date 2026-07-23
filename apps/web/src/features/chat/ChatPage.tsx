// 聊天工作台是文件智能体主入口，文件打开动作必须经过后端受控接口。
import { ChangeEvent, FormEvent, KeyboardEvent, useCallback, useEffect, useRef, useState } from 'react';
import { BookOpen, LogOut, MessageSquare, Paperclip, Send, Trash2, User as UserIcon } from 'lucide-react';

import {
  ApiError,
  clearConversationHistory,
  deleteUploadedFile,
  getDuplicateReview,
  getUploadArchiveStatus,
  fetchManagedFileBlob,
  fetchUploadedFileBlob,
  getConversationDetail,
  getFilesystemJob,
  sendAgentMessage,
  uploadFile,
} from '../../api/client';
import { formatError } from '../../api/errors';
import type { ConversationHistoryMessage, DuplicateDecisionResponse, DuplicateReview, ManagedFileResult, User } from '../../types';
import { AttachmentRail } from './AttachmentRail';
import { ChatTurnView } from './ChatTurnView';
import { DuplicateUploadReviewCard } from './DuplicateUploadReviewCard';
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
    response: historyMessage.task_result
      ? {
          message: {
            id: historyMessage.id,
            conversation_id: historyMessage.conversation_id,
            user_id: historyMessage.user_id,
            role: historyMessage.role,
            content: historyMessage.content,
            attachments: historyMessage.attachments.map((file) => ({ document_id: file.document_id })),
          },
          task_result: historyMessage.task_result!,
        }
      : undefined,
    status: 'completed',
    role: historyMessage.role,
    metadata: historyMessage.metadata,
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
  const [duplicateReviews, setDuplicateReviews] = useState<Record<string, DuplicateReview>>({});
  const [chatTurns, setChatTurns] = useState<ChatTurn[]>([]);
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [loadingMoreHistory, setLoadingMoreHistory] = useState(false);
  const [hasMoreHistory, setHasMoreHistory] = useState(false);
  const previewUrls = useRef<Set<string>>(new Set());
  const pageActiveRef = useRef(true);
  const pollingAgentRunsRef = useRef<Set<string>>(new Set());
  const pollingUploadReviewsRef = useRef<Set<string>>(new Set());
  const messageListRef = useRef<HTMLDivElement | null>(null);
  const hasTurns = chatTurns.length > 0;
  const primaryConversationId = getWebConversationId(user.id);
  const [conversationId, setConversationId] = useState(primaryConversationId);

  const clearConversation = async () => {
    // “删除对话”只能清空聊天记录，必须明确告知用户不会删除已上传、归档或整理的文件。
    if (!window.confirm('确定清空当前对话吗？这不会删除任何已上传或已整理的文件。')) {
      return;
    }
    try {
      setError('');
      await clearConversationHistory(token, conversationId);
      setChatTurns([]);
      setDraftAttachments([]);
      setDuplicateReviews({});
      setHasMoreHistory(false);
      setMessage('');
    } catch (err) {
      setError(formatError(err));
    }
  };

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
    pageActiveRef.current = true;
    return () => {
      pageActiveRef.current = false;
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

  useEffect(() => {
    // 页面刷新后也要继续跟踪尚未完成的后台分类任务。
    chatTurns.forEach((turn) => {
      const agentRun = turn.response?.task_result;
      if (
        !agentRun
        || agentRun.task_status !== 'processing'
        || agentRun.pending_job_ids.length === 0
        || pollingAgentRunsRef.current.has(agentRun.task_id)
      ) {
        return;
      }
      pollingAgentRunsRef.current.add(agentRun.task_id);
      void pollAsyncAgentRun({
        turnId: turn.id,
        messageId: turn.response?.message.id ?? turn.id,
        jobIds: agentRun.pending_job_ids,
      }).finally(() => {
        pollingAgentRunsRef.current.delete(agentRun.task_id);
      });
    });
  }, [chatTurns]);

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
    if (submitting || uploading || historyLoading) {
      return;
    }
    const currentMessage = message.trim();
    if (!currentMessage) {
      return;
    }
    setError('');
    setSubmitting(true);
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

  async function pollAsyncAgentRun({
    turnId,
    messageId,
    jobIds,
  }: {
    turnId: string;
    messageId: string;
    jobIds: string[];
  }) {
    // 后台批量任务完成后重新读取服务端 AgentRun，避免前端拼装分类和 ChangeSet。
    while (pageActiveRef.current) {
      try {
        const jobs = await Promise.all(jobIds.map((jobId) => getFilesystemJob(token, jobId)));
        const completed = jobs.every((job) => ['COMPLETED', 'FAILED'].includes(job.status));
        if (completed) {
          const conversation = await getConversationDetail(token, conversationId, { limit: 50 });
          const historyMessage = conversation.messages.find((item) => item.id === messageId);
          if (historyMessage?.task_result) {
            const updatedTaskResult = historyMessage.task_result;
            setChatTurns((current) => current.map((turn) => (
              turn.id === turnId
                ? {
                    ...turn,
                    response: {
                      message: {
                        id: historyMessage.id,
                        conversation_id: historyMessage.conversation_id,
                        user_id: historyMessage.user_id,
                        role: historyMessage.role,
                        content: historyMessage.content,
                        attachments: historyMessage.attachments.map((file) => ({
                          document_id: file.document_id,
                        })),
                      },
                      task_result: updatedTaskResult,
                    },
                    status: 'completed',
                  }
                : turn
            )));
          }
          return;
        }
      } catch (pollError) {
        if (pageActiveRef.current) {
          setError(formatError(pollError));
        }
        return;
      }
      await new Promise((resolve) => window.setTimeout(resolve, 1500));
    }
  }

  function handleComposerKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    // 中文输入法合成期间的 Enter 只用于确认候选词，不能触发消息发送。
    if (event.key !== 'Enter' || event.shiftKey || event.nativeEvent.isComposing) {
      return;
    }
    event.preventDefault();
    if (submitting || uploading || historyLoading || !message.trim()) {
      return;
    }
    event.currentTarget.form?.requestSubmit();
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
      for (const file of files) {
        const uploadedFile = await uploadFile(token, file, conversationId);
        const previewUrl = file.type.startsWith('image/') ? URL.createObjectURL(file) : undefined;
        if (previewUrl) {
          previewUrls.current.add(previewUrl);
        }
        const attachment = {
          ...uploadedFile,
          preview_url: previewUrl,
        };
        // 先把单文件加入状态再启动轮询，避免小文件查重瞬间完成时回写不到附件。
        setDraftAttachments((current) => [...current, attachment]);
        if (uploadedFile.filesystem_job_id && uploadedFile.upload_document_version_id) {
          void pollUploadDuplicateReview(attachment);
        }
      }
    } catch (err) {
      setError(formatError(err));
    } finally {
      setUploading(false);
      event.target.value = '';
    }
  }

  async function pollUploadDuplicateReview(file: ChatAttachment) {
    // 上传请求只入队；前端轮询查重任务，不占用上传 HTTP 连接等待归档或导入。
    const jobId = file.filesystem_job_id;
    const uploadVersionId = file.upload_document_version_id;
    if (!jobId || !uploadVersionId || pollingUploadReviewsRef.current.has(uploadVersionId)) {
      return;
    }
    pollingUploadReviewsRef.current.add(uploadVersionId);
    try {
      while (pageActiveRef.current) {
        const job = await getFilesystemJob(token, jobId);
        if (job.status === 'FAILED') {
          setError(job.error_message || `文件“${file.filename}”查重失败，请稍后重试。`);
          return;
        }
        if (job.status === 'COMPLETED') {
          const review = await getDuplicateReview(token, uploadVersionId);
          setDraftAttachments((current) => current.map((item) => (
            item.upload_document_version_id === uploadVersionId
              ? { ...item, duplicate_review_status: review.status }
              : item
          )));
          if (review.status === 'WAITING_CONFIRMATION') {
            setDuplicateReviews((current) => ({ ...current, [review.id]: review }));
          } else if (review.decision === 'CONTINUE_UPLOAD') {
            void pollUploadArchiveStatus(uploadVersionId, file.filename);
          }
          return;
        }
        await new Promise((resolve) => window.setTimeout(resolve, 1200));
      }
    } catch (err) {
      if (pageActiveRef.current) {
        setError(formatError(err));
      }
    } finally {
      pollingUploadReviewsRef.current.delete(uploadVersionId);
    }
  }

  async function pollUploadArchiveStatus(uploadVersionId: string, filename: string) {
    // 归档完成不等于工作副本已创建；直到 working_copy_id 出现才结束状态跟踪。
    while (pageActiveRef.current) {
      try {
        const archive = await getUploadArchiveStatus(token, uploadVersionId);
        setDraftAttachments((current) => current.map((item) => (
          item.upload_document_version_id === uploadVersionId
            ? {
                ...item,
                archive_status: archive.status,
                working_copy_id: archive.working_copy_id,
              }
            : item
        )));
        if (archive.status === 'FAILED') {
          setError(archive.error_message || `文件“${filename}”归档失败，系统将按策略重试。`);
          return;
        }
        if (archive.status === 'ARCHIVED' && archive.working_copy_id) {
          return;
        }
        if (['CANCELLED', 'EXISTING_FILE_SELECTED'].includes(archive.status)) {
          return;
        }
        await new Promise((resolve) => window.setTimeout(resolve, 1500));
      } catch (err) {
        if (pageActiveRef.current) setError(formatError(err));
        return;
      }
    }
  }

  function resolveDuplicateReview(result: DuplicateDecisionResponse) {
    // 用户决策按文件生效；取消或使用已有文件不会影响同批其他附件。
    const review = result.review;
    setDuplicateReviews((current) => {
      const next = { ...current };
      delete next[review.id];
      return next;
    });
    if (review.decision === 'CANCEL_UPLOAD') {
      setDraftAttachments((current) => current.filter(
        (item) => item.upload_document_version_id !== review.upload_document_version_id,
      ));
      return;
    }
    if (review.decision === 'USE_EXISTING_FILE' && result.selected_existing_document_id) {
      const selectedCandidate = review.candidates.find(
        (candidate) => candidate.existing_document_id === result.selected_existing_document_id,
      );
      setDraftAttachments((current) => current.map((item) => (
        item.upload_document_version_id === review.upload_document_version_id
          ? {
              ...item,
              document_id: result.selected_existing_document_id as string,
              filename: String(selectedCandidate?.summary.filename ?? item.filename),
              status: 'WORKING_COPY',
              archive_status: result.archive_status,
              duplicate_review_status: 'RESOLVED',
            }
          : item
      )));
      return;
    }
    setDraftAttachments((current) => current.map((item) => (
      item.upload_document_version_id === review.upload_document_version_id
        ? { ...item, archive_status: result.archive_status, duplicate_review_status: 'RESOLVED' }
        : item
    )));
    if (review.decision === 'CONTINUE_UPLOAD') {
      void pollUploadArchiveStatus(review.upload_document_version_id, review.filename);
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
      setDuplicateReviews((current) => Object.fromEntries(
        Object.entries(current).filter(([, review]) => review.document_id !== documentId),
      ));
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

  async function openSearchDocument(documentId: string, filename: string) {
    // 全局检索结果不携带路径或存储位置，只允许用后端再次鉴权的稳定 document_id 打开。
    await openAttachment({
      document_id: documentId,
      filename,
      size_bytes: 0,
      content_type: 'application/octet-stream',
      sha256: '',
      status: 'READY',
      ingest_status: 'INGESTED',
      deduplicated: false,
    });
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
          <button
            className="sidebar-menu-item"
            type="button"
            onClick={() => void clearConversation()}
            disabled={historyLoading || submitting || chatTurns.length === 0}
            title="仅清空聊天记录，不删除文件"
          >
            <Trash2 size={16} />
            <span>清空对话</span>
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
                  onOpenDocument={openSearchDocument}
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
            {Object.values(duplicateReviews).map((review) => (
              <DuplicateUploadReviewCard
                key={review.id}
                token={token}
                review={review}
                onResolved={resolveDuplicateReview}
              />
            ))}
            <textarea
              value={message}
              onChange={(event) => setMessage(event.target.value)}
              onKeyDown={handleComposerKeyDown}
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
