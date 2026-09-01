import type { FieldTableResult } from '../../types';


type FieldTableReceiptProps = {
  result: FieldTableResult;
};


export function FieldTableReceipt({ result }: FieldTableReceiptProps) {
  return (
    <section className="field-table-receipt">
      <header>
        <strong>图片字段识别结果</strong>
        <small>
          已识别 {result.fields.length - result.missing_count} 个字段
          {result.missing_count > 0 ? ` · ${result.missing_count} 个字段未识别` : ''}
          {' · 原始文件未修改'}
        </small>
      </header>
      <div className="structured-extraction-table-wrap">
        <table className="structured-extraction-table">
          <thead>
            <tr>
              {result.fields.map((field) => <th key={field.key}>{field.label}</th>)}
            </tr>
          </thead>
          <tbody>
            <tr>
              {result.fields.map((field) => (
                <td
                  className={field.status === 'MISSING' ? 'structured-cell--review' : undefined}
                  key={field.key}
                >
                  <span>{formatValue(field.value)}</span>
                  {field.page_number ? <small>第 {field.page_number} 页</small> : null}
                </td>
              ))}
            </tr>
          </tbody>
        </table>
      </div>
      {result.missing_count > 0 ? (
        <small className="field-table-note">“—”表示当前 OCR 正文中没有取得足够证据。</small>
      ) : null}
    </section>
  );
}


function formatValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—';
  if (typeof value === 'number') return value.toLocaleString('zh-CN');
  if (typeof value === 'string') return value;
  return JSON.stringify(value);
}
