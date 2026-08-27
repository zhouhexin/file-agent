// Excel 结构化预览只保存可展示的单元格事实，不执行公式、宏或外部资源。
export const XLSX_MAX_LOCAL_BYTES = 25 * 1024 * 1024;
export const XLSX_MAX_SHEETS = 30;
export const XLSX_MAX_CELLS = 100_000;
export const XLSX_ROW_PAGE_SIZE = 100;
export const XLSX_COLUMN_PAGE_SIZE = 50;

export type XlsxCellValue = string | number | boolean | null;

export type XlsxMergePreview = {
  range: string;
  startRow: number;
  startColumn: number;
  endRow: number;
  endColumn: number;
};

export type XlsxCellPreview = {
  row: number;
  column: number;
  address: string;
  rawValue: XlsxCellValue;
  displayValue: string;
  valueType: 'empty' | 'string' | 'number' | 'boolean' | 'date' | 'formula' | 'error';
  formula?: string;
  cachedResult?: XlsxCellValue;
  numberFormat?: string;
  mergeRange?: string;
};

export type XlsxSheetPreview = {
  name: string;
  rowCount: number;
  columnCount: number;
  frozenRows: number;
  frozenColumns: number;
  hidden: boolean;
  hiddenRows: number[];
  hiddenColumns: number[];
  merges: XlsxMergePreview[];
  cells: XlsxCellPreview[];
};

export type XlsxWorkbookPreview = {
  sheets: XlsxSheetPreview[];
  warnings: string[];
  truncated: boolean;
};

export type XlsxWorkerResponse =
  | { ok: true; workbook: XlsxWorkbookPreview }
  | { ok: false; message: string };

export function excelColumnLabel(column: number): string {
  // 列标由确定性换算生成，避免表格分页后把相对下标误当真实 Excel 列号。
  let current = Math.max(1, Math.trunc(column));
  let label = '';
  while (current > 0) {
    const remainder = (current - 1) % 26;
    label = String.fromCharCode(65 + remainder) + label;
    current = Math.floor((current - 1) / 26);
  }
  return label;
}

export function pageStart(page: number, pageSize: number): number {
  return Math.max(0, Math.trunc(page)) * pageSize + 1;
}

export function pageCount(total: number, pageSize: number): number {
  return Math.max(1, Math.ceil(Math.max(0, total) / pageSize));
}

function decimalPlaces(pattern: string): number {
  const match = /\.([0#]+)/.exec(pattern);
  return match ? match[1].length : 0;
}

export function formatXlsxDisplayValue(
  value: unknown,
  numberFormat: string | undefined,
  fallback = '',
): string {
  // ExcelJS 的 cell.text 不应用 numFmt；常见业务格式必须由确定性代码生成，不能交给 LLM 猜测。
  if (value === null || value === undefined) return '';
  if (value instanceof Date) {
    const year = value.getFullYear().toString().padStart(4, '0');
    const month = (value.getMonth() + 1).toString().padStart(2, '0');
    const day = value.getDate().toString().padStart(2, '0');
    const dateText = `${year}-${month}-${day}`;
    const pattern = (numberFormat ?? '').toLowerCase();
    if (!/[hs]/.test(pattern)) return dateText;
    const hours = value.getHours().toString().padStart(2, '0');
    const minutes = value.getMinutes().toString().padStart(2, '0');
    const seconds = value.getSeconds().toString().padStart(2, '0');
    return `${dateText} ${hours}:${minutes}:${seconds}`;
  }
  if (typeof value === 'boolean') return value ? 'TRUE' : 'FALSE';
  if (typeof value !== 'number') return fallback || String(value);

  const pattern = (numberFormat || 'General')
    .split(';', 1)[0]
    .replace(/\[[^\]]*]/g, '')
    .replace(/"([^"]*)"/g, '$1');
  if (pattern.toLowerCase() === 'general') return fallback || String(value);
  const decimals = decimalPlaces(pattern);
  if (pattern.includes('%')) return `${(value * 100).toFixed(decimals)}%`;
  const integerPattern = pattern.split('.', 1)[0];
  const useThousands = integerPattern.includes(',');
  const zeroWidth = (integerPattern.match(/0/g) ?? []).length;
  let numberText: string;
  if (decimals === 0 && zeroWidth > 1 && !integerPattern.includes('#') && !useThousands) {
    numberText = Math.round(value).toString().padStart(zeroWidth, '0');
  } else {
    numberText = value.toLocaleString('en-US', {
      useGrouping: useThousands,
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
    });
  }
  const currency = ['¥', '￥', '$', '€', '£'].find((symbol) => pattern.includes(symbol)) ?? '';
  return `${currency}${numberText}`;
}
