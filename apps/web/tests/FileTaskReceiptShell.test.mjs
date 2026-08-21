// 通过 Vite 的 SSR 加载真实 TSX 组件，验证处理中不会渲染可点击的后续操作。
import assert from 'node:assert/strict';
import test from 'node:test';

import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';

/** 构造最小公共回执；事实字段全部使用普通用户可见值。 */
function makePresentation(phaseCode) {
  return {
    schema_version: 'file-task-receipt.v1',
    task_kind: 'SEARCH',
    title: '文件查找结果',
    phase: { code: phaseCode, label: '正在处理' },
    request: {
      target_label: '相关文件',
      scope_label: '学校',
      action_label: '查找相关文件',
      conditions: [],
    },
    outcome: {
      headline: '正在查找文件',
      total_count: 0,
      completed_count: 0,
      failed_count: 0,
      needs_review_count: 0,
      skipped_count: 0,
      completeness: 'PROCESSING',
    },
    change_impact: {
      originals_changed: false,
      working_copies_changed: false,
      derivatives_created: 0,
      operation_executed: false,
      message: '原文件未改变。',
    },
    notices: [],
    // 即使历史后端错误携带了动作，前端也必须按阶段阻止按钮出现。
    next_actions: [
      {
        id: 'continue-search',
        label: '继续筛选',
        action_kind: 'FILL_PROMPT',
        prompt: '继续筛选文件',
        target_ref: null,
        requires_confirmation: false,
      },
    ],
  };
}

test('公共回执仅在已有结果的阶段渲染下一步按钮', async () => {
  const vite = await createServer({
    logLevel: 'silent',
    server: { middlewareMode: true },
  });
  try {
    const { FileTaskReceiptShell } = await vite.ssrLoadModule(
      '/src/features/chat/FileTaskReceiptShell.tsx',
    );
    const processingHtml = renderToStaticMarkup(
      React.createElement(FileTaskReceiptShell, {
        presentation: makePresentation('PROCESSING'),
      }),
    );
    const completedHtml = renderToStaticMarkup(
      React.createElement(FileTaskReceiptShell, {
        presentation: makePresentation('COMPLETED'),
        onUsePrompt: () => {},
      }),
    );

    assert.doesNotMatch(processingHtml, /继续筛选/);
    assert.match(completedHtml, /继续筛选/);
  } finally {
    await vite.close();
  }
});
