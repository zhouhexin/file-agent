// 重命名建议回执展示 Tool 的结构化结果，不从最终回复字符串反向解析文件名。
import { FilePenLine } from 'lucide-react';

import type { ManagedFileResult } from '../../types';

type RenameSuggestion = {
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
  ready_count?: number;
  needs_review_count?: number;
  suggestions?: RenameSuggestion[];
};

export function RenameSuggestionReceipt({
  result,
  onOpenManagedFile,
}: {
  result: RenamePlanResult;
  onOpenManagedFile?: (file: ManagedFileResult) => void;
}) {
  const suggestions = (result.suggestions ?? []).filter((item) => !item.proposed_filename || item.status !== 'READY');
  if (suggestions.length === 0) {
    return null;
  }
  return (
    <section className="rename-suggestion-card">
      <header className="rename-suggestion-header">
        <strong><FilePenLine size={18} />待复核文件</strong>
        <span>{result.needs_review_count ?? suggestions.length} 个暂未处理</span>
      </header>
      <p>以下文件缺少重命名所需信息（年份或正文标题），暂未处理。</p>
      <div className="rename-suggestion-list">
        {suggestions.map((suggestion, index) => (
          <button
            className="rename-suggestion-item rename-suggestion-item--openable"
            key={suggestion.managed_file_id ?? `${suggestion.filename}-${index}`}
            type="button"
            disabled={!onOpenManagedFile || !suggestion.root_key || !suggestion.relative_path}
            onClick={() => openSuggestion(suggestion, onOpenManagedFile)}
          >
            <span>{index + 1}</span>
            <div>
              <strong>{suggestion.filename ?? '未知文件'}</strong>
              <small>{suggestion.relative_path ?? ''}</small>
            </div>
          </button>
        ))}
      </div>
      <p>如需改名，请回复：文件原文件名更正为新文件名</p>
      <p>不需要改名请回复“不需要”。</p>
    </section>
  );
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
