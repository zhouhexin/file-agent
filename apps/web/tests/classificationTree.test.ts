// 使用纯函数测试保护主分类树：一个文件只能出现一次，相关分类不得创建额外分支。
import assert from 'node:assert/strict';
import test from 'node:test';

import { buildClassificationTree, primaryCategoryPath } from '../src/features/chat/classificationTree.ts';
import type { DocumentResult } from '../src/types.ts';

function result(filename: string, paths: string[][]): DocumentResult {
  return {
    document_id: filename,
    filename,
    extraction_status: 'COMPLETED',
    page_count: 1,
    text_reused: false,
    classification_reused: false,
    categories: paths.map((path, index) => ({
      category_id: `${filename}-${index}`,
      name: path.join('/'),
      category_path: path,
      confidence: 0.8,
      evidence: [],
    })),
    warnings: [],
    errors: [],
  };
}

test('分类树只使用第一条主分类路径且文件不重复', () => {
  const files = [
    result('通知.doc', [
      ['学校', '人事师资', '考核聘任'],
      ['学校', '行政综合管理类', '规章制度'],
    ]),
    result('职称.doc', [['学校', '人事师资', '职称']]),
  ];
  const tree = buildClassificationTree(files);

  assert.equal(tree.length, 1);
  assert.equal(tree[0].name, '学校');
  assert.equal(tree[0].fileCount, 2);
  assert.equal(tree[0].children[0].name, '人事师资');
  assert.deepEqual(
    tree[0].children[0].children.map((node) => node.name),
    ['考核聘任', '职称'],
  );
  assert.equal(tree[0].children[0].children[0].files.length, 1);
});

test('缺少主分类的文件进入待确认', () => {
  const file = result('未知.txt', []);
  assert.deepEqual(primaryCategoryPath(file), ['待确认']);
  assert.equal(buildClassificationTree([file])[0].name, '待确认');
});
