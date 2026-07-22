// 前端 API 客户端只封装受控 HTTP 接口，不绕过后端 Tool、权限和路径策略。
import type {
  AgentCapabilityCatalog,
  ClassificationFeedbackResponse,
  ConversationDetailResponse,
  DuplicateDecisionResponse,
  DuplicateReview,
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
};

export class ApiError extends Error {
  // 保留 HTTP 状态码，页面可据此展示登录失效、重复用户名等不同提示。
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
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

export async function getFilesystemJob(
  token: string,
  jobId: string,
): Promise<FilesystemJobResponse> {
  // 普通用户只能轮询自己创建的异步分类任务。
  return request<FilesystemJobResponse>(`/filesystem-jobs/${jobId}`, { token });
}

export async function fetchUploadedFileBlob(token: string, documentId: string): Promise<Blob> {
  // 附件内容接口返回原始文件流，前端根据类型决定预览或下载。
  const response = await fetch(`${API_BASE_URL}/files/${documentId}/content`, {
    headers: {
      Authorization: `Bearer ${token}`,
    },
  });

  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    const message = data?.detail ?? data?.error?.message ?? '文件打开失败';
    throw new ApiError(response.status, String(message));
  }
  return response.blob();
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
    const data = await response.json().catch(() => ({}));
    const message = data?.detail ?? data?.error?.message ?? '文件预览失败';
    throw new ApiError(response.status, String(message));
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

  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = data?.detail ?? data?.error?.message ?? '上传失败';
    throw new ApiError(response.status, String(message));
  }
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

  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = data?.detail ?? data?.error?.message ?? '删除失败';
    throw new ApiError(response.status, String(message));
  }
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  // 统一封装 fetch，确保所有受保护请求都通过同一处追加 Bearer token。
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: options.body ? 'POST' : 'GET',
    headers: {
      'Content-Type': 'application/json',
      ...(options.token ? { Authorization: `Bearer ${options.token}` } : {}),
    },
    body: options.body ? JSON.stringify(options.body) : undefined,
  });

  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = data?.detail ?? data?.error?.message ?? '请求失败';
    throw new ApiError(response.status, String(message));
  }
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
    corrected_category_path?: string[];
  },
): Promise<ClassificationFeedbackResponse> {
  // 只有用户明确操作才写入反馈，未点击不推断为正样本。
  return request(`/classification/suggestions/${suggestionId}/feedback`, {
    token,
    body: payload,
  });
}
