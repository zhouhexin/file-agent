// 文件任务摘要区只展示后端确定性统计，不根据前端当前渲染条目重新推断完成度。
import type { FileTaskPresentation } from '../../types';

type FileTaskOutcomeSummaryProps = {
  outcome: FileTaskPresentation['outcome'];
};

/** 展示文件任务总体结论和必要的状态数量。 */
export function FileTaskOutcomeSummary({ outcome }: FileTaskOutcomeSummaryProps) {
  const completenessLabels: Record<FileTaskPresentation['outcome']['completeness'], string> = {
    COMPLETE: '结果完整',
    PROCESSING: '仍在处理',
    PARTIAL: '部分完成',
    UNVERIFIABLE: '完整性待确认',
  };
  const counts = [
    outcome.completed_count > 0 ? `完成 ${outcome.completed_count}` : null,
    outcome.needs_review_count > 0 ? `待留意 ${outcome.needs_review_count}` : null,
    outcome.failed_count > 0 ? `未完成 ${outcome.failed_count}` : null,
    outcome.skipped_count > 0 ? `跳过 ${outcome.skipped_count}` : null,
  ].filter(Boolean);
  return (
    <section className="file-task-outcome" aria-label="结果摘要">
      <div>
        <h4>结果摘要</h4>
        <strong>{outcome.headline}</strong>
        {counts.length > 0 ? <span>{counts.join(' · ')}</span> : null}
      </div>
      <em className={`file-task-completeness file-task-completeness--${outcome.completeness.toLowerCase()}`}>
        {completenessLabels[outcome.completeness]}
      </em>
    </section>
  );
}
