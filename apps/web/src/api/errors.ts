import { ApiError } from './client';

export function formatError(error: unknown): string {
  // 将 API 错误收敛成用户可读文本，避免直接暴露异常对象。
  if (error instanceof ApiError) {
    if (error.status === 401) {
      return '登录状态无效，请重新登录。';
    }
    if (error.status === 409) {
      return '用户名已存在。';
    }
    return error.message;
  }
  return '请求失败，请稍后重试。';
}
