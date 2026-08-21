// 文件任务组件共享的纯展示规则；单独抽离后可用 Node 原生测试保护唯一键和阶段边界。
import type { FileSearchResultFile, FileTaskPresentation } from '../../types';

/** 为搜索卡片生成逻辑文件唯一键，同名不同路径必须保留为不同条目。 */
export function fileSearchResultKey(file: FileSearchResultFile): string {
  if (file.managed_file_id) return `managed:${file.managed_file_id}`;
  if (file.root_key && file.relative_path) {
    return `path:${file.root_key}:${file.relative_path}`;
  }
  if (file.working_copy_id) return `working-copy:${file.working_copy_id}`;
  return `document-version:${file.document_id}:${file.document_version_id}`;
}

/** 只有已有可核对结果的阶段才展示后续建议，处理中禁止诱导用户继续操作。 */
export function canShowFileTaskNextActions(
  phaseCode: FileTaskPresentation['phase']['code'],
): boolean {
  return phaseCode === 'COMPLETED' || phaseCode === 'NEEDS_ATTENTION';
}
