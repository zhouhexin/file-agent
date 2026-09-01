// 聊天工作台是文件智能体主入口，文件打开动作必须经过后端受控接口。
import { ChangeEvent, FormEvent, KeyboardEvent, useCallback, useEffect, useRef, useState } from 'react';
import { Activity, AlertTriangle, BookOpen, FolderTree, Lightbulb, LogOut, MessageSquare, Paperclip, Send, Trash2, User as UserIcon } from 'lucide-react';

import {
  ApiError,
  clearConversationHistory,
  deleteUploadedFile,
  getDuplicateReview,
  getUploadArchiveStatus,
  fetchManagedFileBlob,
  fetchUploadedFileBlob,
  getFilePreview,
  getConversationDetail,
  getFilesystemJob,
  sendAgentMessage,
  uploadFile,
} from '../../api/client';
import { formatError } from '../../api/errors';
import type {
  ConversationHistoryMessage,
  DuplicateDecisionResponse,
  DuplicateReview,
  FilePreviewResponse,
  ManagedFileResult,
  SendMessageResponse,
  User,
} from '../../types';
import { AttachmentRail } from './AttachmentRail';
import {
  buildUploadOrganizationInstruction,
  getSelectedFileRelativePath,
  inferSelectedFolderName,
} from './batchUpload';
import type { UploadBatchProgressState } from './batchUpload';
import { ChatTurnView } from './ChatTurnView';
import { DuplicateUploadReviewCard } from './DuplicateUploadReviewCard';
import { DocumentPreviewDialog } from './DocumentPreviewDialog';
import {
  canPreviewFileInfo,
  canPreviewInBrowser,
  deduplicateAttachmentsByDocumentId,
  hasUnresolvedUploadReview,
  isVisibleConversationHistoryMessage,
} from './presentation';
import type { ChatAttachment, ChatTurn } from './presentation';
import { UploadBatchProgress } from './UploadBatchProgress';

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
  onOpenFiles: () => void;
  onOpenFailedFiles: () => void;
  onOpenAgentRuns: () => void;
  onOpenCapabilitySuggestions: () => void;
  initialDraft?: string;
};

const HISTORY_PAGE_SIZE = 10;

type PendingUploadOrganization = {
  batchId: string;
  folderName: string;
  attachmentKeys: string[];
};

function getAttachmentUploadKey(file: ChatAttachment): string {
  // 重复文件选择可能替换 document_id，因此用上传版本 ID 稳定追踪文件夹批次中的原始条目。
  return file.upload_document_version_id || file.document_id;
}

function historyMessagesToTurns(messages: ConversationHistoryMessage[]): ChatTurn[] {
  // 后端已保证分页消息按时间正序返回。转换前再过滤内部审计角色和旧生命周期文案，
  // 防止旧 API 进程或历史数据把后台状态插到用户上传文件卡片之前。
  return messages.filter(isVisibleConversationHistoryMessage).map((historyMessage) => {
    const attachments = deduplicateAttachmentsByDocumentId(historyMessage.attachments);
    return {
      id: historyMessage.id,
      userText: historyMessage.content,
      attachments,
      response: historyMessage.task_result
        ? {
            message: {
              id: historyMessage.id,
              conversation_id: historyMessage.conversation_id,
              user_id: historyMessage.user_id,
              role: historyMessage.role,
              content: historyMessage.content,
              attachments: attachments.map((file) => ({ document_id: file.document_id })),
            },
            task_result: historyMessage.task_result!,
          }
        : undefined,
      status: 'completed' as const,
      role: historyMessage.role,
      metadata: historyMessage.metadata,
    };
  });
}

export function ChatPage({
  token,
  user,
  onLogout,
  onOpenOnboarding,
  onOpenFiles,
  onOpenFailedFiles,
  onOpenAgentRuns,
  onOpenCapabilitySuggestions,
  initialDraft,
}: ChatPageProps) {
  // ChatPage 管理对话工作台状态；具体展示交给 features/chat 下的展示组件。
  const [message, setMessage] = useState('');
  const [draftAttachments, setDraftAttachments] = useState<ChatAttachment[]>([]);
  const [duplicateReviews, setDuplicateReviews] = useState<Record<string, DuplicateReview>>({});
  const [chatTurns, setChatTurns] = useState<ChatTurn[]>([]);
  const [error, setError] = useState('');
  const [documentPreview, setDocumentPreview] = useState<FilePreviewResponse | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadBatch, setUploadBatch] = useState<UploadBatchProgressState | null>(null);
  const [pendingUploadOrganization, setPendingUploadOrganization] = useState<PendingUploadOrganization | null>(null);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [loadingMoreHistory, setLoadingMoreHistory] = useState(false);
  const [hasMoreHistory, setHasMoreHistory] = useState(false);
  const previewUrls = useRef<Set<string>>(new Set());
  const pageActiveRef = useRef(true);
  const pollingAgentRunsRef = useRef<Set<string>>(new Set());
  const pollingUploadReviewsRef = useRef<Set<string>>(new Set());
  const messageListRef = useRef<HTMLDivElement | null>(null);
  const folderInputRef = useRef<HTMLInputElement | null>(null);
  const hasTurns = chatTurns.length > 0;
  const waitingForDuplicateResolution = hasUnresolvedUploadReview(draftAttachments)
    || Object.keys(duplicateReviews).length > 0;
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
      setUploadBatch(null);
      setPendingUploadOrganization(null);
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
    // React 类型尚未标准化目录选择属性；DOM 属性仍由 Chromium/Edge 等浏览器原生执行。
    folderInputRef.current?.setAttribute('webkitdirectory', '');
    folderInputRef.current?.setAttribute('directory', '');
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
        || pollingAgentRunsRef.current.has(agentRun.task_id)
      ) {
        return;
      }
      pollingAgentRunsRef.current.add(agentRun.task_id);
      void pollAsyncAgentRun({
        turnId: turn.id,
        messageId: turn.response?.message.id ?? turn.id,
      }).finally(() => {
        pollingAgentRunsRef.current.delete(agentRun.task_id);
      });
    });
  }, [chatTurns]);

  useEffect(() => {
    // 自动整理任务完成后，同步关闭批次进度；逐文件明细继续由树形回执展示。
    if (!uploadBatch?.agentRunId) return;
    const task = chatTurns
      .map((turn) => turn.response?.task_result)
      .find((item) => item?.task_id === uploadBatch.agentRunId);
    if (!task || task.task_status === 'processing') return;
    setUploadBatch((current) => current?.agentRunId === task.task_id ? {
      ...current,
      status: task.task_status === 'failed' ? 'failed' : 'completed',
    } : current);
  }, [chatTurns, uploadBatch?.agentRunId]);

  useEffect(() => {
    // 文件夹批次必须等每个查重项完成或明确失败后再自动分类；失败项不会阻塞成功文件。
    const pending = pendingUploadOrganization;
    if (!pending || uploading || submitting) {
      return;
    }
    const batchKeys = new Set(pending.attachmentKeys);
    const batchAttachments = draftAttachments.filter(
      (file) => batchKeys.has(getAttachmentUploadKey(file)),
    );
    const attachmentByKey = new Map(
      batchAttachments.map((file) => [getAttachmentUploadKey(file), file]),
    );
    const allSettled = pending.attachmentKeys.every((key) => {
      const attachment = attachmentByKey.get(key);
      // 重复确认中取消的文件已从草稿移除，也属于明确结束，不应阻塞同批其他文件。
      if (!attachment || attachment.duplicate_review_status === 'FAILED') return true;
      if (attachment.duplicate_review_status !== 'RESOLVED') return false;
      if (attachment.archive_status === 'EXISTING_FILE_SELECTED') return true;
      return ['COMPLETED', 'FAILED'].includes(attachment.processing_status || '');
    });
    if (!allSettled) {
      return;
    }

    const eligibleAttachments = deduplicateAttachmentsByDocumentId(
      batchAttachments.filter((file) => file.duplicate_review_status === 'RESOLVED'),
    );
    setPendingUploadOrganization(null);
    if (eligibleAttachments.length === 0) {
      setUploadBatch((current) => current?.id === pending.batchId ? {
        ...current,
        status: 'failed',
      } : current);
      return;
    }

    setUploadBatch((current) => current?.id === pending.batchId ? {
      ...current,
      status: 'submitting',
    } : current);
    const instruction = buildUploadOrganizationInstruction(
      pending.folderName,
      eligibleAttachments.length,
    );
    void sendTask(
      instruction,
      eligibleAttachments,
      new Set(eligibleAttachments.map((file) => file.document_id)),
      false,
    ).then((sent) => {
      if (!pageActiveRef.current) return;
      setUploadBatch((current) => current?.id === pending.batchId ? {
        ...current,
        agentRunId: sent?.task_result.task_id,
        status: sent
          ? sent.task_result.task_status === 'completed'
            ? 'completed'
            : sent.task_result.task_status === 'failed'
              ? 'failed'
              : 'submitted'
          : 'failed',
      } : current);
    });
  }, [draftAttachments, pendingUploadOrganization, submitting, uploading]);

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
    if (submitting || uploading || historyLoading || waitingForDuplicateResolution) {
      if (waitingForDuplicateResolution) {
        setError('请先完成附件的重复文件确认，再发送任务。');
      }
      return;
    }
    const currentMessage = message.trim();
    if (!currentMessage) {
      return;
    }
    // 发送前再次按 document_id 收敛，防止重复上传确认把新卡片替换成已有文件后，
    // 同一文件在请求和聊天流中出现两次。
    const attachmentsForTurn = deduplicateAttachmentsByDocumentId(draftAttachments);
    await sendTask(currentMessage, attachmentsForTurn, new Set(attachmentsForTurn.map((file) => file.document_id)));
  }

  async function sendTask(
    currentMessage: string,
    attachmentsForTurn: ChatAttachment[],
    clearDraftDocumentIds: Set<string>,
    clearComposer = true,
  ): Promise<SendMessageResponse | null> {
    // 手动任务和文件夹默认分类共用完全相同的消息、AgentRun、回执和异步轮询链路。
    setError('');
    setSubmitting(true);
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
    if (clearComposer) {
      setMessage('');
    }
    setDraftAttachments((current) => current.filter(
      (file) => !clearDraftDocumentIds.has(file.document_id),
    ));

    try {
      const result = await sendAgentMessage(
        token,
        conversationId,
        currentMessage,
        attachmentsForTurn.map((file) => ({
          document_id: file.document_id,
          relative_path: file.relative_path,
        })),
      );
      setChatTurns((current) => current.map((turn) => (
        turn.id === turnId ? { ...turn, response: result, status: 'completed' } : turn
      )));
      scrollMessageListToBottom();
      return result;
    } catch (err) {
      setChatTurns((current) => current.map((turn) => (
        turn.id === turnId ? { ...turn, status: 'failed' } : turn
      )));
      setError(formatError(err));
      // 提交失败时把本批附件放回草稿，用户无需重新选择整个文件夹。
      setDraftAttachments((current) => deduplicateAttachmentsByDocumentId([
        ...attachmentsForTurn,
        ...current,
      ]));
      return null;
    } finally {
      setSubmitting(false);
    }
  }

  async function pollAsyncAgentRun({
    turnId,
    messageId,
  }: {
    turnId: string;
    messageId: string;
  }) {
    // 普通前端只轮询原消息任务投影，不读取 filesystem job 类型、队列或任务编号。
    // worker 会在内部依赖完成后更新同一个 AgentRun，避免向用户暴露“待准备”阶段。
    while (pageActiveRef.current) {
      try {
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
          if (updatedTaskResult.task_status !== 'processing') {
            return;
          }
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
    if (submitting || uploading || historyLoading || waitingForDuplicateResolution || !message.trim()) {
      return;
    }
    event.currentTarget.form?.requestSubmit();
  }

  async function handleFileChange(
    event: ChangeEvent<HTMLInputElement>,
    mode: 'files' | 'folder' = 'files',
  ) {
    // 批量选择仍逐个复用单文件上传服务；单项失败被隔离，不中断同批其余文件。
    const files = Array.from(event.target.files ?? []);
    if (files.length === 0) {
      return;
    }

    const batchId = createClientId();
    const folderName = mode === 'folder' ? inferSelectedFolderName(files) : undefined;
    setError('');
    setUploading(true);
    setUploadBatch({
      id: batchId,
      mode,
      folderName,
      total: files.length,
      completed: 0,
      processed: 0,
      succeeded: 0,
      failed: 0,
      failures: [],
      status: 'uploading',
    });
    const uploadedAttachments: ChatAttachment[] = [];
    for (const file of files) {
      const relativePath = mode === 'folder' ? getSelectedFileRelativePath(file) : file.name;
      try {
        const uploadedFile = await uploadFile(token, file, conversationId, relativePath);
        const previewUrl = file.type.startsWith('image/') ? URL.createObjectURL(file) : undefined;
        if (previewUrl) {
          previewUrls.current.add(previewUrl);
        }
        const attachment = {
          ...uploadedFile,
          preview_url: previewUrl,
        };
        uploadedAttachments.push(attachment);
        // 先把单文件加入状态再启动轮询，避免小文件查重瞬间完成时回写不到附件。
        setDraftAttachments((current) => deduplicateAttachmentsByDocumentId([...current, attachment]));
        setUploadBatch((current) => current?.id === batchId ? {
          ...current,
          completed: current.completed + 1,
          succeeded: current.succeeded + 1,
        } : current);
        if (uploadedFile.filesystem_job_id && uploadedFile.upload_document_version_id) {
          void pollUploadDuplicateReview(attachment, batchId);
        }
      } catch (err) {
        const failureMessage = formatError(err);
        setUploadBatch((current) => current?.id === batchId ? {
          ...current,
          completed: current.completed + 1,
          processed: current.processed + 1,
          failed: current.failed + 1,
          failures: [...current.failures, { relativePath, message: failureMessage }],
        } : current);
      }
    }
    setUploading(false);
    event.target.value = '';

    if (uploadedAttachments.length > 0) {
      setPendingUploadOrganization({
        batchId,
        folderName: folderName || (uploadedAttachments.length === 1 ? uploadedAttachments[0].filename : '本次上传'),
        attachmentKeys: uploadedAttachments.map(getAttachmentUploadKey),
      });
      setUploadBatch((current) => current?.id === batchId ? {
        ...current,
        status: 'waiting_review',
      } : current);
    } else {
      setUploadBatch((current) => current?.id === batchId ? {
        ...current,
        status: uploadedAttachments.length > 0 ? 'completed' : 'failed',
      } : current);
    }
  }

  async function pollUploadDuplicateReview(file: ChatAttachment, batchId: string) {
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
          setDraftAttachments((current) => current.map((item) => (
            item.upload_document_version_id === uploadVersionId
              ? {
                  ...item,
                  duplicate_review_status: 'FAILED',
                  upload_error: job.error_message || '重复文件检查失败',
                }
              : item
          )));
          setError(job.error_message || `文件“${file.filename}”查重失败，请稍后重试。`);
          markBatchFileFailed(batchId, file.relative_path || file.filename, job.error_message || '重复文件检查失败');
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
            void pollUploadArchiveStatus(uploadVersionId, file.filename, batchId);
          }
          return;
        }
        await new Promise((resolve) => window.setTimeout(resolve, 1200));
      }
    } catch (err) {
      if (pageActiveRef.current) {
        setDraftAttachments((current) => current.map((item) => (
          item.upload_document_version_id === uploadVersionId
            ? { ...item, duplicate_review_status: 'FAILED', upload_error: formatError(err) }
            : item
        )));
        setError(formatError(err));
        markBatchFileFailed(batchId, file.relative_path || file.filename, formatError(err));
      }
    } finally {
      pollingUploadReviewsRef.current.delete(uploadVersionId);
    }
  }

  async function pollUploadArchiveStatus(uploadVersionId: string, filename: string, batchId: string) {
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
                working_copy_status: archive.working_copy_status,
                original_filename: archive.original_filename,
                renamed_filename: archive.renamed_filename,
                processing_status: archive.processing_status,
              }
            : item
        )));
        if (archive.status === 'FAILED') {
          markBatchFileFailed(batchId, filename, archive.error_message || '自动整理失败');
          setError(archive.error_message || `文件“${filename}”归档失败，系统将按策略重试。`);
          return;
        }
        if (archive.processing_status === 'COMPLETED') {
          setUploadBatch((current) => current?.id === batchId ? {
            ...current,
            processed: current.processed + 1,
          } : current);
          return;
        }
        if (['CANCELLED', 'EXISTING_FILE_SELECTED'].includes(archive.status)) {
          return;
        }
        await new Promise((resolve) => window.setTimeout(resolve, 1500));
      } catch (err) {
        if (pageActiveRef.current) {
          setError(formatError(err));
          markBatchFileFailed(batchId, filename, formatError(err));
        }
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
      markBatchFileFailed(
        pendingUploadOrganization?.batchId || '',
        review.filename,
        '用户取消了重复文件上传',
      );
      setDraftAttachments((current) => current.filter(
        (item) => item.upload_document_version_id !== review.upload_document_version_id,
      ));
      return;
    }
    if (review.decision === 'USE_EXISTING_FILE' && result.selected_existing_document_id) {
      setUploadBatch((current) => current && current.id === pendingUploadOrganization?.batchId ? {
        ...current,
        processed: current.processed + 1,
      } : current);
      const selectedCandidate = review.candidates.find(
        (candidate) => candidate.existing_document_id === result.selected_existing_document_id,
      );
      setDraftAttachments((current) => deduplicateAttachmentsByDocumentId(
        current.map((item) => (
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
        )),
      ));
      return;
    }
    setDraftAttachments((current) => current.map((item) => (
      item.upload_document_version_id === review.upload_document_version_id
        ? { ...item, archive_status: result.archive_status, duplicate_review_status: 'RESOLVED' }
        : item
    )));
    if (review.decision === 'CONTINUE_UPLOAD') {
      void pollUploadArchiveStatus(
        review.upload_document_version_id,
        review.filename,
        pendingUploadOrganization?.batchId || '',
      );
    }
  }

  function markBatchFileFailed(batchId: string, relativePath: string, failureMessage: string) {
    if (!batchId) return;
    setUploadBatch((current) => current?.id === batchId ? {
      ...current,
      processed: current.processed + 1,
      succeeded: Math.max(0, current.succeeded - 1),
      failed: current.failed + 1,
      failures: [...current.failures, { relativePath, message: failureMessage }],
    } : current);
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
    // Office 文件优先展示已解析正文；浏览器原生支持的格式继续使用鉴权 Blob 预览。
    setError('');
    if (file.file_availability === 'TRASHED') {
      setError('文件已删除并保存在回收站中，请先恢复后再查看。');
      return;
    }
    if (file.file_availability && file.file_availability !== 'AVAILABLE') {
      setError(file.availability_message || '当前文件不可用。');
      return;
    }
    if (file.status === 'MISSING') {
      setError('原始文件已不存在，无法打开附件。');
      return;
    }
    try {
      if (!canPreviewInBrowser(file)) {
        try {
          setDocumentPreview(await getFilePreview(token, file.document_id));
          return;
        } catch (previewError) {
          // 尚未生成 document_pages 时保留原有下载能力；权限或文件不存在错误必须关闭式失败。
          if (!(previewError instanceof ApiError) || previewError.status !== 409) {
            throw previewError;
          }
        }
      }
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

  async function restoreHistoricalAttachment(file: ChatAttachment) {
    // 恢复按钮只生成一轮明确的对话请求；真实移动仍必须展示 OperationPlan 并再次确认。
    if (
      submitting
      || uploading
      || historyLoading
      || file.file_availability !== 'TRASHED'
      || !file.can_restore
    ) {
      return;
    }
    const currentMessage = `恢复文件《${file.filename}》`;
    const attachmentsForTurn = [file];
    const turnId = createClientId();
    setError('');
    setSubmitting(true);
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
    try {
      const result = await sendAgentMessage(
        token,
        conversationId,
        currentMessage,
        [{ document_id: file.document_id, relative_path: file.relative_path }],
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

  async function refreshHistoricalAttachmentStatuses() {
    // OperationPlan 确认后只刷新附件当前状态，不清空已经加载的聊天分页或本地结果。
    const conversation = await getConversationDetail(token, conversationId, { limit: 50 });
    const latestByDocumentId = new Map(
      conversation.messages.flatMap((historyMessage) => historyMessage.attachments)
        .map((attachment) => [attachment.document_id, attachment] as const),
    );
    setChatTurns((current) => current.map((turn) => ({
      ...turn,
      attachments: turn.attachments.map((attachment) => {
        const latest = latestByDocumentId.get(attachment.document_id);
        return latest ? { ...attachment, ...latest } : attachment;
      }),
    })));
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
    <>
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
            onClick={onOpenFiles}
          >
            <FolderTree size={16} />
            <span>文件列表</span>
          </button>
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
          {['ops', 'admin'].includes(user.role) ? (
            <>
              <button
                className="sidebar-menu-item"
                type="button"
                onClick={onOpenFailedFiles}
              >
                <AlertTriangle size={16} />
                <span>失败文件</span>
              </button>
              <button
                className="sidebar-menu-item"
                type="button"
                onClick={onOpenAgentRuns}
              >
                <Activity size={16} />
                <span>任务诊断</span>
              </button>
              <button
                className="sidebar-menu-item"
                type="button"
                onClick={onOpenCapabilitySuggestions}
              >
                <Lightbulb size={16} />
                <span>能力建议</span>
              </button>
            </>
          ) : null}
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
                  onRestoreAttachment={restoreHistoricalAttachment}
                  onOpenDocument={openSearchDocument}
                  onOpenManagedFile={openManagedFile}
                  onOperationConfirmed={refreshHistoricalAttachmentStatuses}
                  onUsePrompt={(prompt) => {
                    // 后续建议只填入编辑框，仍由用户检查并主动发送。
                    setMessage(prompt);
                  }}
                  onFollowupResult={(response) => {
                    // 选择卡续跑会在后端创建真实消息和 AgentRun；页面直接追加该轮，
                    // 不伪造本地搜索结果，刷新后仍能从同一会话历史恢复。
                    setChatTurns((current) => {
                      if (current.some((item) => item.id === response.message.id)) {
                        return current;
                      }
                      return [
                        ...current,
                        {
                          id: response.message.id,
                          userText: response.message.content,
                          attachments: [],
                          status: 'completed',
                          response,
                        },
                      ];
                    });
                    scrollMessageListToBottom();
                  }}
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
            {uploadBatch ? (
              <UploadBatchProgress
                batch={uploadBatch}
                onDismiss={() => setUploadBatch(null)}
              />
            ) : null}
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
              <div className="upload-picker-group">
                <label className="file-picker">
                  <Paperclip size={18} />
                  <span>{uploading ? '上传中...' : '选择文件'}</span>
                  <input
                    accept="image/*,.pdf,.doc,.docx,.xls,.xlsx,.txt,.md,.csv"
                    disabled={uploading || submitting}
                    multiple
                    type="file"
                    onChange={(event) => void handleFileChange(event, 'files')}
                  />
                </label>
                <label className="file-picker folder-picker">
                  <FolderTree size={18} />
                  <span>{uploading ? '上传中...' : '上传文件夹'}</span>
                  <input
                    ref={folderInputRef}
                    accept="image/*,.pdf,.doc,.docx,.xls,.xlsx,.txt,.md,.csv"
                    disabled={uploading || submitting}
                    multiple
                    type="file"
                    onChange={(event) => void handleFileChange(event, 'folder')}
                  />
                </label>
              </div>
              <button
                className="primary-button send-button"
                disabled={submitting || uploading || historyLoading || waitingForDuplicateResolution}
                type="submit"
              >
                <Send size={18} />
                {submitting
                  ? '发送中...'
                  : historyLoading
                    ? '加载中...'
                    : waitingForDuplicateResolution
                      ? '请先确认重复文件'
                      : '发送'}
              </button>
            </div>
          </form>

          {error ? <p className="form-message error">{error}</p> : null}
        </div>
      </section>
      </main>
      {documentPreview ? (
        <DocumentPreviewDialog
          preview={documentPreview}
          onClose={() => setDocumentPreview(null)}
        />
      ) : null}
    </>
  );
}
