// 重命名建议回执展示 Tool 的结构化结果，不从最终回复字符串反向解析文件名。
import { FilePenLine } from 'lucide-react';
import { useState } from 'react';

import { getRenameBatchItems } from '../../api/client';
import type { ManagedFileResult, RenameBatchItem } from '../../types';

type RenameSuggestion = {
  document_id?: string;
  source_kind?: string;
  managed_file_id?: string;
  root_key?: string;
  relative_path?: string;
  filename?: string;
  extension?: string;
  size_bytes?: number;
  managed_status?: string;
  proposed_filename?: string | null;
  status?: string;
  warnings?: string[];
  errors?: Array<{ code?: string; message?: string }>;
  year?: { status?: string };
  title?: { status?: string };
};

export type RenamePlanResult = {
  ok?: boolean;
  source_kind?: string;
  ready_count?: number;
  needs_review_count?: number;
  rename_batch_id?: string;
  suggestions?: RenameSuggestion[];
};

export function RenameSuggestionReceipt({
  result,
  token,
  onOpenManagedFile,
}: {
  result: RenamePlanResult;
  token?: string;
  onOpenManagedFile?: (file: ManagedFileResult) => void;
}) {
  const initialSuggestions = (result.suggestions ?? []).filter(
    (item) => !item.proposed_filename || item.status !== 'READY',
  );
  const [loadedSuggestions, setLoadedSuggestions] = useState<RenameSuggestion[] | null>(null);
  const [nextCursor, setNextCursor] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const suggestions = loadedSuggestions ?? initialSuggestions;
  const uploadedScope = result.source_kind === 'uploaded_document';
  if (suggestions.length === 0) {
    return null;
  }
  return (
    <section className="rename-suggestion-card">
      <header className="rename-suggestion-header">
        <strong><FilePenLine size={18} />待确认文件</strong>
        <span>{result.needs_review_count ?? suggestions.length} 个暂未处理</span>
      </header>
      <p>以下文件暂不满足自动重命名条件，请通过对话确认名称。</p>
      <div className="rename-suggestion-list">
        {suggestions.map((suggestion, index) => (
          <button
            className="rename-suggestion-item rename-suggestion-item--openable"
            key={suggestion.managed_file_id ?? suggestion.document_id ?? `${suggestion.filename}-${index}`}
            type="button"
            disabled={!onOpenManagedFile || !suggestion.root_key || !suggestion.relative_path}
            onClick={() => openSuggestion(suggestion, onOpenManagedFile)}
          >
            <span>{index + 1}</span>
            <div>
              <strong>{suggestion.filename ?? '未知文件'}</strong>
              <small>{suggestion.relative_path ?? ''}</small>
              {renameReviewMessage(suggestion) ? <em>{renameReviewMessage(suggestion)}</em> : null}
            </div>
          </button>
        ))}
      </div>
      {canLoadMore(result, suggestions, nextCursor) && token && result.rename_batch_id ? (
        <button
          className="rename-suggestion-more"
          disabled={loading}
          onClick={async () => {
            setLoading(true);
            try {
              const page = await getRenameBatchItems(
                token,
                result.rename_batch_id!,
                'NEEDS_REVIEW',
                loadedSuggestions === null ? 0 : (nextCursor ?? 0),
              );
              const mapped = page.items.map(batchItemToSuggestion);
              setLoadedSuggestions((current) => current === null ? mapped : [...current, ...mapped]);
              setNextCursor(page.next_cursor);
            } finally {
              setLoading(false);
            }
          }}
          type="button"
        >
          {loading ? '加载中...' : '查看其余待复核文件'}
        </button>
      ) : null}
      {uploadedScope ? (
        <p>请补充可识别的年份和正文标题后重新发起附件重命名。</p>
      ) : (
        <>
          <p>如需改名，请回复：文件原文件名更正为新文件名</p>
          <p>不需要改名请回复“不需要”。</p>
        </>
      )}
    </section>
  );
}

const RENAME_EVIDENCE_REVIEW_CODES = new Set([
  'RENAME_DIFFERENCE_UNVERIFIED',
  'TITLE_NOT_IN_EVIDENCE',
  'TITLE_FROM_LATER_PAGE',
  'PARSER_TITLE_CONFLICT',
  'OCR_QUALITY_LOW',
  'DOCUMENT_NUMBER_CONFLICT',
  'DOCUMENT_DATE_CONFLICT',
  'LLM_VALIDATION_UNAVAILABLE',
  'LLM_VALIDATION_LIMIT_REACHED',
]);

function renameReviewMessage(suggestion: RenameSuggestion): string | null {
  if (suggestion.status !== 'NEEDS_REVIEW') return null;
  if ((suggestion.warnings ?? []).some((item) => RENAME_EVIDENCE_REVIEW_CODES.has(item))) {
    return '名称差异较大，系统未能确认标题依据。';
  }
  return null;
}

function canLoadMore(
  result: RenamePlanResult,
  suggestions: RenameSuggestion[],
  nextCursor: number | null,
): boolean {
  if (nextCursor !== null) return true;
  return (result.needs_review_count ?? 0) > suggestions.length;
}

function batchItemToSuggestion(item: RenameBatchItem): RenameSuggestion {
  return {
    managed_file_id: item.managed_file_id,
    root_key: item.root_key,
    relative_path: item.original_relative_path,
    filename: item.original_filename,
    extension: item.original_filename.includes('.') ? `.${item.original_filename.split('.').pop()}` : '',
    status: item.status,
    warnings: item.warnings,
  };
}

function openSuggestion(
  suggestion: RenameSuggestion,
  onOpenManagedFile?: (file: ManagedFileResult) => void,
) {
  if (!onOpenManagedFile || !suggestion.root_key || !suggestion.relative_path) return;
  onOpenManagedFile({
    root_key: suggestion.root_key,
    display_name: suggestion.root_key,
    relative_path: suggestion.relative_path,
    category_path: null,
    filename: suggestion.filename ?? '未知文件',
    extension: suggestion.extension ?? '',
    size_bytes: suggestion.size_bytes ?? 0,
    modified_at: null,
    status: suggestion.managed_status ?? 'ACTIVE',
  });
}
