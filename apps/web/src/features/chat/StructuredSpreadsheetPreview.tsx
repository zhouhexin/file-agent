// Excel 预览按真实 Sheet、行号、列标和单元格地址展示，不从制表符文本反推结构。
import { useEffect, useMemo, useState } from 'react';
import { AlertCircle, ChevronLeft, ChevronRight, LoaderCircle } from 'lucide-react';

import {
  excelColumnLabel,
  pageCount,
  pageStart,
  XLSX_COLUMN_PAGE_SIZE,
  XLSX_ROW_PAGE_SIZE,
  type XlsxCellPreview,
  type XlsxSheetPreview,
  type XlsxWorkbookPreview,
  type XlsxWorkerResponse,
} from './xlsxPreview';

type StructuredSpreadsheetPreviewProps = {
  blob: Blob;
  filename: string;
};

function cellTitle(cell: XlsxCellPreview | undefined): string {
  if (!cell) return '';
  const details = [`单元格 ${cell.address}`, `显示值：${cell.displayValue || '（空）'}`];
  if (cell.formula) details.push(`公式：${cell.formula}`);
  if (cell.formula && cell.cachedResult === undefined) details.push('工作簿未保存可用计算结果');
  if (cell.numberFormat) details.push(`格式：${cell.numberFormat}`);
  return details.join('\n');
}

function SheetGrid({ sheet }: { sheet: XlsxSheetPreview }) {
  const [rowPage, setRowPage] = useState(0);
  const [columnPage, setColumnPage] = useState(0);
  const [showFormulas, setShowFormulas] = useState(false);

  useEffect(() => {
    setRowPage(0);
    setColumnPage(0);
  }, [sheet.name]);

  const rowPages = pageCount(sheet.rowCount, XLSX_ROW_PAGE_SIZE);
  const columnPages = pageCount(sheet.columnCount, XLSX_COLUMN_PAGE_SIZE);
  const firstRow = pageStart(rowPage, XLSX_ROW_PAGE_SIZE);
  const lastRow = Math.min(sheet.rowCount, firstRow + XLSX_ROW_PAGE_SIZE - 1);
  const firstColumn = pageStart(columnPage, XLSX_COLUMN_PAGE_SIZE);
  const lastColumn = Math.min(sheet.columnCount, firstColumn + XLSX_COLUMN_PAGE_SIZE - 1);
  const cells = useMemo(
    () => new Map(sheet.cells.map((cell) => [`${cell.row}:${cell.column}`, cell])),
    [sheet.cells],
  );
  const rows = Array.from({ length: Math.max(0, lastRow - firstRow + 1) }, (_, index) => firstRow + index);
  const columns = Array.from(
    { length: Math.max(0, lastColumn - firstColumn + 1) },
    (_, index) => firstColumn + index,
  );
  const visibleMerges = useMemo(() => {
    const mergeCells = new Map<string, {
      master: boolean;
      rowSpan: number;
      columnSpan: number;
      range: string;
    }>();
    for (const merge of sheet.merges) {
      // 合并主格不在当前分页区域时按普通空格展示，避免跨页合并造成列错位。
      if (
        merge.startRow < firstRow || merge.startRow > lastRow
        || merge.startColumn < firstColumn || merge.startColumn > lastColumn
      ) continue;
      const endRow = Math.min(merge.endRow, lastRow);
      const endColumn = Math.min(merge.endColumn, lastColumn);
      for (let row = merge.startRow; row <= endRow; row += 1) {
        for (let column = merge.startColumn; column <= endColumn; column += 1) {
          mergeCells.set(`${row}:${column}`, {
            master: row === merge.startRow && column === merge.startColumn,
            rowSpan: endRow - merge.startRow + 1,
            columnSpan: endColumn - merge.startColumn + 1,
            range: merge.range,
          });
        }
      }
    }
    return mergeCells;
  }, [firstColumn, firstRow, lastColumn, lastRow, sheet.merges]);

  if (!sheet.rowCount || !sheet.columnCount) {
    return <p className="duplicate-comparison-state">该工作表没有可展示的单元格。</p>;
  }

  return (
    <div className="duplicate-xlsx-sheet">
      <div className="duplicate-xlsx-toolbar">
        <span>行 {firstRow}–{lastRow} / {sheet.rowCount}</span>
        <button disabled={rowPage === 0} onClick={() => setRowPage((page) => page - 1)} type="button">
          <ChevronLeft size={15} />上一段行
        </button>
        <button disabled={rowPage + 1 >= rowPages} onClick={() => setRowPage((page) => page + 1)} type="button">
          下一段行<ChevronRight size={15} />
        </button>
        <span>列 {excelColumnLabel(firstColumn)}–{excelColumnLabel(lastColumn)} / {excelColumnLabel(sheet.columnCount)}</span>
        <button disabled={columnPage === 0} onClick={() => setColumnPage((page) => page - 1)} type="button">
          <ChevronLeft size={15} />上一段列
        </button>
        <button disabled={columnPage + 1 >= columnPages} onClick={() => setColumnPage((page) => page + 1)} type="button">
          下一段列<ChevronRight size={15} />
        </button>
        <label>
          <input checked={showFormulas} onChange={(event) => setShowFormulas(event.target.checked)} type="checkbox" />
          显示公式
        </label>
      </div>
      <div className="duplicate-comparison-sheet duplicate-xlsx-grid">
        <table>
          <thead>
            <tr>
              <th className="duplicate-xlsx-corner" />
              {columns.map((column) => <th key={column}>{excelColumnLabel(column)}</th>)}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row}>
                <th>{row}</th>
                {columns.map((column) => {
                  const cell = cells.get(`${row}:${column}`);
                  const merge = visibleMerges.get(`${row}:${column}`);
                  if (merge && !merge.master) return null;
                  const value = showFormulas && cell?.formula ? cell.formula : cell?.displayValue ?? '';
                  return (
                    <td
                      className={cell?.formula && cell.cachedResult === undefined ? 'duplicate-xlsx-cell--uncalculated' : undefined}
                      colSpan={merge?.columnSpan}
                      key={column}
                      rowSpan={merge?.rowSpan}
                      title={cellTitle(cell)}
                    >
                      {value}
                      {merge?.range ? <small>{merge.range}</small> : null}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function StructuredSpreadsheetPreview({ blob, filename }: StructuredSpreadsheetPreviewProps) {
  const [workbook, setWorkbook] = useState<XlsxWorkbookPreview | null>(null);
  const [activeSheet, setActiveSheet] = useState(0);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    const worker = new Worker(new URL('./xlsxPreview.worker.ts', import.meta.url), { type: 'module' });
    worker.onmessage = (event: MessageEvent<XlsxWorkerResponse>) => {
      if (cancelled) return;
      if (event.data.ok) {
        setWorkbook(event.data.workbook);
        setActiveSheet(0);
      } else {
        setError(event.data.message);
      }
    };
    worker.onerror = () => {
      if (!cancelled) setError('XLSX 本地结构化预览启动失败。');
    };
    void blob.arrayBuffer()
      .then((buffer) => {
        if (!cancelled) worker.postMessage(buffer, [buffer]);
      })
      .catch(() => {
        if (!cancelled) setError('无法读取 XLSX 文件。');
      });
    return () => {
      cancelled = true;
      worker.terminate();
    };
  }, [blob]);

  if (error) {
    return <p className="duplicate-comparison-state"><AlertCircle size={20} />{error}</p>;
  }
  if (!workbook) {
    return <p className="duplicate-comparison-state"><LoaderCircle className="spin" size={20} />正在本地解析 XLSX…</p>;
  }
  if (!workbook.sheets.length) {
    return <p className="duplicate-comparison-state">{filename} 没有可展示的工作表。</p>;
  }
  const sheet = workbook.sheets[Math.min(activeSheet, workbook.sheets.length - 1)];
  return (
    <div className="duplicate-xlsx-preview">
      <nav aria-label={`${filename} 工作表`} className="duplicate-xlsx-tabs">
        {workbook.sheets.map((item, index) => (
          <button
            aria-pressed={index === activeSheet}
            className={index === activeSheet ? 'is-active' : undefined}
            key={`${item.name}-${index}`}
            onClick={() => setActiveSheet(index)}
            type="button"
          >
            {item.name}{item.hidden ? '（隐藏）' : ''}
          </button>
        ))}
      </nav>
      {workbook.warnings.map((warning) => (
        <p className="duplicate-xlsx-warning" key={warning}><AlertCircle size={15} />{warning}</p>
      ))}
      <SheetGrid sheet={sheet} />
    </div>
  );
}
