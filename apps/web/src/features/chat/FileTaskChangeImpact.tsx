// 文件变化区明确区分只读分析、工作副本变更和受管原件保护，不能根据任务名称自行猜测。
import { ShieldCheck } from 'lucide-react';

import type { FileTaskPresentation } from '../../types';

type FileTaskChangeImpactProps = {
  impact: FileTaskPresentation['change_impact'];
};

/** 展示后端确认的文件变化状态。 */
export function FileTaskChangeImpact({ impact }: FileTaskChangeImpactProps) {
  return (
    <section className="file-task-change-impact" aria-label="文件变化">
      <ShieldCheck size={17} aria-hidden />
      <div>
        <h4>文件变化</h4>
        <p>{impact.message}</p>
        {impact.derivatives_created > 0 ? (
          <span>生成了 {impact.derivatives_created} 个派生结果。</span>
        ) : null}
      </div>
    </section>
  );
}
