// WorkBuddy 重复候选的独立只读页面：仅以固定快照读取，不在浏览器中提交任何决定。
import { useEffect, useRef, useState } from 'react';

import { fetchIngestDuplicateComparisonBlob, getIngestDuplicateComparison, getIngestDuplicateComparisonPreview } from '../../api/client';
import type { IngestDuplicateComparison, IngestDuplicateComparisonSide } from '../../types';
import { PreviewPane, type PreviewSide, TEXT_EXTENSIONS, MAX_LOCAL_DOCX_BYTES } from './DuplicateComparisonDialog';
import { XLSX_MAX_LOCAL_BYTES } from './xlsxPreview';

type Props = { token: string; query: URLSearchParams; onBack: () => void };
type Side = 'UPLOAD' | 'CANDIDATE';
const DOWNLOAD_LIMIT_BYTES = 128 * 1024 * 1024;
const RAW_PREVIEW_LIMIT_BYTES = 25 * 1024 * 1024;

function validId(value: string | null): boolean { return Boolean(value && /^[a-zA-Z0-9-]{1,36}$/.test(value)); }
function extension(filename: string): string { return filename.split('.').pop()?.toLowerCase() ?? ''; }

function unavailableSide(value: IngestDuplicateComparisonSide): PreviewSide {
  return { filename: value.filename, size: value.size_bytes, status: 'unavailable', mode: null, message: value.reason_code ?? '当前文件没有可用的受控预览或下载资源。' };
}

function browserPreview(value: IngestDuplicateComparisonSide, blob: Blob): PreviewSide {
  const suffix = extension(value.filename);
  const objectUrl = URL.createObjectURL(blob);
  if (blob.type.startsWith('image/')) return { filename: value.filename, size: blob.size, status: 'ready', mode: 'image', objectUrl };
  if (blob.type === 'application/pdf' || suffix === 'pdf') return { filename: value.filename, size: blob.size, status: 'ready', mode: 'pdf', objectUrl };
  if (suffix === 'docx') {
    if (blob.size > MAX_LOCAL_DOCX_BYTES) return { filename: value.filename, size: blob.size, status: 'unavailable', mode: 'download', objectUrl, message: 'DOCX 超过 20 MB 的本地预览上限，可下载后在本地查看。' };
    return { filename: value.filename, size: blob.size, status: 'ready', mode: 'docx', docxBlob: blob, objectUrl, message: 'DOCX 仅在当前浏览器本地解析，不会发送给第三方。' };
  }
  if (suffix === 'xlsx') {
    if (blob.size > XLSX_MAX_LOCAL_BYTES) return { filename: value.filename, size: blob.size, status: 'unavailable', mode: 'download', objectUrl, message: 'XLSX 超过 25 MB 的本地预览上限，可下载后在本地查看。' };
    return { filename: value.filename, size: blob.size, status: 'ready', mode: 'xlsx', xlsxBlob: blob, objectUrl, message: 'XLSX 仅在当前浏览器本地结构化解析，不执行公式、宏或外部链接。' };
  }
  if (blob.type.startsWith('text/') || TEXT_EXTENSIONS.has(suffix)) return { filename: value.filename, size: blob.size, status: 'ready', mode: 'text', text: '', objectUrl, message: '正在读取受控文本预览。' };
  return { filename: value.filename, size: blob.size, status: 'unavailable', mode: 'download', objectUrl, message: '该格式没有浏览器安全预览，仍可受控下载后在本地查看。' };
}

export function IngestDuplicateComparisonPage({ token, query, onBack }: Props) {
  const itemId = query.get('item_id'); const reviewId = query.get('review_id'); const candidateId = query.get('candidate_id');
  const revision = Number(query.get('review_revision')); const group = query.get('group_revision');
  const [data, setData] = useState<IngestDuplicateComparison | null>(null);
  const [error, setError] = useState('');
  const [actionError, setActionError] = useState('');
  const [previewSides, setPreviewSides] = useState<Partial<Record<Side, PreviewSide>>>({});
  const [downloading, setDownloading] = useState(false);
  const objectUrls = useRef<string[]>([]);

  useEffect(() => () => objectUrls.current.forEach((url) => URL.revokeObjectURL(url)), []);
  useEffect(() => {
    if (!validId(itemId) || !validId(reviewId) || !validId(candidateId) || !Number.isInteger(revision) || revision < 1 || (group !== null && (!Number.isInteger(Number(group)) || Number(group) < 1))) { setError('对比链接无效或已损坏。'); return; }
    getIngestDuplicateComparison(token, itemId!, { review_id: reviewId!, review_revision: revision, candidate_id: candidateId!, group_revision: group === null ? null : Number(group) })
      .then((response) => { setData(response); setPreviewSides({ UPLOAD: unavailableSide(response.upload), CANDIDATE: unavailableSide(response.candidate) }); })
      .catch((value) => setError(value instanceof Error ? value.message : '无法读取重复候选。'));
  }, [token, itemId, reviewId, candidateId, revision, group]);

  async function loadPreview(side: Side) {
    if (!data) return;
    const value = side === 'UPLOAD' ? data.upload : data.candidate;
    if (!value.download_available && value.preview_status !== 'AVAILABLE') return;
    const suffix = extension(value.filename);
    const needsStoredPreview = value.preview_mode === 'SECTIONS'
      || (['IMAGE', 'PDF', 'TEXT'].includes(value.preview_mode) && (value.size_bytes ?? 0) > RAW_PREVIEW_LIMIT_BYTES)
      || (suffix === 'docx' && (value.size_bytes ?? 0) > MAX_LOCAL_DOCX_BYTES)
      || (suffix === 'xlsx' && (value.size_bytes ?? 0) > XLSX_MAX_LOCAL_BYTES);
    if (needsStoredPreview) {
      setPreviewSides((current) => ({ ...current, [side]: { filename: value.filename, size: value.size_bytes, status: 'loading', mode: null } }));
      try {
        const preview = await getIngestDuplicateComparisonPreview(token, data.item_id, { review_id: data.review_id, review_revision: data.review_revision, candidate_id: data.candidate_id, group_revision: data.group_revision, snapshot_id: data.snapshot_id, side });
        setPreviewSides((current) => ({ ...current, [side]: { filename: preview.filename, size: value.size_bytes, status: 'ready', mode: 'sections', sections: preview.sections, message: preview.truncated ? '正文较长，当前只展示前 100,000 个字符。' : undefined } }));
      } catch (reason) {
        setPreviewSides((current) => ({ ...current, [side]: { ...unavailableSide(value), mode: 'download', message: reason instanceof Error ? `${reason.message}；可下载后在本地查看。` : '当前没有已有正文预览，可下载后在本地查看。' } }));
      }
      return;
    }
    if ((value.size_bytes ?? 0) > DOWNLOAD_LIMIT_BYTES) { setPreviewSides((current) => ({ ...current, [side]: { ...unavailableSide(value), mode: 'download', message: '文件超过 128 MB 的浏览器读取上限，可使用下载按钮保存到本地查看。' } })); return; }
    setPreviewSides((current) => ({ ...current, [side]: { filename: value.filename, size: value.size_bytes, status: 'loading', mode: null } }));
    try {
      const blob = await fetchIngestDuplicateComparisonBlob(token, data.item_id, { review_id: data.review_id, review_revision: data.review_revision, candidate_id: data.candidate_id, group_revision: data.group_revision, snapshot_id: data.snapshot_id, side });
      if (blob.size > DOWNLOAD_LIMIT_BYTES) throw new Error('文件超过 128 MB 的浏览器读取上限。');
      const preview = browserPreview(value, blob);
      if (preview.objectUrl) objectUrls.current.push(preview.objectUrl);
      if (preview.mode === 'text') { const text = await blob.text(); preview.text = text.slice(0, 100_000); preview.message = text.length > 100_000 ? '内容较长，当前只展示前 100,000 个字符。' : undefined; }
      setPreviewSides((current) => ({ ...current, [side]: preview }));
    } catch (value) {
      setPreviewSides((current) => ({ ...current, [side]: { filename: side === 'UPLOAD' ? data.upload.filename : data.candidate.filename, size: null, status: 'error', mode: null, message: value instanceof Error ? value.message : '预览读取失败。' } }));
    }
  }

  async function download(side: Side) {
    if (!data || downloading) return;
    const value = side === 'UPLOAD' ? data.upload : data.candidate;
    setDownloading(true);
    setActionError('');
    try {
      const blob = await fetchIngestDuplicateComparisonBlob(token, data.item_id, { review_id: data.review_id, review_revision: data.review_revision, candidate_id: data.candidate_id, group_revision: data.group_revision, snapshot_id: data.snapshot_id, side });
      const url = URL.createObjectURL(blob); objectUrls.current.push(url);
      const anchor = document.createElement('a'); anchor.href = url; anchor.download = `${side === 'UPLOAD' ? '本次上传_' : '候选_'}${value.filename}`; anchor.click();
    } catch (value) { setActionError(value instanceof Error ? value.message : '下载失败。'); }
    finally { setDownloading(false); }
  }

  function pane(side: Side, value: IngestDuplicateComparisonSide) {
    const preview = previewSides[side] ?? unavailableSide(value);
    const tooLargeToDownload = (value.size_bytes ?? 0) > DOWNLOAD_LIMIT_BYTES;
    return <div><PreviewPane title={side === 'UPLOAD' ? '本次上传' : '候选文件'} side={preview} />{value.download_available && !tooLargeToDownload ? <p className="duplicate-comparison-actions"><button onClick={() => void loadPreview(side)}>加载或刷新预览</button><button disabled={downloading} onClick={() => void download(side)}>{downloading ? '正在准备下载…' : `下载${side === 'UPLOAD' ? '本次上传文件' : '候选文件'}`}</button></p> : null}{value.download_available && tooLargeToDownload ? <p className="duplicate-comparison-note">该文件超过本页 128 MiB 下载上限，无法在此页面下载或预览。</p> : null}</div>;
  }

  if (error) return <main className="screen-center"><p>{error}</p><button onClick={onBack}>返回聊天</button></main>;
  if (!data) return <main className="screen-center">正在读取重复文件对比…</main>;
  return <main className="chat-page"><header className="chat-header"><button onClick={onBack}>返回聊天</button><div><h1>重复文件对比</h1><p>{data.verdict === 'EXACT_CONTENT' ? '两份文件的字节内容和大小均已核验一致。' : '请查看两侧文件后，回到 WorkBuddy 选择处理方式。'}</p></div></header>{actionError ? <p className="duplicate-comparison-note">{actionError}</p> : null}<div className="duplicate-comparison-grid">{pane('UPLOAD', data.upload)}{pane('CANDIDATE', data.candidate)}</div><p>本页面只能查看和下载固定快照，不会提交“使用已有文件”“继续上传”或取消导入等决定。</p></main>;
}
