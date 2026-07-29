// 文件检索范围选择卡只展示业务选项；分词、Tool、内部模式和执行短语保留在后端。
import { useEffect, useState } from 'react';
import { Search } from 'lucide-react';

import {
  getFileSearchClarification,
  resolveFileSearchClarification,
} from '../../api/client';
import { formatError } from '../../api/errors';
import type {
  FileSearchClarificationResult,
  SendMessageResponse,
} from '../../types';

type FileSearchClarificationCardProps = {
  token: string;
  result: FileSearchClarificationResult;
  onResolved?: (response: SendMessageResponse) => void;
};

export function FileSearchClarificationCard({
  token,
  result,
  onResolved,
}: FileSearchClarificationCardProps) {
  const [selectedOptionIds, setSelectedOptionIds] = useState<string[]>([]);
  const [customPhrase, setCustomPhrase] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [resolvedLabel, setResolvedLabel] = useState('');
  const [currentStatus, setCurrentStatus] = useState(result.status);
  const [error, setError] = useState('');

  useEffect(() => {
    let active = true;
    void getFileSearchClarification(token, result.id)
      .then((latest) => {
        if (active) {
          setCurrentStatus(latest.status);
        }
      })
      .catch(() => {
        // 会话历史本身仍可展示；状态刷新失败不应阻断整个聊天页面。
      });
    return () => {
      active = false;
    };
  }, [result.id, token]);

  async function submitSelection() {
    const options = result.options.filter((item) => selectedOptionIds.includes(item.id));
    if (options.length === 0) {
      setError(
        result.selection_type === 'DOCUMENT_SELECTION'
          ? '请至少选择一份文件。'
          : '请先选择本次查找范围。',
      );
      return;
    }
    const option = options[0];
    if (option.id === 'custom' && customPhrase.trim().length < 2) {
      setError('请至少输入 2 个字符的查找短语。');
      return;
    }
    setSubmitting(true);
    setError('');
    try {
      const response = await resolveFileSearchClarification(
        token,
        result.id,
        {
          option_id: result.allow_multiple ? null : option.id,
          option_ids: result.allow_multiple ? options.map((item) => item.id) : [],
          custom_phrase: option.id === 'custom' ? customPhrase.trim() : null,
        },
      );
      setResolvedLabel(
        option.id === 'custom'
          ? customPhrase.trim()
          : options.map((item) => item.label).join('、'),
      );
      setCurrentStatus('RESOLVED');
      onResolved?.(response);
    } catch (err) {
      setError(formatError(err));
    } finally {
      setSubmitting(false);
    }
  }

  if (resolvedLabel || currentStatus === 'RESOLVED') {
    return (
      <section className="file-search-clarification-card is-resolved">
        <Search size={18} aria-hidden />
        <span>
          {resolvedLabel ? `已按“${resolvedLabel}”继续查找。` : '该查找范围已选择。'}
        </span>
      </section>
    );
  }

  if (currentStatus === 'SUPERSEDED' || currentStatus === 'EXPIRED') {
    return (
      <section className="file-search-clarification-card is-resolved">
        <Search size={18} aria-hidden />
        <span>
          {currentStatus === 'EXPIRED'
            ? '该查找选择已过期，请重新描述需要查找的文件。'
            : '该查找选择已被后续请求替代。'}
        </span>
      </section>
    );
  }

  return (
    <section className="file-search-clarification-card" aria-label="选择文件查找范围">
      <header>
        <Search size={18} aria-hidden />
        <div>
          <strong>
            {result.selection_type === 'DOCUMENT_SELECTION' ? '请选择文件' : '请选择查找范围'}
          </strong>
          <span>{result.prompt}</span>
        </div>
      </header>
      <div className="file-search-clarification-options">
        {result.options.map((option) => {
          const checked = selectedOptionIds.includes(option.id);
          return (
            <label
              className={checked ? 'file-search-clarification-option is-selected' : 'file-search-clarification-option'}
              key={option.id}
            >
              <input
                checked={checked}
                disabled={submitting}
                name={`file-search-clarification-${result.id}`}
                onChange={() => {
                  if (result.allow_multiple) {
                    setSelectedOptionIds((current) => (
                      current.includes(option.id)
                        ? current.filter((item) => item !== option.id)
                        : [...current, option.id]
                    ));
                  } else {
                    setSelectedOptionIds([option.id]);
                  }
                }}
                type={result.allow_multiple ? 'checkbox' : 'radio'}
              />
              <div>
                <strong>{option.label}</strong>
                {option.description ? <span>{option.description}</span> : null}
                {option.examples.length > 0 ? (
                  <small>示例：{option.examples.join('、')}</small>
                ) : null}
                {typeof option.estimated_count === 'number' ? (
                  <small>当前预计 {option.estimated_count} 个文件</small>
                ) : null}
                {option.id === 'custom' && checked ? (
                  <input
                    aria-label="自定义查找短语"
                    className="file-search-custom-input"
                    disabled={submitting}
                    maxLength={30}
                    onChange={(event) => setCustomPhrase(event.target.value)}
                    placeholder="输入需要连续匹配的短语"
                    type="text"
                    value={customPhrase}
                  />
                ) : null}
              </div>
            </label>
          );
        })}
      </div>
      <footer>
        <button
          disabled={submitting || selectedOptionIds.length === 0}
          onClick={() => void submitSelection()}
          type="button"
        >
          {submitting
            ? '正在继续…'
            : result.selection_type === 'DOCUMENT_SELECTION'
            ? '使用所选文件继续'
            : '继续查找'}
        </button>
      </footer>
      {error ? <p className="duplicate-review-error">{error}</p> : null}
    </section>
  );
}
