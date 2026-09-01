// 批量上传辅助函数只整理浏览器文件元数据，不访问文件系统或替代后端路径校验。

export type FolderSelectedFile = {
  name: string;
  webkitRelativePath?: string;
};

export type UploadBatchFailure = {
  relativePath: string;
  message: string;
};

export type UploadBatchProgressState = {
  id: string;
  mode: 'files' | 'folder';
  folderName?: string;
  total: number;
  completed: number;
  processed: number;
  succeeded: number;
  failed: number;
  failures: UploadBatchFailure[];
  agentRunId?: string;
  status: 'uploading' | 'waiting_review' | 'submitting' | 'submitted' | 'completed' | 'failed';
};

export function getSelectedFileRelativePath(file: FolderSelectedFile): string {
  // webkitRelativePath 是浏览器目录选择提供的展示路径；真正的安全校验仍由后端执行。
  return file.webkitRelativePath?.replace(/\\/g, '/') || file.name;
}

export function inferSelectedFolderName(files: FolderSelectedFile[]): string {
  // 同一目录选择批次共享第一级目录；浏览器未提供时使用中性名称，不猜测本地绝对路径。
  const relativePath = files[0] ? getSelectedFileRelativePath(files[0]) : '';
  const firstSegment = relativePath.split('/')[0];
  return relativePath.includes('/') && firstSegment ? firstSegment : '所选文件夹';
}

export function buildFolderClassificationInstruction(folderName: string, fileCount: number): string {
  // 文件夹上传是用户已明确授权的默认分类入口，仍生成一条可审计的显式任务消息。
  return `请读取并分类文件夹“${folderName}”中的 ${fileCount} 个文件，逐文件展示分类、置信度、证据和处理状态。`;
}

export function buildUploadOrganizationInstruction(batchName: string, fileCount: number): string {
  // 上传即代表用户授权执行默认整理链路；仍生成显式消息，以保留 AgentRun 和逐文件回执。
  return `请读取并分类“${batchName}”中的 ${fileCount} 个文件。系统已完成工作副本标准名称整理，请逐文件展示上传时名称、整理后名称、分类结果、处理状态，以及失败或待复核原因。`;
}
