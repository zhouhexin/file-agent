/// <reference lib="webworker" />

// 工作簿只在隔离 Worker 中读取；解析过程不会执行公式、宏、链接或数据连接。
import ExcelJS from 'exceljs';

import {
  formatXlsxDisplayValue,
  XLSX_MAX_CELLS,
  XLSX_MAX_SHEETS,
  type XlsxCellPreview,
  type XlsxCellValue,
  type XlsxMergePreview,
  type XlsxSheetPreview,
  type XlsxWorkerResponse,
} from './xlsxPreview';

type FormulaValue = {
  formula?: string;
  sharedFormula?: string;
  result?: unknown;
};

function serializableValue(value: unknown): XlsxCellValue {
  if (value === null || value === undefined) return null;
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return value;
  if (value instanceof Date) return value.toISOString();
  if (typeof value === 'object') {
    const richText = (value as { richText?: Array<{ text?: string }> }).richText;
    if (Array.isArray(richText)) return richText.map((part) => part.text ?? '').join('');
    const hyperlinkText = (value as { text?: unknown }).text;
    if (typeof hyperlinkText === 'string') return hyperlinkText;
    const error = (value as { error?: unknown }).error;
    if (typeof error === 'string') return error;
  }
  return String(value);
}

function cellKind(cell: ExcelJS.Cell, formula: string | undefined): XlsxCellPreview['valueType'] {
  if (formula) return 'formula';
  if (cell.value === null || cell.value === undefined) return 'empty';
  if (cell.value instanceof Date) return 'date';
  if (cell.type === ExcelJS.ValueType.Error) return 'error';
  if (typeof cell.value === 'number') return 'number';
  if (typeof cell.value === 'boolean') return 'boolean';
  return 'string';
}

function addressColumn(label: string): number {
  return [...label.toUpperCase()].reduce((value, char) => value * 26 + char.charCodeAt(0) - 64, 0);
}

function parseMerge(range: string): XlsxMergePreview | null {
  const match = /^([A-Z]+)(\d+):([A-Z]+)(\d+)$/i.exec(range);
  if (!match) return null;
  return {
    range,
    startColumn: addressColumn(match[1]),
    startRow: Number(match[2]),
    endColumn: addressColumn(match[3]),
    endRow: Number(match[4]),
  };
}

function formulaValue(cell: ExcelJS.Cell): FormulaValue | null {
  if (!cell.value || typeof cell.value !== 'object') return null;
  const value = cell.value as FormulaValue;
  return value.formula || value.sharedFormula ? value : null;
}

function buildSheet(worksheet: ExcelJS.Worksheet, remainingCells: number): XlsxSheetPreview {
  const modelMerges = ((worksheet.model as { merges?: string[] }).merges ?? [])
    .map(parseMerge)
    .filter((merge): merge is XlsxMergePreview => merge !== null);
  const mergeByMaster = new Map(modelMerges.map((merge) => [
    `${merge.startRow}:${merge.startColumn}`,
    merge.range,
  ]));
  const cells: XlsxCellPreview[] = [];
  const hiddenRows: number[] = [];
  let maxRow = 0;
  let maxColumn = 0;

  worksheet.eachRow({ includeEmpty: false }, (row, rowNumber) => {
    if (row.hidden) hiddenRows.push(rowNumber);
    if (cells.length >= remainingCells) return;
    row.eachCell({ includeEmpty: false }, (cell, columnNumber) => {
      if (cells.length >= remainingCells) return;
      // 合并区域的从属格与主格共享值，只保存主格，避免预览重复内容。
      if (cell.isMerged && cell.master.address !== cell.address) return;
      const formulaEntry = formulaValue(cell);
      const formula = formulaEntry?.formula ?? formulaEntry?.sharedFormula;
      const cachedResult = formulaEntry && formulaEntry.result !== undefined && formulaEntry.result !== null
        ? serializableValue(formulaEntry.result)
        : undefined;
      const rawValue = formulaEntry ? null : serializableValue(cell.value);
      const displaySource = formulaEntry ? formulaEntry.result : cell.value;
      const displayValue = formulaEntry && cachedResult === undefined
        ? `=${formula ?? ''}`
        : formatXlsxDisplayValue(displaySource, cell.numFmt, cell.text || String(cachedResult ?? rawValue ?? ''));
      cells.push({
        row: rowNumber,
        column: columnNumber,
        address: cell.address,
        rawValue,
        displayValue,
        valueType: cellKind(cell, formula),
        formula: formula ? `=${formula.replace(/^=/, '')}` : undefined,
        cachedResult,
        numberFormat: cell.numFmt || undefined,
        mergeRange: mergeByMaster.get(`${rowNumber}:${columnNumber}`),
      });
      maxRow = Math.max(maxRow, rowNumber);
      maxColumn = Math.max(maxColumn, columnNumber);
    });
  });
  for (const merge of modelMerges) {
    maxRow = Math.max(maxRow, merge.endRow);
    maxColumn = Math.max(maxColumn, merge.endColumn);
  }
  const frozenView = worksheet.views.find((view) => view.state === 'frozen');
  const frozenSplits = frozenView as ({ xSplit?: number; ySplit?: number } | undefined);
  return {
    name: worksheet.name,
    rowCount: maxRow,
    columnCount: maxColumn,
    frozenRows: frozenSplits?.ySplit ?? 0,
    frozenColumns: frozenSplits?.xSplit ?? 0,
    hidden: worksheet.state !== 'visible',
    hiddenRows,
    hiddenColumns: worksheet.columns
      .map((column, index) => column?.hidden ? index + 1 : null)
      .filter((column): column is number => column !== null),
    merges: modelMerges,
    cells,
  };
}

self.onmessage = async (event: MessageEvent<ArrayBuffer>) => {
  try {
    const workbook = new ExcelJS.Workbook();
    // ExcelJS 的浏览器加载器只解析 OOXML；它不会启动 Excel 或执行工作簿代码。
    await workbook.xlsx.load(event.data);
    const sheets: XlsxSheetPreview[] = [];
    const warnings: string[] = [];
    let remainingCells = XLSX_MAX_CELLS;
    for (const worksheet of workbook.worksheets.slice(0, XLSX_MAX_SHEETS)) {
      const sheet = buildSheet(worksheet, remainingCells);
      sheets.push(sheet);
      remainingCells -= sheet.cells.length;
      if (remainingCells <= 0) break;
    }
    if (workbook.worksheets.length > XLSX_MAX_SHEETS) {
      warnings.push(`工作簿包含 ${workbook.worksheets.length} 个工作表，当前安全预览只解析前 ${XLSX_MAX_SHEETS} 个。`);
    }
    if (remainingCells <= 0) {
      warnings.push(`工作簿有效单元格超过 ${XLSX_MAX_CELLS.toLocaleString()} 个，当前安全预览只展示已解析部分。`);
    }
    const response: XlsxWorkerResponse = {
      ok: true,
      workbook: {
        sheets,
        warnings,
        truncated: warnings.length > 0,
      },
    };
    self.postMessage(response);
  } catch {
    const response: XlsxWorkerResponse = {
      ok: false,
      message: 'XLSX 本地结构化解析失败，文件可能已损坏或包含暂不支持的结构。',
    };
    self.postMessage(response);
  }
};

export {};
