// 批量上传辅助函数只整理浏览器文件元数据和后端状态投影，不执行分类判断。

import type { DocumentResult, UploadArchiveStatus } from '../../types';
import type { ChatAttachment } from './presentation';

export type FolderSelectedFile = {
  name: string;
  webkitRelativePath?: string;
};

export type UploadBatchFailure = {
  relativePath: string;
  message: string;
};

export type UploadBatchProgressState = {
  id: string;
  mode: 'files' | 'folder';
  folderName?: string;
  total: number;
  completed: number;
  processed: number;
  succeeded: number;
  needsReview: number;
  failed: number;
  failures: UploadBatchFailure[];
  files: UploadBatchFileState[];
  status: 'uploading' | 'processing' | 'completed' | 'failed';
};

export type UploadBatchFileState = {
  id: string;
  relativePath: string;
  uploadVersionId?: string;
  attachment?: ChatAttachment;
  result: DocumentResult;
};

export function getSelectedFileRelativePath(file: FolderSelectedFile): string {
  // webkitRelativePath 是浏览器目录选择提供的展示路径；真正的安全校验仍由后端执行。
  return file.webkitRelativePath?.replace(/\\/g, '/') || file.name;
}

export function inferSelectedFolderName(files: FolderSelectedFile[]): string {
  // 同一目录选择批次共享第一级目录；浏览器未提供时使用中性名称，不猜测本地绝对路径。
  const relativePath = files[0] ? getSelectedFileRelativePath(files[0]) : '';
  const firstSegment = relativePath.split('/')[0];
  return relativePath.includes('/') && firstSegment ? firstSegment : '所选文件夹';
}

export function createPendingUploadResult(id: string, filename: string): DocumentResult {
  return {
    document_id: id,
    filename,
    original_filename: filename,
    renamed_filename: filename,
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
  return {
    document_id: status.document_id,
    working_copy_id: status.working_copy_id || undefined,
    filename: status.renamed_filename || status.original_filename,
    original_filename: status.original_filename,
    renamed_filename: status.renamed_filename || status.original_filename,
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
    review_reasons: status.review_reasons,
    managed_original_unchanged: true,
    warnings: status.review_reasons,
    errors: failed
      ? [{ code: status.error_code || 'UPLOAD_PROCESSING_FAILED', message: status.error_message || '文件自动处理失败。' }]
      : [],
  };
}
