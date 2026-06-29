import type { ConversationDetailResponse, SendMessageResponse, TokenResponse, UploadedFile, User } from '../types';

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

export async function getConversationDetail(token: string, conversationId: string): Promise<ConversationDetailResponse> {
  // 页面刷新后通过会话详情接口恢复历史消息、附件和对应 AgentRun。
  return request<ConversationDetailResponse>(`/conversations/${conversationId}`, { token });
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

export async function uploadFile(token: string, file: File): Promise<UploadedFile> {
  // 文件上传必须使用 FormData，不能复用 JSON 请求封装。
  const formData = new FormData();
  formData.append('file', file);

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
