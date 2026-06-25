// token 存储集中封装，便于后续替换为更安全的 cookie/session 方案。

const TOKEN_KEY = 'file-agent-token';

export function readToken(): string | null {
  // 服务端渲染不存在 window；虽然当前是纯前端应用，仍保留保护。
  if (typeof window === 'undefined') {
    return null;
  }
  return window.localStorage.getItem(TOKEN_KEY);
}

export function saveToken(token: string): void {
  window.localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  window.localStorage.removeItem(TOKEN_KEY);
}
