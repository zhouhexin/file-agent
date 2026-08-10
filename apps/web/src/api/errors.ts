import { ApiError } from './client';

export function formatError(error: unknown): string {
  // 将 API 错误收敛成用户可读文本，避免直接暴露异常对象。
  if (error instanceof ApiError) {
    if (error.status === 401) {
      return '登录状态无效，请重新登录。';
    }
    // 409 是通用“状态冲突”，文件选择失效、计划已执行等业务也会使用该状态码；
    // 只有注册接口返回明确的用户名重复错误时，才能翻译成工号冲突。
    if (
      error.status === 409
      && error.message.trim().toLowerCase() === 'username already exists'
    ) {
      return '工号已存在。';
    }
    return error.message;
  }
  return '请求失败，请稍后重试。';
}
