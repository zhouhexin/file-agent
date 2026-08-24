import { useState } from 'react';

import { fetchDocumentArtifactBlob } from '../../api/client';
import type { StructuredExtractionResult } from '../../types';


type StructuredExtractionReceiptProps = {
  result: StructuredExtractionResult;
  token?: string;
};


export function StructuredExtractionReceipt({ result, token }: StructuredExtractionReceiptProps) {
  const [downloadError, setDownloadError] = useState('');
  const [downloading, setDownloading] = useState(false);
  const showJson = result.presentation === 'JSON';
  const showText = result.presentation === 'TEXT';
  return (
    <section className="structured-extraction-card">
      <header>
        <div>
          <strong>已提取 {result.record_count} 条记录</strong>
          <span className={`structured-quality structured-quality--${result.quality_band.toLowerCase()}`}>
            {qualityLabel(result.quality_band)}
          </span>
        </div>
        <small>
          {result.review_count > 0 ? `${result.review_count} 个字段需要复核 · ` : ''}
          原始文件未修改
        </small>
      </header>

      {showJson ? (
        <pre className="structured-extraction-json">
          {JSON.stringify(toDisplayJson(result), null, 2)}
        </pre>
      ) : showText ? (
        <div className="structured-extraction-text">
          {result.records.map((record) => (
            <article key={record.record_index}>
              <strong>第 {record.record_index} 条</strong>
              <dl>
                {result.field_schema.map((field) => (
                  <div key={field.key}>
                    <dt>{field.label}</dt>
                    <dd>{formatCellValue(record.fields[field.key])}</dd>
                  </div>
                ))}
              </dl>
            </article>
          ))}
        </div>
      ) : (
        <div className="structured-extraction-table-wrap">
          <table className="structured-extraction-table">
            <thead>
              <tr>
                <th>序号</th>
                {result.field_schema.map((field) => <th key={field.key}>{field.label}</th>)}
              </tr>
            </thead>
            <tbody>
              {result.records.map((record) => (
                <tr key={record.record_index}>
                  <td>{record.record_index}</td>
                  {result.field_schema.map((field) => {
                    const cell = record.fields[field.key];
                    const needsReview = cell?.status === 'NEEDS_REVIEW'
                      || cell?.status === 'MISSING'
                      || cell?.status === 'CONFLICTED';
                    return (
                      <td className={needsReview ? 'structured-cell--review' : undefined} key={field.key}>
                        <span>{formatCellValue(cell)}</span>
                        {cell?.evidence?.page_number ? <small>第 {cell.evidence.page_number} 页</small> : null}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {result.export_artifact ? (
        <div className="structured-extraction-export-note">
          <button
            type="button"
            disabled={!token || downloading}
            onClick={() => void downloadArtifact()}
          >
            {downloading ? '正在生成下载…' : `下载 ${result.export_artifact.format}`}
          </button>
          <span>页面同时保留表格预览</span>
          {downloadError ? <small role="alert">{downloadError}</small> : null}
        </div>
      ) : null}

      {result.review_items.length > 0 ? (
        <details className="structured-extraction-review">
          <summary>查看待复核字段（{result.review_items.length}）</summary>
          <ul>
            {result.review_items.map((item, index) => (
              <li key={`${item.record_index}-${item.field_key}-${index}`}>
                第 {item.record_index} 条 · {item.field_label || item.field_key}
                {item.page_number ? ` · 第 ${item.page_number} 页` : ''}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </section>
  );

  async function downloadArtifact() {
    if (!token || !result.export_artifact) return;
    setDownloading(true);
    setDownloadError('');
    try {
      const blob = await fetchDocumentArtifactBlob(
        token,
        result.document_id,
        result.export_artifact.artifact_id,
      );
      const objectUrl = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = objectUrl;
      link.download = result.export_artifact.filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000);
    } catch {
      setDownloadError('下载失败，请稍后重试。');
    } finally {
      setDownloading(false);
    }
  }
}


function formatCellValue(cell: StructuredExtractionResult['records'][number]['fields'][string] | undefined): string {
  if (!cell || cell.normalized_value === null || cell.normalized_value === undefined || cell.normalized_value === '') {
    return '待确认';
  }
  return typeof cell.normalized_value === 'string'
    ? cell.normalized_value
    : JSON.stringify(cell.normalized_value);
}


function toDisplayJson(result: StructuredExtractionResult) {
  return result.records.map((record) => ({
    record_index: record.record_index,
    ...Object.fromEntries(
      result.field_schema.map((field) => [
        field.key,
        record.fields[field.key]?.normalized_value ?? null,
      ]),
    ),
  }));
}


function qualityLabel(value: string): string {
  if (value === 'HIGH') return '高置信度';
  if (value === 'MEDIUM') return '部分待复核';
  return '需要复核';
}
