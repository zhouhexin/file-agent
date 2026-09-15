// 分类深链接纯函数测试，保证 WorkBuddy 发出的链接可被分类页稳定恢复。
import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildClassificationDeepLink,
  readClassificationDeepLink,
} from '../src/features/files/classificationDeepLink.ts';

test('reads category and page from a classification deep link', () => {
  assert.deepEqual(readClassificationDeepLink('?category_id=school.hr&page=3'), {
    categoryId: 'school.hr',
    page: 3,
    accessToken: null,
  });
});

test('normalizes invalid classification deep-link values', () => {
  assert.deepEqual(readClassificationDeepLink('?page=-2'), {
    categoryId: null,
    page: 1,
    accessToken: null,
  });
  assert.deepEqual(readClassificationDeepLink('?category_id=../school&page=2x'), {
    categoryId: null,
    page: 1,
    accessToken: null,
  });
});

test('builds a compact classification deep link', () => {
  assert.equal(buildClassificationDeepLink('school.hr', 2), '/files?category_id=school.hr&page=2');
  assert.equal(buildClassificationDeepLink(null, 1), '/files');
  assert.equal(
    buildClassificationDeepLink('school.hr', 1, 'signed.token'),
    '/files?category_id=school.hr&classification_access_token=signed.token',
  );
});
