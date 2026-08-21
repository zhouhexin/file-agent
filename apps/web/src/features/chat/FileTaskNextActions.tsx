// 文件任务下一步只提供受控 UI 动作；填入建议不会自动发送，高风险操作仍由 OperationPlan 确认。
import type { FileTaskNextAction } from '../../types';

type FileTaskNextActionsProps = {
  actions: FileTaskNextAction[];
  onUsePrompt?: (prompt: string) => void;
};

/** 展示与当前结果直接相关的安全后续动作。 */
export function FileTaskNextActions({ actions, onUsePrompt }: FileTaskNextActionsProps) {
  if (actions.length === 0) return null;
  return (
    <section className="file-task-next-actions" aria-label="下一步">
      <h4>下一步</h4>
      <div>
        {actions.map((action) => {
          const canFillPrompt = action.action_kind === 'FILL_PROMPT' && Boolean(action.prompt && onUsePrompt);
          return canFillPrompt ? (
            <button
              key={action.id}
              type="button"
              onClick={() => onUsePrompt?.(action.prompt || '')}
            >
              {action.label}
            </button>
          ) : (
            <span className="file-task-next-action-label" key={action.id}>{action.label}</span>
          );
        })}
      </div>
      <small>建议只会填入输入框，不会自动发送或修改文件。</small>
    </section>
  );
}
