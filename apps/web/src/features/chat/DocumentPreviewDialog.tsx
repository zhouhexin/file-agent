// 文件预览弹窗只展示后端已授权、已截断的正文区段，不在浏览器解析不可信 Office 二进制。
import { useEffect } from 'react';
import { X } from 'lucide-react';

import type { FilePreviewResponse } from '../../types';

type DocumentPreviewDialogProps = {
  preview: FilePreviewResponse;
  onClose: () => void;
};

/** 展示 DOC/DOCX/XLS/XLSX 等文件的安全文本预览。 */
export function DocumentPreviewDialog({
  preview,
  onClose,
}: DocumentPreviewDialogProps) {
  useEffect(() => {
    // Escape 是只读预览的快捷退出方式，不触发任何文件变更。
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', closeOnEscape);
    return () => window.removeEventListener('keydown', closeOnEscape);
  }, [onClose]);

  return (
    <div
      className="document-preview-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section
        aria-label={`${preview.filename} 文件预览`}
        aria-modal="true"
        className="document-preview-dialog"
        role="dialog"
      >
        <header className="document-preview-header">
          <div>
            <strong>{preview.filename}</strong>
            <span>正文预览</span>
          </div>
          <button aria-label="关闭文件预览" onClick={onClose} type="button">
            <X size={18} />
          </button>
        </header>
        <div className="document-preview-content">
          {preview.sections.length > 0 ? preview.sections.map((section, index) => (
            <article
              className="document-preview-section"
              key={`${section.sheet_name || section.page_number || 'section'}-${index}`}
            >
              {section.sheet_name || section.page_number ? (
                <h3>
                  {section.sheet_name
                    ? `工作表：${section.sheet_name}`
                    : `第 ${section.page_number} 页`}
                </h3>
              ) : null}
              <pre>{section.text}</pre>
            </article>
          )) : (
            <p className="document-preview-empty">该文件暂时没有可展示的正文。</p>
          )}
          {preview.truncated ? (
            <p className="document-preview-truncated">文件内容较长，当前只展示前 100,000 个字符。</p>
          ) : null}
        </div>
      </section>
    </div>
  );
}
