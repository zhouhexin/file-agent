// 通过真实 TSX 渲染保护重复上传卡的三项决策入口，避免后端字段短暂缺失时静默隐藏按钮。
import assert from 'node:assert/strict';
import test from 'node:test';

import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';

/** 构造一个处于待确认状态的最小重复上传记录。 */
function makeReview({ canUseExisting }) {
  return {
    id: 'review-1',
    upload_document_version_id: 'upload-version-1',
    document_id: 'upload-document-1',
    filename: 'duplicate.jpg',
    status: 'WAITING_CONFIRMATION',
    decision: null,
    expires_at: '2026-09-05T12:00:00Z',
    candidates: [
      {
        id: 'candidate-1',
        match_type: 'EXACT_SHA256',
        match_scope: 'SAME_WORKSPACE',
        similarity_score: 1,
        summary: {
          message: '检测到共享工作目录中的相同文件',
          filename: 'duplicate.jpg',
        },
        existing_working_copy_id: canUseExisting ? 'working-copy-1' : null,
        existing_document_id: canUseExisting ? 'existing-document-1' : null,
      },
    ],
    allowed_decisions: canUseExisting
      ? ['CONTINUE_UPLOAD', 'USE_EXISTING_FILE', 'CANCEL_UPLOAD']
      : ['CONTINUE_UPLOAD', 'CANCEL_UPLOAD'],
    duplicate_check_job_id: 'job-1',
  };
}

test('使用现有文件按钮始终展示，并仅在后端候选可用时启用', async () => {
  const vite = await createServer({
    logLevel: 'silent',
    server: { middlewareMode: true },
  });
  try {
    const { DuplicateUploadReviewCard } = await vite.ssrLoadModule(
      '/src/features/chat/DuplicateUploadReviewCard.tsx',
    );
    const unavailableHtml = renderToStaticMarkup(
      React.createElement(DuplicateUploadReviewCard, {
        token: 'test-token',
        review: makeReview({ canUseExisting: false }),
        onResolved: () => {},
      }),
    );
    const availableHtml = renderToStaticMarkup(
      React.createElement(DuplicateUploadReviewCard, {
        token: 'test-token',
        review: makeReview({ canUseExisting: true }),
        onResolved: () => {},
      }),
    );

    assert.match(unavailableHtml, /disabled=""[^>]*>使用现有文件<\/button>/);
    assert.match(unavailableHtml, /现有文件尚未准备完成，暂时不能选择/);
    assert.match(availableHtml, /<button[^>]*>使用现有文件<\/button>/);
    assert.doesNotMatch(availableHtml, /现有文件尚未准备完成，暂时不能选择/);
  } finally {
    await vite.close();
  }
});
