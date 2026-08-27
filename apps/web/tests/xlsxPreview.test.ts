// 只测试结构化预览的确定性坐标换算；工作簿解析由构建和后端固定样例共同验证。
import assert from 'node:assert/strict';
import test from 'node:test';

import {
  excelColumnLabel,
  formatXlsxDisplayValue,
  pageCount,
  pageStart,
} from '../src/features/chat/xlsxPreview.ts';

test('Excel 列标在分页后仍使用真实工作簿坐标', () => {
  assert.equal(excelColumnLabel(1), 'A');
  assert.equal(excelColumnLabel(26), 'Z');
  assert.equal(excelColumnLabel(27), 'AA');
  assert.equal(excelColumnLabel(703), 'AAA');
});

test('结构化预览分页不会静默丢弃 200 行后的内容', () => {
  assert.equal(pageCount(251, 100), 3);
  assert.equal(pageStart(2, 100), 201);
});

test('结构化预览按 Excel 数字格式显示百分比、货币和前导零', () => {
  assert.equal(formatXlsxDisplayValue(0.125, '0.0%'), '12.5%');
  assert.equal(formatXlsxDisplayValue(1200, '¥#,##0.00'), '¥1,200.00');
  assert.equal(formatXlsxDisplayValue(7, '000000'), '000007');
});
