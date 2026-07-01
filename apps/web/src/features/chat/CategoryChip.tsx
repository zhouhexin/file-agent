import { Tag } from 'lucide-react';
import { useState } from 'react';

import type { DocumentCategory } from '../../types';
import { formatConfidence } from './presentation';

type CategoryChipProps = {
  category: DocumentCategory;
  compact?: boolean;
};

export function CategoryChip({ category, compact = false }: CategoryChipProps) {
  // 点击分类标签时展开证据，不把证据和来源塞进结果主行。
  const [expanded, setExpanded] = useState(false);
  const evidenceText = category.evidence.length > 0 ? category.evidence.join('、') : '暂无明确关键词依据';

  return (
    <div className="category-chip-wrap">
      <button
        className={compact ? 'category-chip category-chip--compact' : 'category-chip'}
        type="button"
        aria-expanded={expanded}
        onClick={() => setExpanded((current) => !current)}
      >
        <Tag size={14} />
        <span>{category.name}</span>
        {!compact ? <em className="category-chip__confidence">{formatConfidence(category.confidence)}</em> : null}
      </button>
      {expanded ? (
        <div className="result-evidence">
          <p>证据关键词：{evidenceText}</p>
          <p>分类状态：{category.status || 'SUGGESTED'}</p>
          <p>分类来源：{category.source || 'rule'}</p>
        </div>
      ) : null}
    </div>
  );
}
