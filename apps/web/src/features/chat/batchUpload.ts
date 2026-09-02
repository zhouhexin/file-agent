// 批量上传辅助函数只整理浏览器文件元数据和后端状态投影，不执行分类判断。

import type { DocumentResult, UploadArchiveStatus } from '../../types';
import type { ChatAttachment } from './presentation';

export const SUPPORTED_UPLOAD_EXTENSIONS = [
  '.pdf',
  '.doc',
  '.docx',
  '.xls',
  '.xlsx',
  '.txt',
  '.md',
  '.csv',
  '.png',
  '.jpg',
  '.jpeg',
  '.tif',
  '.tiff',
  '.bmp',
  '.webp',
] as const;

export const SUPPORTED_UPLOAD_ACCEPT = SUPPORTED_UPLOAD_EXTENSIONS.join(',');

const SUPPORTED_UPLOAD_EXTENSION_SET = new Set<string>(SUPPORTED_UPLOAD_EXTENSIONS);

export function partitionUploadFiles<T extends { name: string }>(files: readonly T[]): {
  supported: T[];
  unsupported: T[];
} {
  const supported: T[] = [];
  const unsupported: T[] = [];
  files.forEach((file) => {
    const dotIndex = file.name.lastIndexOf('.');
    const extension = dotIndex > 0 ? file.name.slice(dotIndex).toLowerCase() : '';
    (SUPPORTED_UPLOAD_EXTENSION_SET.has(extension) ? supported : unsupported).push(file);
  });
  return { supported, unsupported };
}

export type UploadBatchFailure = {
  relativePath: string;
  message: string;
};

export type UploadBatchProgressState = {
  id: string;
  submitted: boolean;
  total: number;
  completed: number;
  processed: number;
  succeeded: number;
  needsReview: number;
  failed: number;
  failures: UploadBatchFailure[];
  files: UploadBatchFileState[];
  status: 'uploading' | 'staged' | 'processing' | 'completed' | 'failed';
};

export type UploadBatchFileState = {
  id: string;
  relativePath: string;
  uploadVersionId?: string;
  attachment?: ChatAttachment;
  result: DocumentResult;
};

export function createPendingUploadResult(id: string, filename: string): DocumentResult {
  return {
    document_id: id,
    filename,
    original_filename: filename,
    // 暂存和处理中还没有实际产出的标准名称，不能用原名冒充重命名结果。
    renamed_filename: null,
    rename_status: 'PROCESSING',
    processing_status: 'PROCESSING',
    extraction_status: 'PROCESSING',
    page_count: 0,
    text_reused: false,
    classification_reused: false,
    categories: [],
    warnings: [],
    errors: [],
  };
}

export function archiveStatusToDocumentResult(status: UploadArchiveStatus): DocumentResult {
  const failed = status.processing_status === 'FAILED';
  const renameSettled = ['COMPLETED', 'NO_CHANGE'].includes(status.rename_status);
  const proposedFilename = typeof status.pending_decision?.proposed_filename === 'string'
    ? status.pending_decision.proposed_filename.trim()
    : '';
  return {
    document_id: status.document_id,
    working_copy_id: status.working_copy_id || undefined,
    filename: status.renamed_filename || status.original_filename,
    original_filename: status.original_filename,
    renamed_filename: renameSettled ? status.renamed_filename : null,
    rename_status: status.rename_status,
    processing_status: status.processing_status,
    organization_status: status.organization_status === 'NEEDS_REVIEW'
      ? 'NEEDS_REVIEW'
      : 'READY',
    extraction_status: failed ? 'FAILED' : status.classification_status,
    page_count: 0,
    text_reused: false,
    classification_reused: false,
    categories: status.categories,
    pending_decision: status.pending_decision,
    rename_suggestion: proposedFilename ? { proposed_filename: proposedFilename } : null,
    review_reasons: status.review_reasons,
    managed_original_unchanged: true,
    warnings: status.review_reasons,
    errors: failed
      ? [{ code: status.error_code || 'UPLOAD_PROCESSING_FAILED', message: status.error_message || '文件自动处理失败。' }]
      : [],
  };
}
