import type { ChangeItem, UploadedFile } from '../../types';

export type ChatAttachment = UploadedFile & {
  // 图片预览使用浏览器本地 object URL，发送后仍仅以 document_id 作为后端引用。
  preview_url?: string;
  deleting?: boolean;
};

export type ChatTurn = {
  id: string;
  userText: string;
  attachments: ChatAttachment[];
  response?: import('../../types').SendMessageResponse;
  status: 'sending' | 'completed' | 'failed';
};

export type AttachmentListProps = {
  attachments: ChatAttachment[];
  layout?: 'rail' | 'stack';
  locked?: boolean;
  onOpen?: (file: ChatAttachment) => void;
  onRemove?: (documentId: string) => void;
};

const FILE_MUTATION_CHANGE_TYPES = new Set([
  'FILE_RENAMED',
  'FILE_MOVED',
  'FILE_DELETED',
  'FILE_OVERWRITTEN',
]);

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
  const filename = file.filename.toLowerCase();
  if (file.content_type.startsWith('image/')) {
    return true;
  }
  if (file.content_type === 'application/pdf' || filename.endsWith('.pdf')) {
    return true;
  }
  if (file.content_type.startsWith('text/')) {
    return true;
  }
  return ['.txt', '.md', '.csv', '.json'].some((suffix) => filename.endsWith(suffix));
}

export function formatConfidence(confidence: number): string {
  // 分类置信度统一转成整数百分比，避免卡片里出现过多小数。
  return `${Math.round(confidence * 100)}%`;
}

export function hasFileMutation(items: ChangeItem[] | null): boolean {
  // 只有真实文件改名、移动、删除、覆盖才算修改原始文件；解析和分类建议不算。
  return Boolean(items?.some((item) => FILE_MUTATION_CHANGE_TYPES.has(item.change_type)));
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
