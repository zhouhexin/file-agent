// 源码契约测试保护未引入路由库时的登录返回行为。
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

test('preserves the files deep link across the login gate', () => {
  const source = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');

  assert.match(source, /function readInitialReturnTarget/);
  assert.match(source, /`\/files\$\{window\.location\.search\}`/);
  assert.match(source, /window\.history\.replaceState\(null, '', returnTarget\)/);
  assert.match(source, /setCurrentPath\('\/files'\)/);
});
