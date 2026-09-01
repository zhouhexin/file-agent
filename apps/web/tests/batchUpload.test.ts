// 这些纯函数测试保护文件夹批次范围和默认分类文案，避免前端猜测本地绝对路径。
import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildFolderClassificationInstruction,
  buildUploadOrganizationInstruction,
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

test('builds the default upload organization task for files and folders', () => {
  assert.equal(
    buildUploadOrganizationInstruction('本次上传', 2),
    '请读取并分类“本次上传”中的 2 个文件。系统已完成工作副本标准名称整理，请逐文件展示上传时名称、整理后名称、分类结果、处理状态，以及失败或待复核原因。',
  );
});

test('builds an explicit auditable classification task for a folder batch', () => {
  assert.equal(
    buildFolderClassificationInstruction('财务处', 2),
    '请读取并分类文件夹“财务处”中的 2 个文件，逐文件展示分类、置信度、证据和处理状态。',
  );
});
