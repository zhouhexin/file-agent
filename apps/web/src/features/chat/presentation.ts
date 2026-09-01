// 聊天页展示工具只负责前端呈现规则，不承担文件权限或后端路径校验。
import type { ConversationHistoryMessage, UploadedFile } from '../../types';

export type ChatAttachment = UploadedFile & {
  // 图片预览使用浏览器本地 object URL，发送后仍仅以 document_id 作为后端引用。
  preview_url?: string;
  deleting?: boolean;
  upload_error?: string;
};

export type ChatTurn = {
  id: string;
  userText: string;
  attachments: ChatAttachment[];
  response?: import('../../types').SendMessageResponse;
  status: 'sending' | 'completed' | 'failed';
  role?: 'user' | 'assistant' | string;
  metadata?: Record<string, unknown>[];
};

export type AttachmentListProps = {
  attachments: ChatAttachment[];
  layout?: 'rail' | 'stack';
  locked?: boolean;
  onOpen?: (file: ChatAttachment) => void;
  onRemove?: (documentId: string) => void;
  onRestore?: (file: ChatAttachment) => void;
};

export function deduplicateAttachmentsByDocumentId(
  attachments: ChatAttachment[],
): ChatAttachment[] {
  // 只折叠相同 document_id；同名、同大小甚至同哈希但 ID 不同的文件仍需分别展示，
  // 避免前端替用户决定应该保留或操作哪一份文件。
  const seen = new Set<string>();
  return attachments.filter((attachment) => {
    if (seen.has(attachment.document_id)) {
      return false;
    }
    seen.add(attachment.document_id);
    return true;
  });
}

export function hasUnresolvedUploadReview(attachments: ChatAttachment[]): boolean {
  return attachments.some(
    (attachment) => Boolean(attachment.upload_document_version_id)
      && !['RESOLVED', 'FAILED'].includes(attachment.duplicate_review_status || ''),
  );
}

export function isVisibleConversationHistoryMessage(
  message: Pick<ConversationHistoryMessage, 'role' | 'content'>,
): boolean {
  // 普通聊天页只接收用户消息和最终助手消息。SYSTEM_AUDIT 等内部角色应由后端过滤，
  // 这里保留展示层防线，避免旧服务或旧数据库记录把生命周期状态渲染成聊天气泡。
  if (!['user', 'assistant'].includes(message.role)) {
    return false;
  }
  if (message.role !== 'assistant') {
    return true;
  }
  const content = message.content.trim();
  return !(
    content.startsWith('重复上传处理：')
    || content.startsWith('已记录重复上传决策：')
    || content.startsWith('工作副本操作完成：')
    || content.endsWith('的原件已归档，正在创建工作副本。')
  );
}

export function formatFileSize(sizeBytes: number): string {
  // 文件大小只用于界面展示，后端仍保存精确字节数。
  if (sizeBytes < 1024) {
    return `${sizeBytes} B`;
  }
  if (sizeBytes < 1024 * 1024) {
    return `${(sizeBytes / 1024).toFixed(1)} KB`;
  }
  return `${(sizeBytes / 1024 / 1024).toFixed(1)} MB`;
}

export function formatUploadStatus(file: UploadedFile): string {
  // 展示上传和 deterministic ingest 的合并状态，便于用户理解文件是否已完成基础处理。
  if (file.deduplicated) {
    return '已存在，复用处理结果';
  }
  if (file.ingest_status === 'INGESTED') {
    return '已处理';
  }
  if (file.ingest_status === 'INGESTING') {
    return '处理中';
  }
  if (file.ingest_status === 'FAILED') {
    return '处理失败';
  }
  return file.status;
}

export function canPreviewInBrowser(file: UploadedFile): boolean {
  // 浏览器原生支持图片、PDF 和常见纯文本预览；Office 文件先走下载。
  return canPreviewFileInfo(file.filename, file.content_type);
}

export function canPreviewFileInfo(filenameValue: string, contentType: string): boolean {
  // 上传附件和受管文件预览共用同一套浏览器能力判断。
  const filename = filenameValue.toLowerCase();
  if (contentType.startsWith('image/')) {
    return true;
  }
  if (contentType === 'application/pdf' || filename.endsWith('.pdf')) {
    return true;
  }
  if (contentType.startsWith('text/')) {
    return true;
  }
  return ['.txt', '.md', '.csv', '.json'].some((suffix) => filename.endsWith(suffix));
}

export function findAttachmentByDocumentId(
  attachments: ChatAttachment[],
  documentId: string,
): ChatAttachment | undefined {
  // 结果卡片需要从本轮附件中找回原始文件信息，用于点击打开文件。
  return attachments.find((file) => file.document_id === documentId);
}

export function getFailureMessage(errors: Array<{ message?: string } | string>): string {
  // 失败结果可能来自不同 Tool，统一收敛成用户可读的失败原因。
  const firstError = errors[0];
  if (!firstError) {
    return '未知错误';
  }
  if (typeof firstError === 'string') {
    return firstError;
  }
  return firstError.message || '未知错误';
}
