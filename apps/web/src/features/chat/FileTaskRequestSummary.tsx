// 文件任务理解区展示后端已确认的对象、业务范围和条件，不解释内部 Planner 或 Tool 数据。
import type { FileTaskPresentation } from '../../types';

type FileTaskRequestSummaryProps = {
  request: FileTaskPresentation['request'];
};

/** 展示用户可以核对的文件任务理解。 */
export function FileTaskRequestSummary({ request }: FileTaskRequestSummaryProps) {
  const statusLabels: Record<string, string> = {
    APPLIED: '已应用',
    SEMANTIC_ONLY: '用于语义匹配',
    RELAXED: '已放宽',
    UNSUPPORTED: '当前无法硬过滤',
    REJECTED: '未采用',
  };
  return (
    <section className="file-task-request" aria-label="任务理解">
      <h4>任务理解</h4>
      <dl className="file-task-request-grid">
        <div><dt>对象</dt><dd>{request.target_label}</dd></div>
        <div><dt>范围</dt><dd>{request.scope_label}</dd></div>
        <div><dt>动作</dt><dd>{request.action_label}</dd></div>
      </dl>
      {request.conditions.length > 0 ? (
        <ul className="file-task-condition-list" aria-label="已采用条件">
          {request.conditions.map((condition, index) => (
            <li key={`${condition.label}-${condition.value}-${index}`}>
              <span>{condition.label}：{condition.value}</span>
              <em>{statusLabels[condition.status] || condition.status}</em>
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}
