// 通过 SSR 保护 WorkBuddy 文档流的两个关键边界：助手身份可见、无结果不渲染空卡片。
import assert from 'node:assert/strict';
import test from 'node:test';

import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';

test('助手回执展示 File Agent 身份与用户可读状态', async () => {
  const vite = await createServer({ logLevel: 'silent', server: { middlewareMode: true } });
  try {
    const { ChatTurnView } = await vite.ssrLoadModule('/src/features/chat/ChatTurnView.tsx');
    const html = renderToStaticMarkup(React.createElement(ChatTurnView, {
      token: '',
      turn: { id: 'turn-1', userText: '读取文件', attachments: [], status: 'completed', response: {
        message: { id: 'message-1', content: '读取文件' },
        task_result: {
          task_status: 'completed',
          presentation: null,
          document_results: [],
          final_response: '已完成',
        },
      } },
      onOpenAttachment: () => {},
      onRestoreAttachment: () => {},
      onOpenDocument: () => {},
      onOpenManagedFile: () => {},
    }));
    assert.match(html, /File Agent/);
    assert.match(html, /已完成/);
  } finally {
    await vite.close();
  }
});

test('无搜索结果只展示澄清文字，不展示空结果大卡片', async () => {
  const vite = await createServer({ logLevel: 'silent', server: { middlewareMode: true } });
  try {
    const { AgentRunReceipt } = await vite.ssrLoadModule('/src/features/chat/AgentRunReceipt.tsx');
    const html = renderToStaticMarkup(React.createElement(AgentRunReceipt, {
      taskResult: {
        task_status: 'completed',
        response_type: 'file_search_results',
        file_search_result: {
          query: '不存在的主题',
          total_returned: 0,
          partial: false,
          user_message: '没有找到相关文件，请再精确或确认一下查询条件。',
          files: [],
        },
        document_results: [],
        presentation: {
          schema_version: 'file-task-receipt.v1',
          task_kind: 'SEARCH',
          title: '文件搜索',
          phase: { code: 'COMPLETED', label: '已完成' },
          request: { target_label: '文件', scope_label: '当前范围', action_label: '查找', conditions: [] },
          outcome: { headline: '未找到', total_count: 0, completed_count: 0, failed_count: 0, needs_review_count: 0, skipped_count: 0, completeness: 'COMPLETE' },
          change_impact: { originals_changed: false, working_copies_changed: false, derivatives_created: 0, operation_executed: false, message: '原件未改变。' },
          notices: [],
          next_actions: [],
        },
      },
    }));
    assert.match(html, /没有找到相关文件/);
    assert.doesNotMatch(html, /search-results-receipt/);
  } finally {
    await vite.close();
  }
});
