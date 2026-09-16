import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

test('application exposes the prompt guide route from the chat sidebar', () => {
  const appSource = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const chatSource = readFileSync(new URL('../src/features/chat/ChatPage.tsx', import.meta.url), 'utf8');
  const authSource = readFileSync(new URL('../src/features/auth/AuthPage.tsx', import.meta.url), 'utf8');
  const guideSource = readFileSync(new URL('../src/features/guide/PromptGuidePage.tsx', import.meta.url), 'utf8');

  assert.match(appSource, /'\/prompt-guide'/);
  assert.match(appSource, /<PromptGuidePage/);
  assert.match(appSource, /提示词指南不包含用户数据或文件入口/);
  assert.match(chatSource, /onOpenPromptGuide/);
  assert.match(chatSource, /提示词指南/);
  assert.match(authSource, /href="\/prompt-guide"/);
  assert.match(guideSource, /className="prompt-guide-prompt-row"/);
  assert.doesNotMatch(guideSource, /返回聊天/);
  assert.doesNotMatch(guideSource, /onBack/);
  assert.match(appSource, /return <PromptGuidePage \/>/);
});
