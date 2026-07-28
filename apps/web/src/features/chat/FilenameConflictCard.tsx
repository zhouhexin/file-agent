import { AlertCircle } from 'lucide-react';

import type { FilenameConflictResult } from '../../types';

/** 展示共享目录同名冲突；最终决定仍由用户在聊天框中明确回复。 */
export function FilenameConflictCard({ result }: { result: FilenameConflictResult }) {
  return (
    <section className="task-pending-decision" aria-label="文件名冲突">
      <strong><AlertCircle size={17} /> 文件名冲突</strong>
      <p>{result.message}</p>
      <div className="category-feedback__actions">
        <span>覆盖已有文件</span>
        <span>同时保留</span>
        <span>取消</span>
      </div>
      <small>
        请直接回复“覆盖已有文件”“同时保留”或“取消”。覆盖时旧文件会进入可恢复回收站。
      </small>
    </section>
  );
}
