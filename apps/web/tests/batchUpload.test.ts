// 这些测试保护文件夹批次范围和后台状态投影，避免前端猜测任务或本地绝对路径。
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import {
  archiveStatusToDocumentResult,
  createPendingUploadResult,
  getSelectedFileRelativePath,
  inferSelectedFolderName,
} from '../src/features/chat/batchUpload.ts';

test('preserves browser folder-relative paths and infers the selected root folder', () => {
  const files = [
    { name: '通知.docx', webkitRelativePath: '财务处/2026/通知.docx' },
    { name: '回执.xlsx', webkitRelativePath: '财务处/回执.xlsx' },
  ];

  assert.equal(getSelectedFileRelativePath(files[0]), '财务处/2026/通知.docx');
  assert.equal(inferSelectedFolderName(files), '财务处');
});

test('creates a pending upload receipt', () => {
  const result = createPendingUploadResult('batch-file-1', '通知.docx');

  assert.equal(result.processing_status, 'PROCESSING');
  assert.equal(result.original_filename, '通知.docx');
  assert.deepEqual(result.categories, []);
});

test('upload flow polls archive status without constructing or sending an implicit task message', () => {
  const source = readFileSync(
    new URL('../src/features/chat/ChatPage.tsx', import.meta.url),
    'utf8',
  );

  assert.doesNotMatch(source, /buildUploadOrganizationInstruction/);
  assert.doesNotMatch(source, /请读取并分类“/);
  assert.match(source, /archiveStatusToDocumentResult\(archive\)/);
  assert.match(source, /releaseProcessedDraftAttachment\(uploadVersionId\)/);
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
