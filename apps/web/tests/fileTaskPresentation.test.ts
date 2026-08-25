// 使用 Node 原生测试保护文件任务组件依赖的纯展示规则，避免额外引入浏览器测试依赖。
import assert from 'node:assert/strict';
import test from 'node:test';

import {
  canShowFileTaskNextActions,
  fileSearchResultKey,
} from '../src/features/chat/fileTaskPresentation.ts';
import { hasUnresolvedUploadReview } from '../src/features/chat/presentation.ts';
import type { FileSearchResultFile } from '../src/types.ts';

/** 构造最小搜索结果，测试只关注逻辑身份，不依赖后端或浏览器。 */
function makeFile(overrides: Partial<FileSearchResultFile>): FileSearchResultFile {
  return {
    working_copy_id: null,
    document_id: 'document-1',
    document_version_id: 'version-1',
    filename: '工作总结.docx',
    category_path: [],
    match_reasons: [],
    match_location: null,
    evidence_preview: '',
    ...overrides,
  };
}

test('搜索卡片优先使用受管文件 ID，避免同一路径的历史版本产生重复 key', () => {
  const first = makeFile({
    managed_file_id: 'managed-1',
    document_version_id: 'version-1',
  });
  const second = makeFile({
    managed_file_id: 'managed-1',
    document_version_id: 'version-2',
  });

  assert.equal(fileSearchResultKey(first), 'managed:managed-1');
  assert.equal(fileSearchResultKey(first), fileSearchResultKey(second));
});

test('没有受管文件 ID 时，同名不同逻辑路径仍生成不同 key', () => {
  const office = makeFile({
    root_key: 'school-files',
    relative_path: '办公室/工作总结.docx',
  });
  const hr = makeFile({
    root_key: 'school-files',
    relative_path: '人事处/工作总结.docx',
  });

  assert.notEqual(fileSearchResultKey(office), fileSearchResultKey(hr));
});

test('处理中不展示下一步按钮，完成或待处理阶段才允许展示', () => {
  assert.equal(canShowFileTaskNextActions('PROCESSING'), false);
  assert.equal(canShowFileTaskNextActions('ORGANIZING'), false);
  assert.equal(canShowFileTaskNextActions('COMPLETED'), true);
  assert.equal(canShowFileTaskNextActions('NEEDS_ATTENTION'), true);
});

test('上传查重未完成或等待确认时禁止发送，确认完成后放行', () => {
  const attachment = {
    document_id: 'upload-document',
    filename: 'duplicate.jpg',
    content_type: 'image/jpeg',
    size_bytes: 10,
    sha256: 'sha256',
    status: 'UPLOADED',
    ingest_status: 'DUPLICATE_CHECK_PENDING',
    deduplicated: false,
    upload_document_version_id: 'upload-version',
    duplicate_review_status: 'CHECKING',
  };

  assert.equal(hasUnresolvedUploadReview([attachment]), true);
  assert.equal(
    hasUnresolvedUploadReview([{ ...attachment, duplicate_review_status: 'WAITING_CONFIRMATION' }]),
    true,
  );
  assert.equal(
    hasUnresolvedUploadReview([{ ...attachment, duplicate_review_status: 'RESOLVED' }]),
    false,
  );
  assert.equal(hasUnresolvedUploadReview([]), false);
  assert.equal(
    hasUnresolvedUploadReview([{
      ...attachment,
      upload_document_version_id: undefined,
      duplicate_review_status: undefined,
      status: 'WORKING_COPY',
    }]),
    false,
  );
});
