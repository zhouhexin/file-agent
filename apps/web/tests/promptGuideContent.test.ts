import assert from 'node:assert/strict';
import test from 'node:test';

import { PROMPT_GUIDE_SECTIONS } from '../src/features/guide/promptGuideContent.ts';

test('prompt guide covers current user workflows with unique copyable examples', () => {
  const items = PROMPT_GUIDE_SECTIONS.flatMap((section) => section.items);
  const ids = items.map((item) => item.id);

  assert.equal(new Set(ids).size, ids.length);
  assert.ok(items.length >= 20);
  assert.ok(items.some((item) => item.prompt.includes('01引进人才工作合同-王磊磊.doc')));
  assert.ok(items.some((item) => item.prompt.includes('本轮附件')));
  assert.ok(items.some((item) => item.prompt.includes('授权根')));
  assert.ok(items.some((item) => item.prompt.includes('原文依据')));
  assert.ok(items.some((item) => item.prompt.includes('全部分类建议、分类角色和每项原文依据')));
  assert.ok(items.some((item) => item.id === 'classification-reclassify' && item.prompt.includes('重新分类')));
  assert.ok(items.every((item) => item.prompt.trim().length > 0));
});
