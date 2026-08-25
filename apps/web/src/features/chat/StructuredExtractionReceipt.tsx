import { useState } from 'react';

import { fetchDocumentArtifactBlob } from '../../api/client';
import type { StructuredExtractionResult } from '../../types';
import { buildStructuredExtractionTableLayout } from './structuredExtractionTable';


type StructuredExtractionReceiptProps = {
  result: StructuredExtractionResult;
  token?: string;
};


export function StructuredExtractionReceipt({ result, token }: StructuredExtractionReceiptProps) {
  const [downloadError, setDownloadError] = useState('');
  const [downloading, setDownloading] = useState(false);
  const showJson = result.presentation === 'JSON';
  const showText = result.presentation === 'TEXT';
  const reviewNotes = buildReviewNotes(result);
  const dateRangeNote = buildDateRangeNote(result);
  const moneyTotal = buildCompleteMoneyTotal(result);
  const tableLayout = buildStructuredExtractionTableLayout(result);
  return (
    <section className="structured-extraction-card">
      <header>
        <div>
          <strong>已完成图片识别</strong>
          <span className={`structured-quality structured-quality--${result.quality_band.toLowerCase()}`}>
            {qualityLabel(result.quality_band)}
          </span>
        </div>
        <small>
          共登记 {result.record_count} 条记录
          {result.review_count > 0 ? ` · ${result.review_count} 个字段需要复核` : ''}
          {' · '}
          原始文件未修改
        </small>
      </header>

      <div className="structured-extraction-heading">
        <strong>识别结果汇总</strong>
        <small>* 表示可辨认但需要人工复核；“—”表示没有取得足够证据</small>
      </div>

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
                    <dd>{formatCellValue(record.fields[field.key], field.field_type)}</dd>
                  </div>
                ))}
              </dl>
            </article>
          ))}
        </div>
      ) : (
        <div className="structured-extraction-table-wrap">
          <table
            aria-colcount={tableLayout.totalColumnCount}
            className="structured-extraction-table"
            style={{ minWidth: `${tableLayout.minimumWidth}px` }}
          >
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
                        <span>{formatCellValue(cell, field.field_type)}</span>
                        {cell?.evidence?.page_number ? <small>第 {cell.evidence.page_number} 页</small> : null}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
            {moneyTotal ? (
              <tfoot>
                <tr>
                  <th>合计</th>
                  {result.field_schema.map((field) => (
                    <td key={field.key}>{field.key === moneyTotal.fieldKey ? moneyTotal.label : '—'}</td>
                  ))}
                </tr>
              </tfoot>
            ) : null}
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

      {reviewNotes.length > 0 || dateRangeNote ? (
        <section className="structured-extraction-review" aria-label="识别说明">
          <strong>说明</strong>
          <ul>
            {reviewNotes.map((note) => <li key={note.key}>{note.text}</li>)}
            {dateRangeNote ? <li>{dateRangeNote}</li> : null}
          </ul>
        </section>
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


function formatCellValue(
  cell: StructuredExtractionResult['records'][number]['fields'][string] | undefined,
  fieldType: string,
): string {
  if (!cell) return '—';
  const normalizedMissing = cell.normalized_value === null
    || cell.normalized_value === undefined
    || cell.normalized_value === '';
  const rawMissing = cell.raw_text === null || cell.raw_text === undefined || cell.raw_text === '';
  const candidate = normalizedMissing ? (rawMissing ? null : cell.raw_text) : cell.normalized_value;
  if (candidate === null) return '—';
  const displayed = formatTypedValue(candidate, fieldType);
  if (cell.status === 'NEEDS_REVIEW' || cell.status === 'CONFLICTED') {
    return `${displayed}*`;
  }
  return displayed;
}


function formatTypedValue(value: unknown, fieldType: string): string {
  if (fieldType === 'money' && typeof value === 'object' && value !== null && 'amount' in value) {
    const amount = Number((value as { amount: unknown }).amount);
    if (Number.isFinite(amount)) return amount.toLocaleString('zh-CN', { maximumFractionDigits: 2 });
  }
  if (fieldType === 'date' && typeof value === 'string') {
    const matched = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
    if (matched) return `${matched[1]}.${Number(matched[2])}.${Number(matched[3])}`;
  }
  if (typeof value === 'number') return value.toLocaleString('zh-CN');
  if (typeof value === 'string') return value;
  return JSON.stringify(value);
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


function buildReviewNotes(result: StructuredExtractionResult): Array<{ key: string; text: string }> {
  return result.review_items.map((item, index) => {
    const raw = item.raw_text === null || item.raw_text === undefined || item.raw_text === ''
      ? ''
      : `，当前可辨认为“${String(item.raw_text)}”`;
    const reasons = item.reason_codes.map(reviewReasonLabel);
    const reason = reasons.find(Boolean) || '需要结合原图人工确认';
    return {
      key: `${item.record_index}-${item.field_key}-${index}`,
      text: `第 ${item.record_index} 条${item.field_label || item.field_key}${raw}：${reason}`,
    };
  });
}


function reviewReasonLabel(code: string): string {
  const labels: Record<string, string> = {
    FIELD_MISSING: 'OCR 未形成可独立校验的文字块',
    NORMALIZATION_FAILED: '已取得原文，但格式不能安全归一化',
    LOW_CONFIDENCE: '手写内容清晰度较低，已在表格中标注 *',
    EVIDENCE_REQUIRED: '缺少可定位的原图证据',
    EVIDENCE_TEXT_MISMATCH: '候选值与 OCR 原文不完全一致',
    EVIDENCE_ELEMENT_NOT_FOUND: '原图证据块未找到',
    EVIDENCE_SCOPE_MISMATCH: '证据不属于本次图片，已拒绝采用',
  };
  return labels[code] || '';
}


function buildDateRangeNote(result: StructuredExtractionResult): string | null {
  const dateFields = new Set(
    result.field_schema.filter((field) => field.field_type === 'date').map((field) => field.key),
  );
  const dates = result.records.flatMap((record) => Array.from(dateFields).flatMap((key) => {
    const value = record.fields[key]?.normalized_value;
    return typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value) ? [value] : [];
  })).sort();
  if (dates.length < 2) return null;
  const first = formatChineseDate(dates[0]);
  const last = formatChineseDate(dates[dates.length - 1]);
  return first === last ? `已确认的申请日期均为 ${first}` : `已确认的申请日期范围为 ${first} 至 ${last}`;
}


function formatChineseDate(value: string): string {
  const [year, month, day] = value.split('-').map(Number);
  return `${year}年${month}月${day}日`;
}


function buildCompleteMoneyTotal(
  result: StructuredExtractionResult,
): { fieldKey: string; label: string } | null {
  const moneyField = result.field_schema.find((field) => field.field_type === 'money');
  if (!moneyField || result.records.length === 0) return null;
  const amounts: number[] = [];
  for (const record of result.records) {
    const cell = record.fields[moneyField.key];
    if (!cell || !['NORMALIZED', 'EXTRACTED'].includes(cell.status)) return null;
    const value = cell.normalized_value;
    if (typeof value !== 'object' || value === null || !('amount' in value)) return null;
    const amount = Number((value as { amount: unknown }).amount);
    if (!Number.isFinite(amount)) return null;
    amounts.push(amount);
  }
  const total = amounts.reduce((sum, amount) => sum + amount, 0);
  return { fieldKey: moneyField.key, label: total.toLocaleString('zh-CN', { maximumFractionDigits: 2 }) };
}
