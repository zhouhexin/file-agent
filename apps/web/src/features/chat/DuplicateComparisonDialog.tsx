// 重复文件对比弹窗复用受控文件流；DOCX 与 XLSX 仅在浏览器本地安全解析。
import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { AlertCircle, FileText, LoaderCircle, Maximize2, Minimize2, X } from 'lucide-react';

import {
  fetchManagedFileBlob,
  fetchUploadedFileBlob,
  getFilePreview,
} from '../../api/client';
import { formatError } from '../../api/errors';
import type { DuplicateCandidate, DuplicateReview, FilePreviewSection } from '../../types';
import { StructuredSpreadsheetPreview } from './StructuredSpreadsheetPreview';
import { XLSX_MAX_LOCAL_BYTES } from './xlsxPreview';

type PreviewSide = {
  filename: string;
  size: number | null;
  status: 'loading' | 'ready' | 'unavailable' | 'error';
  mode: 'image' | 'pdf' | 'text' | 'docx' | 'xlsx' | 'sections' | 'download' | null;
  objectUrl?: string;
  docxBlob?: Blob;
  xlsxBlob?: Blob;
  text?: string;
  sections?: FilePreviewSection[];
  message?: string;
};

type DuplicateComparisonDialogProps = {
  token: string;
  review: DuplicateReview;
  candidate: DuplicateCandidate;
  submitting: boolean;
  decisionError: string;
  onClose: () => void;
  onDecision: (
    decision: 'CONTINUE_UPLOAD' | 'USE_EXISTING_FILE' | 'CANCEL_UPLOAD',
    selectedExistingWorkingCopyId?: string,
  ) => void;
};

const TEXT_EXTENSIONS = new Set(['txt', 'md', 'csv', 'tsv', 'json', 'xml', 'log']);
const MAX_LOCAL_DOCX_BYTES = 20 * 1024 * 1024;
const DOCX_FRAME_HTML = `<!doctype html>
<html><head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data: blob:; font-src data: blob:; style-src 'unsafe-inline'; connect-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'">
<style>
html,body{margin:0;min-height:100%;background:#eef2f6}body{padding:12px;box-sizing:border-box}
.docx-wrapper{padding:0!important;background:transparent!important}
.docx-wrapper>section.docx{width:100%!important;min-height:0!important;margin:0 0 12px!important;padding:clamp(18px,4vw,48px)!important;box-sizing:border-box!important;box-shadow:none!important;overflow-wrap:anywhere}
.docx-wrapper img,.docx-wrapper svg,.docx-wrapper canvas{max-width:100%!important;height:auto!important}
.docx-wrapper table{max-width:100%!important}
</style>
</head><body></body></html>`;

function extension(filename: string) {
  return filename.split('.').pop()?.toLowerCase() ?? '';
}

function formatBytes(size: number | null) {
  if (size === null) return '大小未知';
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function SectionContent({ section }: { section: FilePreviewSection }) {
  return <pre>{section.text}</pre>;
}

function verdict(candidate: DuplicateCandidate) {
  if (candidate.match_type === 'EXACT_SHA256') {
    return {
      tone: 'exact',
      label: '内容完全一致',
      message: '系统已通过内容指纹确认两份文件的字节内容一致。',
    };
  }
  if (candidate.match_type === 'NEAR_DUPLICATE') {
    return {
      tone: 'near',
      label: '内容高度相似',
      message: '正文特征高度相似，但日期、金额、人员或局部段落仍可能不同。',
    };
  }
  return {
    tone: 'filename',
    label: '仅文件名相同',
    message: '系统只确认文件名相同，不能据此判断文件内容一致。',
  };
}

function sanitizeDocxPreview(container: HTMLElement) {
  // DOCX 中的超链接和外部关系只作为文字展示，禁止预览过程主动访问外部资源。
  container.querySelectorAll('script, iframe, object, embed, link').forEach((node) => node.remove());
  container.querySelectorAll('a').forEach((link) => {
    link.removeAttribute('href');
    link.removeAttribute('target');
    link.setAttribute('aria-disabled', 'true');
  });
  container.querySelectorAll('img').forEach((image) => {
    const source = image.getAttribute('src') ?? '';
    if (source && !source.startsWith('data:') && !source.startsWith('blob:')) {
      image.removeAttribute('src');
    }
  });
}

function LocalDocxPreview({ side }: { side: PreviewSide }) {
  const frameRef = useRef<HTMLIFrameElement>(null);
  const [error, setError] = useState('');
  const [rendering, setRendering] = useState(true);

  useEffect(() => {
    const frame = frameRef.current;
    if (!frame || !side.docxBlob) return undefined;
    let cancelled = false;
    setError('');
    setRendering(true);
    const render = () => {
      const document = frame.contentDocument;
      if (!document?.body || !document.head) return;
      void import('docx-preview')
        .then(({ renderAsync }) => renderAsync(side.docxBlob, document.body, document.head, {
          inWrapper: true,
          breakPages: true,
          ignoreWidth: true,
          ignoreHeight: true,
          useBase64URL: true,
          renderAltChunks: false,
          renderComments: false,
          renderChanges: false,
        }))
        .then(() => {
          if (cancelled) return;
          sanitizeDocxPreview(document.body);
          setRendering(false);
        })
        .catch(() => {
          if (cancelled) return;
          document.body.replaceChildren();
          setRendering(false);
          setError('DOCX 本地预览失败，可下载原文件后查看。');
        });
    };
    frame.addEventListener('load', render, { once: true });
    frame.srcdoc = DOCX_FRAME_HTML;
    return () => {
      cancelled = true;
      frame.removeEventListener('load', render);
      frame.srcdoc = '<!doctype html><html><body></body></html>';
    };
  }, [side.docxBlob]);

  return (
    <div className="duplicate-comparison-docx">
      {rendering ? (
        <p className="duplicate-comparison-state"><LoaderCircle className="spin" size={20} />正在本地解析 DOCX…</p>
      ) : null}
      {error ? (
        <p className="duplicate-comparison-state">
          <AlertCircle size={20} />
          <span>{error}</span>
          {side.objectUrl ? <a download={side.filename} href={side.objectUrl}>下载后查看</a> : null}
        </p>
      ) : null}
      <iframe
        className="duplicate-comparison-docx-content"
        hidden={Boolean(error)}
        ref={frameRef}
        sandbox="allow-same-origin"
        title={`${side.filename} DOCX 本地预览`}
      />
    </div>
  );
}

async function loadSide(
  token: string,
  filename: string,
  documentId?: string | null,
  managedRootKey?: string,
  managedRelativePath?: string,
): Promise<PreviewSide> {
  try {
    const blob = documentId
      ? await fetchUploadedFileBlob(token, documentId)
      : managedRootKey && managedRelativePath
        ? await fetchManagedFileBlob(token, managedRootKey, managedRelativePath)
        : null;
    if (!blob) {
      return {
        filename,
        size: null,
        status: 'unavailable',
        mode: null,
        message: '该候选暂时没有可用的受控预览入口。',
      };
    }

    const suffix = extension(filename);
    if (blob.type.startsWith('image/')) {
      return {
        filename,
        size: blob.size,
        status: 'ready',
        mode: 'image',
        objectUrl: URL.createObjectURL(blob),
      };
    }
    if (blob.type === 'application/pdf' || suffix === 'pdf') {
      return {
        filename,
        size: blob.size,
        status: 'ready',
        mode: 'pdf',
        objectUrl: URL.createObjectURL(blob),
      };
    }
    const docxTooLarge = suffix === 'docx' && blob.size > MAX_LOCAL_DOCX_BYTES;
    const xlsxTooLarge = suffix === 'xlsx' && blob.size > XLSX_MAX_LOCAL_BYTES;
    if (suffix === 'xlsx' && !xlsxTooLarge) {
      return {
        filename,
        size: blob.size,
        status: 'ready',
        mode: 'xlsx',
        xlsxBlob: blob,
        objectUrl: URL.createObjectURL(blob),
        message: 'XLSX 仅在当前浏览器本地结构化解析，不执行公式、宏或外部链接。',
      };
    }
    if (suffix === 'docx' && !docxTooLarge) {
      return {
        filename,
        size: blob.size,
        status: 'ready',
        mode: 'docx',
        docxBlob: blob,
        objectUrl: URL.createObjectURL(blob),
        message: 'DOCX 仅在当前浏览器本地解析，文件不会发送给第三方。',
      };
    }
    if (blob.type.startsWith('text/') || TEXT_EXTENSIONS.has(suffix)) {
      const text = await blob.text();
      return {
        filename,
        size: blob.size,
        status: 'ready',
        mode: 'text',
        text: text.slice(0, 100_000),
        message: text.length > 100_000 ? '内容较长，当前只展示前 100,000 个字符。' : undefined,
      };
    }

    if (documentId) {
      try {
        const preview = await getFilePreview(token, documentId);
        return {
          filename: preview.filename || filename,
          size: blob.size,
          status: 'ready',
          mode: 'sections',
          sections: preview.sections,
          message: preview.truncated ? '内容较长，当前只展示前 100,000 个字符。' : undefined,
        };
      } catch {
        // 上传侧尚未生成正文页时保留原始文件下载，不把预览缺失误判为内容不同。
      }
    }

    return {
      filename,
      size: blob.size,
      status: 'unavailable',
      mode: 'download',
      objectUrl: URL.createObjectURL(blob),
      message: docxTooLarge
        ? 'DOCX 超过 20 MB 本地预览上限，可下载原文件后查看。'
        : xlsxTooLarge
          ? 'XLSX 超过 25 MB 本地预览上限，可下载原文件后查看。'
        : '暂未生成安全正文预览，可下载原文件后查看。',
    };
  } catch (error) {
    return {
      filename,
      size: null,
      status: 'error',
      mode: null,
      message: formatError(error),
    };
  }
}

function PreviewPane({ title, side }: { title: string; side: PreviewSide }) {
  return (
    <article className="duplicate-comparison-pane">
      <header>
        <div>
          <span>{title}</span>
          <strong title={side.filename}>{side.filename}</strong>
        </div>
        <small>{formatBytes(side.size)}</small>
      </header>
      <div className="duplicate-comparison-preview">
        {side.status === 'loading' ? (
          <p className="duplicate-comparison-state"><LoaderCircle className="spin" size={20} />正在准备预览…</p>
        ) : null}
        {side.mode === 'image' && side.objectUrl ? <img alt={side.filename} src={side.objectUrl} /> : null}
        {side.mode === 'pdf' && side.objectUrl ? <iframe src={side.objectUrl} title={`${side.filename} PDF 预览`} /> : null}
        {side.mode === 'text' ? <pre>{side.text}</pre> : null}
        {side.mode === 'docx' ? <LocalDocxPreview side={side} /> : null}
        {side.mode === 'xlsx' && side.xlsxBlob ? (
          <StructuredSpreadsheetPreview blob={side.xlsxBlob} filename={side.filename} />
        ) : null}
        {side.mode === 'sections' ? (
          <div className="duplicate-comparison-sections">
            {side.sections?.length ? side.sections.map((section, index) => (
              <section key={`${section.sheet_name || section.page_number || 'section'}-${index}`}>
                {section.sheet_name || section.page_number ? (
                  <h3>{section.sheet_name ? `工作表：${section.sheet_name}` : `第 ${section.page_number} 页`}</h3>
                ) : null}
                <SectionContent section={section} />
              </section>
            )) : <p>该文件暂时没有可展示的正文。</p>}
          </div>
        ) : null}
        {side.mode === 'download' && side.objectUrl ? (
          <p className="duplicate-comparison-state">
            <FileText size={22} />
            <span>{side.message}</span>
            <a download={side.filename} href={side.objectUrl}>下载后查看</a>
          </p>
        ) : null}
        {(side.status === 'error' || (side.status === 'unavailable' && side.mode === null)) ? (
          <p className="duplicate-comparison-state"><AlertCircle size={20} />{side.message}</p>
        ) : null}
      </div>
      {side.message && side.mode !== 'download' ? <p className="duplicate-comparison-note">{side.message}</p> : null}
    </article>
  );
}

export function DuplicateComparisonDialog({
  token,
  review,
  candidate,
  submitting,
  decisionError,
  onClose,
  onDecision,
}: DuplicateComparisonDialogProps) {
  const existingFilename = String(candidate.summary.filename ?? '现有文件');
  const [uploadSide, setUploadSide] = useState<PreviewSide>({
    filename: review.filename,
    size: null,
    status: 'loading',
    mode: null,
  });
  const [existingSide, setExistingSide] = useState<PreviewSide>({
    filename: existingFilename,
    size: null,
    status: 'loading',
    mode: null,
  });
  const [maximized, setMaximized] = useState(false);
  const result = verdict(candidate);

  useEffect(() => {
    let cancelled = false;
    const objectUrls: string[] = [];
    const retain = (side: PreviewSide) => {
      if (side.objectUrl) objectUrls.push(side.objectUrl);
      return side;
    };
    Promise.all([
      loadSide(token, review.filename, review.document_id),
      loadSide(
        token,
        existingFilename,
        candidate.existing_document_id,
        String(candidate.summary.managed_root_key ?? '') || undefined,
        String(candidate.summary.managed_relative_path ?? '') || undefined,
      ),
    ]).then(([upload, existing]) => {
      if (!cancelled) {
        setUploadSide(retain(upload));
        setExistingSide(retain(existing));
      } else {
        [upload.objectUrl, existing.objectUrl].forEach((url) => url && URL.revokeObjectURL(url));
      }
    });
    return () => {
      cancelled = true;
      objectUrls.forEach((url) => URL.revokeObjectURL(url));
    };
  }, [candidate, existingFilename, review.document_id, review.filename, token]);

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !submitting) onClose();
    };
    window.addEventListener('keydown', closeOnEscape);
    return () => window.removeEventListener('keydown', closeOnEscape);
  }, [onClose, submitting]);

  const canUseExisting = Boolean(
    candidate.existing_working_copy_id
    && review.allowed_decisions.includes('USE_EXISTING_FILE'),
  );

  return createPortal(
    <div className="duplicate-comparison-backdrop" role="presentation">
      <section
        aria-modal="true"
        className={`duplicate-comparison-dialog${maximized ? ' duplicate-comparison-dialog--maximized' : ''}`}
        role="dialog"
      >
        <header className="duplicate-comparison-header">
          <div className="duplicate-comparison-header-copy">
            <span className={`duplicate-verdict duplicate-verdict--${result.tone}`}>{result.label}</span>
            <strong>发现相同或相似文件</strong>
            <p>{result.message}</p>
          </div>
          <div className="duplicate-comparison-header-actions">
            <button
              aria-label={maximized ? '还原对比窗口' : '最大化对比窗口'}
              disabled={submitting}
              onClick={() => setMaximized((current) => !current)}
              title={maximized ? '还原窗口' : '最大化窗口'}
              type="button"
            >
              {maximized ? <Minimize2 size={19} /> : <Maximize2 size={19} />}
            </button>
            <button aria-label="关闭对比预览" disabled={submitting} onClick={onClose} title="关闭" type="button">
              <X size={20} />
            </button>
          </div>
        </header>

        <p className="duplicate-comparison-trash-note">
          <span>回收站文件不会参与本次查重。</span>
          <span className="duplicate-comparison-resize-hint">可拖动窗口右下角调整大小</span>
        </p>

        <div className="duplicate-comparison-grid">
          <PreviewPane side={uploadSide} title="本次上传" />
          <PreviewPane side={existingSide} title="现有文件" />
        </div>

        <footer className="duplicate-comparison-actions">
          <button className="secondary" disabled={submitting} onClick={() => onDecision('CANCEL_UPLOAD')} type="button">
            取消本次上传
          </button>
          <button className="secondary" disabled={submitting} onClick={() => onDecision('CONTINUE_UPLOAD')} type="button">
            继续上传并独立保留
          </button>
          {canUseExisting ? (
            <button
              disabled={submitting}
              onClick={() => onDecision('USE_EXISTING_FILE', candidate.existing_working_copy_id ?? undefined)}
              type="button"
            >
              使用现有文件
            </button>
          ) : null}
        </footer>
        {decisionError ? <p className="duplicate-review-error">{decisionError}</p> : null}
      </section>
    </div>,
    document.body,
  );
}
