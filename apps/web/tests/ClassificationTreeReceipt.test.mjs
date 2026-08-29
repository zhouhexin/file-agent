// 通过真实 TSX 渲染保护分类树回执文案和普通用户的置信度隐藏边界。
import assert from 'node:assert/strict';
import test from 'node:test';

import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';

/** 构造带主分类和相关分类的最小文件结果。 */
function makeResult() {
  return {
    document_id: 'document-1',
    filename: '职称通知.doc',
    extraction_status: 'COMPLETED',
    page_count: 1,
    text_reused: false,
    classification_reused: false,
    categories: [
      {
        category_id: 'primary',
        name: '学校/人事师资/职称',
        category_path: ['学校', '人事师资', '职称'],
        confidence: 0.75,
        evidence: [],
      },
      {
        category_id: 'related',
        name: '人事师资',
        category_path: ['学校', '人事师资'],
        confidence: 0.71,
        evidence: [],
      },
    ],
    warnings: [],
    errors: [],
  };
}

test('分类回执使用已整理文案且不展示置信度', async () => {
  const vite = await createServer({
    logLevel: 'silent',
    server: { middlewareMode: true },
  });
  try {
    const { ClassificationTreeReceipt } = await vite.ssrLoadModule(
      '/src/features/chat/ClassificationTreeReceipt.tsx',
    );
    const html = renderToStaticMarkup(
      React.createElement(ClassificationTreeReceipt, {
        results: [makeResult()],
        attachments: [],
      }),
    );

    assert.match(html, /已整理 1 个文件/);
    assert.match(html, /学校/);
    assert.match(html, /人事师资/);
    assert.match(html, /职称通知\.doc/);
    assert.doesNotMatch(html, /已按主分类整理|置信度|0\.75|75%/);
  } finally {
    await vite.close();
  }
});
