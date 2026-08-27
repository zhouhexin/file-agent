// 前端 API 客户端只封装受控 HTTP 接口，不绕过后端 Tool、权限和路径策略。
import type {
  AgentCapabilityCatalog,
  AdminAgentRun,
  AgentRunDiagnostics,
  ClassificationFeedbackResponse,
  CapabilitySuggestion,
  ClassificationClarificationResult,
  ClassificationTaxonomyOptionsResponse,
  ConversationDetailResponse,
  DuplicateDecisionResponse,
  DuplicateReview,
  FileSearchClarificationResult,
  FilePreviewResponse,
  SpreadsheetPreviewResponse,
  FailedFileJob,
  UploadArchiveStatus,
  FilesystemJobResponse,
  OperationConfirmResponse,
  OperationPlanResponse,
  RenameBatchItemsResponse,
  SendMessageResponse,
  TokenResponse,
  UploadedFile,
  User,
} from '../types';

// API 地址集中管理，后续部署时只需要调整 VITE_API_BASE_URL。
const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8000/api';

type RequestOptions = {
  token?: string | null;
  body?: unknown;
  method?: 'GET' | 'POST' | 'DELETE';
};

export class ApiError extends Error {
  // 保留稳定业务错误码和 request_id，页面负责用户提示，运维可据此关联服务端日志。
  status: number;
  code: string;
  details: unknown;
  requestId: string | null;

  constructor(
    status: number,
    message: string,
    options: { code?: string; details?: unknown; requestId?: string | null } = {},
  ) {
    super(message);
    this.status = status;
    this.code = options.code ?? `HTTP_${status}`;
    this.details = options.details;
    this.requestId = options.requestId ?? null;
  }
}

async function readApiError(response: Response, fallbackMessage: string): Promise<ApiError> {
  // 新版服务统一返回 error Envelope；detail 仅用于滚动升级期间兼容尚未更新的旧实例。
  const data = await response.json().catch(() => ({}));
  const error = data?.error;
  const message = error?.message ?? data?.detail ?? fallbackMessage;
  return new ApiError(response.status, String(message), {
    code: typeof error?.code === 'string' ? error.code : undefined,
    details: error?.details,
    requestId: typeof error?.request_id === 'string' ? error.request_id : null,
  });
}

export async function registerUser(payload: {
  username: string;
  password: string;
  display_name: string;
  email?: string;
}): Promise<User> {
  // 注册只返回用户信息，不自动保存 token，避免用户误以为已登录。
  return request<User>('/auth/register', { body: payload });
}

export async function loginUser(payload: {
  username: string;
  password: string;
}): Promise<TokenResponse> {
  // 登录成功后由调用方决定如何保存 token。
  return request<TokenResponse>('/auth/login', { body: payload });
}

export async function getCurrentUser(token: string): Promise<User> {
  // 启动时用该接口校验本地 token 是否仍有效。
  return request<User>('/auth/me', { token });
}

export async function getAgentCapabilities(
  token: string,
): Promise<AgentCapabilityCatalog> {
  // 功能介绍页使用固定能力清单，避免前端和 Agent 能力说明出现两套文案。
  return request<AgentCapabilityCatalog>('/agent/capabilities', { token });
}

export async function sendAgentMessage(
  token: string,
  conversationId: string,
  content: string,
  documentIds: string[] = [],
): Promise<SendMessageResponse> {
  // 消息附件只传 document_id，真实文件内容已经通过上传接口持久化。
  return request<SendMessageResponse>(`/conversations/${conversationId}/messages`, {
    token,
    body: {
      content,
      attachments: documentIds.map((documentId) => ({ document_id: documentId })),
    },
  });
}

export async function resolveFileSearchClarification(
  token: string,
  clarificationId: string,
  payload: {
    option_id?: string | null;
    option_ids?: string[];
    custom_phrase?: string | null;
  },
): Promise<SendMessageResponse> {
  // 选择卡只提交后端签发的选项 ID；文件卡可多选，但浏览器不能构造检索参数。
  return request<SendMessageResponse>(
    `/file-search/clarifications/${clarificationId}/resolve`,
    {
      token,
      body: payload,
    },
  );
}

export async function getFileSearchClarification(
  token: string,
  clarificationId: string,
): Promise<FileSearchClarificationResult> {
  // 历史消息中的卡片可能已被其他标签页处理，刷新时必须读取后端最新状态。
  return request<FileSearchClarificationResult>(
    `/file-search/clarifications/${clarificationId}`,
    { token },
  );
}

export async function getConversationDetail(
  token: string,
  conversationId: string,
  options: { limit?: number; beforeMessageId?: string } = {},
): Promise<ConversationDetailResponse> {
  // 页面刷新后通过会话详情接口恢复历史消息、附件和对应 AgentRun。
  const params = new URLSearchParams();
  if (options.limit) {
    params.set('limit', String(options.limit));
  }
  if (options.beforeMessageId) {
    params.set('before_message_id', options.beforeMessageId);
  }
  const query = params.toString();
  return request<ConversationDetailResponse>(`/conversations/${conversationId}${query ? `?${query}` : ''}`, { token });
}

export async function clearConversationHistory(
  token: string,
  conversationId: string,
): Promise<{ conversation_id: string; cleared_message_count: number }> {
  // 后端只清空消息展示，不允许聊天页绕过确认逻辑删除文件或工作副本。
  return request<{ conversation_id: string; cleared_message_count: number }>(`/conversations/${conversationId}`, {
    token,
    method: 'DELETE',
  });
}

export async function getFilesystemJob(
  token: string,
  jobId: string,
): Promise<FilesystemJobResponse> {
  // 普通用户只能轮询自己创建的异步分类任务。
  return request<FilesystemJobResponse>(`/filesystem-jobs/${jobId}`, { token });
}

export async function getFailedFileJobs(token: string): Promise<FailedFileJob[]> {
  // 失败列表只对 ops/admin 开放，页面不接收绝对路径和文件正文。
  return request<FailedFileJob[]>('/admin/failed-files?limit=200', { token });
}

export async function getCapabilitySuggestions(
  token: string,
  status = '',
): Promise<CapabilitySuggestion[]> {
  // 管理员页面只读取脱敏能力建议，普通用户无法通过该接口获取内部 Catalog 信息。
  const query = status ? `?status=${encodeURIComponent(status)}` : '';
  return request<CapabilitySuggestion[]>(`/admin/capability-suggestions${query}`, { token });
}

export async function getAdminAgentRuns(
  token: string,
  status = '',
): Promise<AdminAgentRun[]> {
  // 管理员任务列表只读取最近审计摘要，状态过滤由后端校验。
  const params = new URLSearchParams({ limit: '100' });
  if (status) params.set('status', status);
  return request<AdminAgentRun[]>(`/admin/agent-runs?${params.toString()}`, { token });
}

export async function getAgentRunDiagnostics(
  token: string,
  agentRunId: string,
): Promise<AgentRunDiagnostics> {
  // 中文诊断时间线由后端聚合，前端不直接解析服务器日志或 Tool 原始输出。
  return request<AgentRunDiagnostics>(
    `/admin/agent-runs/${encodeURIComponent(agentRunId)}/diagnostics`,
    { token },
  );
}

export async function reviewCapabilitySuggestion(
  token: string,
  suggestionId: string,
  status: 'UNDER_REVIEW' | 'ACCEPTED' | 'REJECTED' | 'MERGED' | 'IMPLEMENTED',
  reviewNote = '',
): Promise<CapabilitySuggestion> {
  // 评审只改变候选状态，后端不会自动注册或启用 Tool/Skill。
  return request<CapabilitySuggestion>(
    `/admin/capability-suggestions/${suggestionId}/review`,
    {
      token,
      body: { status, review_note: reviewNote },
    },
  );
}

export async function fetchUploadedFileBlob(token: string, documentId: string): Promise<Blob> {
  // 附件内容接口返回原始文件流，前端根据类型决定预览或下载。
  const response = await fetch(`${API_BASE_URL}/files/${documentId}/content`, {
    headers: {
      Authorization: `Bearer ${token}`,
    },
  });

  if (!response.ok) {
    throw await readApiError(response, '文件打开失败');
  }
  return response.blob();
}

export async function fetchDocumentArtifactBlob(
  token: string,
  documentId: string,
  artifactId: string,
): Promise<Blob> {
  // 派生件下载只传稳定 ID；后端再次校验文档归属和存储路径边界。
  const response = await fetch(
    `${API_BASE_URL}/files/${documentId}/artifacts/${artifactId}`,
    { headers: { Authorization: `Bearer ${token}` } },
  );
  if (!response.ok) {
    throw await readApiError(response, '结构化结果下载失败');
  }
  return response.blob();
}

export async function getFilePreview(
  token: string,
  documentId: string,
): Promise<FilePreviewResponse> {
  // Office 文件预览只读取后端已解析正文，不把本地存储位置或原始二进制交给页面解析。
  return request<FilePreviewResponse>(`/files/${documentId}/preview`, { token });
}

export async function getSpreadsheetPreview(
  token: string,
  documentId: string,
  options: {
    sheetName?: string;
    rowOffset?: number;
    rowLimit?: number;
    columnOffset?: number;
    columnLimit?: number;
  } = {},
): Promise<SpreadsheetPreviewResponse> {
  // 已入库 Excel 读取后端持久化单元格事实；分页参数不能替代服务端访问控制。
  const params = new URLSearchParams();
  if (options.sheetName) params.set('sheet_name', options.sheetName);
  params.set('row_offset', String(options.rowOffset ?? 0));
  params.set('row_limit', String(options.rowLimit ?? 100));
  params.set('column_offset', String(options.columnOffset ?? 0));
  params.set('column_limit', String(options.columnLimit ?? 50));
  return request<SpreadsheetPreviewResponse>(
    `/files/${documentId}/spreadsheet-preview?${params.toString()}`,
    { token },
  );
}

export async function fetchManagedFileBlob(
  token: string,
  rootKey: string,
  relativePath: string,
): Promise<Blob> {
  // 受管文件只通过 root_key + relative_path 读取，避免前端接触容器绝对路径。
  const params = new URLSearchParams({
    root_key: rootKey,
    relative_path: relativePath,
  });
  const response = await fetch(`${API_BASE_URL}/managed-files/preview?${params.toString()}`, {
    headers: {
      Authorization: `Bearer ${token}`,
    },
  });

  if (!response.ok) {
    throw await readApiError(response, '文件预览失败');
  }
  return response.blob();
}

export async function uploadFile(token: string, file: File, conversationId: string): Promise<UploadedFile> {
  // 文件上传必须使用 FormData，不能复用 JSON 请求封装。
  const formData = new FormData();
  formData.append('file', file);
  formData.append('conversation_id', conversationId);

  const response = await fetch(`${API_BASE_URL}/files/upload`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
    },
    body: formData,
  });

  if (!response.ok) {
    throw await readApiError(response, '上传失败');
  }
  const data = await response.json();
  return data as UploadedFile;
}

export async function getDuplicateReview(
  token: string,
  uploadVersionId: string,
): Promise<DuplicateReview> {
  // 查重 worker 完成后只读取后端已经脱敏的候选，不在前端推断重复关系。
  return request<DuplicateReview>(`/uploads/${uploadVersionId}/duplicate-review`, { token });
}

export async function getUploadArchiveStatus(
  token: string,
  uploadVersionId: string,
): Promise<UploadArchiveStatus> {
  // 归档与导入均在 worker 中执行，前端只轮询脱敏业务状态。
  return request<UploadArchiveStatus>(`/uploads/${uploadVersionId}/archive-status`, { token });
}

export async function decideDuplicateReview(
  token: string,
  uploadVersionId: string,
  payload: {
    duplicate_review_id: string;
    decision: 'CONTINUE_UPLOAD' | 'USE_EXISTING_FILE' | 'CANCEL_UPLOAD';
    selected_existing_working_copy_id?: string | null;
  },
): Promise<DuplicateDecisionResponse> {
  // 重复上传确认使用独立受控接口；它不能被普通消息或 OperationPlan 确认替代。
  return request<DuplicateDecisionResponse>(`/uploads/${uploadVersionId}/duplicate-review/decision`, {
    token,
    body: payload,
  });
}

export async function deleteUploadedFile(token: string, documentId: string): Promise<void> {
  // 发送前删除会同时删除后端 Document、FileObject 和本地存储文件。
  const response = await fetch(`${API_BASE_URL}/files/${documentId}`, {
    method: 'DELETE',
    headers: {
      Authorization: `Bearer ${token}`,
    },
  });

  if (!response.ok) {
    throw await readApiError(response, '删除失败');
  }
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  // 统一封装 fetch，确保所有受保护请求都通过同一处追加 Bearer token。
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: options.method ?? (options.body ? 'POST' : 'GET'),
    headers: {
      'Content-Type': 'application/json',
      ...(options.token ? { Authorization: `Bearer ${options.token}` } : {}),
    },
    body: options.body ? JSON.stringify(options.body) : undefined,
  });

  if (!response.ok) {
    throw await readApiError(response, '请求失败');
  }
  const data = await response.json();
  return data as T;
}

export async function getOperationPlan(
  token: string,
  planId: string,
): Promise<OperationPlanResponse> {
  // OperationPlan 由后端按当前用户校验归属，前端只展示安全的逻辑路径。
  return request(`/operations/plans/${planId}`, { token });
}

export async function confirmOperationPlan(
  token: string,
  planId: string,
  excludedRenameBatchItemIds: string[] = [],
): Promise<OperationConfirmResponse> {
  // 高风险文件操作必须通过独立确认接口，不能复用普通消息发送。
  return request(`/operations/plans/${planId}/confirm`, {
    token,
    body: {
      confirmation: '确认执行',
      excluded_rename_batch_item_ids: excludedRenameBatchItemIds,
    },
  });
}

export async function createTrashRestorePlan(
  token: string,
  trashEntryId: string,
  conversationId: string,
): Promise<OperationPlanResponse> {
  // 完整文件名命中回收站后，只能对用户明确选择的单条记录创建恢复计划。
  return request(`/trash-entries/${trashEntryId}/restore-plan`, {
    token,
    body: { conversation_id: conversationId },
  });
}

export async function getRenameBatchItems(
  token: string,
  batchId: string,
  status: string,
  cursor = 0,
): Promise<RenameBatchItemsResponse> {
  // 大批量重命名明细按游标加载，避免聊天页面一次渲染全部文件。
  const query = new URLSearchParams({ status, cursor: String(cursor), limit: '20' });
  return request(`/file-renames/batches/${batchId}/items?${query.toString()}`, { token });
}

export async function submitClassificationFeedback(
  token: string,
  suggestionId: string,
  payload: {
    action: 'ACCEPT' | 'REJECT' | 'CORRECT';
    corrected_category_id?: string;
    relation_role?: 'PRIMARY' | 'SECONDARY' | 'RELATED' | 'DOCUMENT_TYPE';
    agent_run_id?: string;
  },
): Promise<ClassificationFeedbackResponse> {
  // 只有用户明确操作才写入反馈，未点击不推断为正样本。
  return request(`/classification/suggestions/${suggestionId}/feedback`, {
    token,
    body: payload,
  });
}

export async function getClassificationTaxonomyOptions(
  token: string,
): Promise<ClassificationTaxonomyOptionsResponse> {
  // 更正分类只能从后端当前启用的 taxonomy 选择，不能提交自由文本路径。
  return request('/classification/taxonomy/options', { token });
}

export async function getClassificationClarification(
  token: string,
  clarificationId: string,
): Promise<ClassificationClarificationResult> {
  return request(`/classification/clarifications/${clarificationId}`, { token });
}

export async function resolveClassificationClarification(
  token: string,
  clarificationId: string,
  optionId: string,
): Promise<ClassificationFeedbackResponse> {
  // 选择卡只回传后端签发的 option_id，禁止浏览器拼接文件或分类内部身份。
  return request(`/classification/clarifications/${clarificationId}/resolve`, {
    token,
    body: { option_id: optionId },
  });
}
