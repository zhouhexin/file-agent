// 这些测试保护“先暂存、发送后处理”边界和后台状态投影。
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import {
  archiveStatusToDocumentResult,
  createPendingUploadResult,
  partitionUploadFiles,
  SUPPORTED_UPLOAD_ACCEPT,
} from '../src/features/chat/batchUpload.ts';

test('filters unsupported files before creating an upload batch', () => {
  const selected = [
    { name: 'notice.DOCX' },
    { name: 'slides.pptx' },
    { name: 'photo.gif' },
    { name: 'scan.tif' },
    { name: 'README' },
  ];
  const result = partitionUploadFiles(selected);

  assert.deepEqual(result.supported.map((file) => file.name), ['notice.DOCX', 'scan.tif']);
  assert.deepEqual(
    result.unsupported.map((file) => file.name),
    ['slides.pptx', 'photo.gif', 'README'],
  );
  assert.doesNotMatch(SUPPORTED_UPLOAD_ACCEPT, /image\/\*/);
  assert.doesNotMatch(SUPPORTED_UPLOAD_ACCEPT, /\.pptx/);
});

test('creates a pending upload receipt', () => {
  const result = createPendingUploadResult('batch-file-1', '通知.docx');

  assert.equal(result.processing_status, 'PROCESSING');
  assert.equal(result.original_filename, '通知.docx');
  assert.equal(result.renamed_filename, null);
  assert.deepEqual(result.categories, []);
});

test('upload flow stays staged until send and never forwards folder-relative paths', () => {
  const source = readFileSync(
    new URL('../src/features/chat/ChatPage.tsx', import.meta.url),
    'utf8',
  );

  assert.doesNotMatch(source, /buildUploadOrganizationInstruction/);
  assert.doesNotMatch(source, /请读取并分类“/);
  assert.doesNotMatch(source, /getSelectedFileRelativePath/);
  assert.match(source, /startUploadProcessing\(token, uploadVersionId\)/);
  assert.match(source, /\? 'staged'/);
  assert.match(source, /archiveStatusToDocumentResult\(archive\)/);
  assert.match(source, /releaseProcessedDraftAttachment\(uploadVersionId\)/);
});

test('moves a submitted batch out of the composer and keeps text sending available', () => {
  const source = readFileSync(
    new URL('../src/features/chat/ChatPage.tsx', import.meta.url),
    'utf8',
  );
  const submittedBatchIndex = source.indexOf('className="submitted-upload-batch"');
  const composerIndex = source.indexOf('<form className=');
  const composerSource = source.slice(composerIndex);

  assert.ok(submittedBatchIndex >= 0);
  assert.ok(submittedBatchIndex < composerIndex);
  assert.doesNotMatch(composerSource, /<UploadBatchProgress/);
  assert.match(source, /submitted: true/);
  assert.match(source, /submittedVersionIds/);
  assert.doesNotMatch(source, /waitingForDuplicateResolution/);
  assert.doesNotMatch(source, /historyLoading \|\| uploadBatchActive/);
});

test('keeps the submitted batch as a non-dismissible agent response', () => {
  // 批次回执与检索回执一样属于正式回答，完成或失败后也不能被用户关闭并清空。
  const pageSource = readFileSync(
    new URL('../src/features/chat/ChatPage.tsx', import.meta.url),
    'utf8',
  );
  const receiptSource = readFileSync(
    new URL('../src/features/chat/UploadBatchProgress.tsx', import.meta.url),
    'utf8',
  );

  assert.match(pageSource, /className="submitted-upload-batch"/);
  assert.doesNotMatch(pageSource, /dismissUploadBatch|onDismiss=/);
  assert.doesNotMatch(receiptSource, /onDismiss|关闭批量上传进度/);
});

test('expanding staged attachments never submits the composer form', () => {
  const source = readFileSync(
    new URL('../src/features/chat/AttachmentRail.tsx', import.meta.url),
    'utf8',
  );

  assert.match(
    source,
    /<button\s+type="button"\s+onClick=\{\(\) => setExpanded/,
  );
});

test('projects backend archive status directly into a classification and rename receipt', () => {
  const result = archiveStatusToDocumentResult({
    upload_document_version_id: 'upload-version-1',
    document_id: 'working-document-1',
    status: 'ARCHIVED',
    managed_file_id: 'managed-file-1',
    working_copy_id: 'working-copy-1',
    working_copy_status: 'ACTIVE',
    original_filename: '通知.docx',
    renamed_filename: '2026_财务处_项目通知.docx',
    processing_status: 'NEEDS_REVIEW',
    rename_status: 'COMPLETED',
    classification_status: 'COMPLETED',
    categories: [{
      name: '学校/财务/其他',
      category_path: ['学校', '财务', '其他'],
      confidence: 0.8,
      status: 'SUGGESTED',
      evidence: [],
    }],
    organization_status: 'NEEDS_REVIEW',
    review_reasons: ['只能确定为部门下的其他分类，需要人工确认。'],
    pending_decision: null,
    filesystem_job_id: 'job-1',
    error_code: null,
    error_message: null,
  });

  assert.equal(result.document_id, 'working-document-1');
  assert.equal(result.renamed_filename, '2026_财务处_项目通知.docx');
  assert.deepEqual(result.categories?.[0].category_path, ['学校', '财务', '其他']);
  assert.equal(result.processing_status, 'NEEDS_REVIEW');
  assert.deepEqual(result.review_reasons, ['只能确定为部门下的其他分类，需要人工确认。']);
});

test('does not project an unsettled rename as the original filename', () => {
  const result = archiveStatusToDocumentResult({
    upload_document_version_id: 'upload-version-processing',
    document_id: 'working-document-processing',
    status: 'ARCHIVED',
    managed_file_id: 'managed-file-processing',
    working_copy_id: 'working-copy-processing',
    working_copy_status: 'ORGANIZING',
    original_filename: '原始名称.docx',
    renamed_filename: null,
    processing_status: 'PROCESSING',
    rename_status: 'PROCESSING',
    classification_status: 'PROCESSING',
    categories: [],
    organization_status: null,
    review_reasons: [],
    pending_decision: null,
    filesystem_job_id: 'job-processing',
    error_code: null,
    error_message: null,
  });

  assert.equal(result.original_filename, '原始名称.docx');
  assert.equal(result.renamed_filename, null);
  assert.equal(result.rename_status, 'PROCESSING');
});

test('projects a pending proposed name separately from an executed rename', () => {
  const result = archiveStatusToDocumentResult({
    upload_document_version_id: 'upload-version-review',
    document_id: 'working-document-review',
    status: 'ARCHIVED',
    managed_file_id: 'managed-file-review',
    working_copy_id: 'working-copy-review',
    working_copy_status: 'ACTIVE',
    original_filename: '附件1.docx',
    renamed_filename: '附件1.docx',
    processing_status: 'NEEDS_REVIEW',
    rename_status: 'NEEDS_REVIEW',
    classification_status: 'COMPLETED',
    categories: [],
    organization_status: 'NEEDS_REVIEW',
    review_reasons: ['命名依据不足。'],
    pending_decision: { proposed_filename: '2026_奖学金通知.docx' },
    filesystem_job_id: 'job-review',
    error_code: null,
    error_message: null,
  });

  assert.equal(result.renamed_filename, null);
  assert.equal(result.rename_suggestion?.proposed_filename, '2026_奖学金通知.docx');
});
