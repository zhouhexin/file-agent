// 统一文件任务回执外壳固定六段式阅读顺序，专用卡片作为中间明细插槽继续复用。
import type { ReactNode } from 'react';
import { CheckCircle2, Clock3 } from 'lucide-react';

import type { FileTaskPresentation } from '../../types';
import { FileTaskChangeImpact } from './FileTaskChangeImpact';
import { FileTaskNextActions } from './FileTaskNextActions';
import { FileTaskOutcomeSummary } from './FileTaskOutcomeSummary';
import { FileTaskRequestSummary } from './FileTaskRequestSummary';
import { canShowFileTaskNextActions } from './fileTaskPresentation';

type FileTaskReceiptShellProps = {
  presentation: FileTaskPresentation;
  children?: ReactNode;
  onUsePrompt?: (prompt: string) => void;
};

/** 统一组织任务理解、业务进度、摘要、专用明细、文件变化和下一步。 */
export function FileTaskReceiptShell({
  presentation,
  children,
  onUsePrompt,
}: FileTaskReceiptShellProps) {
  const completed = presentation.phase.code === 'COMPLETED';
  return (
    <section className="file-task-receipt-shell" aria-live="polite">
      <header className="file-task-receipt-header">
        <div>
          {completed ? <CheckCircle2 size={19} aria-hidden /> : <Clock3 size={19} aria-hidden />}
          <strong>{presentation.title}</strong>
        </div>
        <span>{presentation.phase.label}</span>
      </header>

      <FileTaskRequestSummary request={presentation.request} />
      <FileTaskOutcomeSummary outcome={presentation.outcome} />

      {presentation.notices.length > 0 ? (
        <div className="file-task-notices" aria-label="任务提示">
          {presentation.notices.map((notice, index) => (
            <p className={`file-task-notice file-task-notice--${notice.level.toLowerCase()}`} key={`${notice.message}-${index}`}>
              {notice.message}
            </p>
          ))}
        </div>
      ) : null}

      {children ? (
        <section className="file-task-details" aria-label="文件明细">
          <h4>文件明细</h4>
          {children}
        </section>
      ) : null}

      <FileTaskChangeImpact impact={presentation.change_impact} />
      {canShowFileTaskNextActions(presentation.phase.code) ? (
        <FileTaskNextActions actions={presentation.next_actions} onUsePrompt={onUsePrompt} />
      ) : null}
    </section>
  );
}
