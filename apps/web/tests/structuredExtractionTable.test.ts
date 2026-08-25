import assert from 'node:assert/strict';
import test from 'node:test';

import { buildStructuredExtractionTableLayout } from '../src/features/chat/structuredExtractionTable.ts';


test('结构化表格按自定义业务字段数量生成全部显示列', () => {
  const layout = buildStructuredExtractionTableLayout({
    field_schema: [
      { key: 'applicant', label: '申请人', field_type: 'person_name', required: false },
      { key: 'amount', label: '资助金额', field_type: 'money', required: false },
      { key: 'date', label: '申请日期', field_type: 'date', required: false },
      { key: 'usage', label: '使用情况摘要', field_type: 'string', required: false },
      { key: 'remark', label: '备注', field_type: 'string', required: false },
    ],
  });

  assert.equal(layout.businessColumnCount, 5);
  assert.equal(layout.totalColumnCount, 6);
  assert.equal(layout.minimumWidth, 920);
});


test('单业务列仍保持回执最小可读宽度', () => {
  const layout = buildStructuredExtractionTableLayout({
    field_schema: [
      { key: 'name', label: '姓名', field_type: 'person_name', required: false },
    ],
  });

  assert.deepEqual(layout, {
    businessColumnCount: 1,
    totalColumnCount: 2,
    minimumWidth: 620,
  });
});
