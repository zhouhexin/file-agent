import { useEffect, useMemo, useState } from 'react';

import { getCurrentUser } from './api/client';
import { clearToken, readToken, saveToken } from './auth/storage';
import { AuthPage } from './features/auth/AuthPage';
import { ChatPage } from './features/chat/ChatPage';
import './features/chat/chat.css';
import type { User } from './types';

export function App() {
  // App 只负责认证态路由，登录页和聊天工作台分别在 feature 模块中维护。
  const [token, setToken] = useState<string | null>(() => readToken());
  const [currentUser, setCurrentUser] = useState<User | null>(null);
  const [authChecked, setAuthChecked] = useState(false);

  useEffect(() => {
    // 应用启动时校验本地 token；失败则清除本地登录态，避免页面停留在假登录状态。
    if (!token) {
      setAuthChecked(true);
      return;
    }
    getCurrentUser(token)
      .then(setCurrentUser)
      .catch(() => {
        clearToken();
        setToken(null);
        setCurrentUser(null);
      })
      .finally(() => setAuthChecked(true));
  }, [token]);

  const route = useMemo(() => {
    // 当前不用引入路由库，最小实现只根据登录态决定 /login 或 /chat 视图。
    if (!authChecked) {
      return 'loading';
    }
    return token && currentUser ? 'chat' : 'login';
  }, [authChecked, currentUser, token]);

  function handleLogin(nextToken: string, user: User) {
    // 登录成功后保存 token，并进入受保护的 Chat 工作台。
    saveToken(nextToken);
    setToken(nextToken);
    setCurrentUser(user);
    window.history.replaceState(null, '', '/chat');
  }

  function handleLogout() {
    // 当前后端没有 logout 接口，退出登录只清除本地 token。
    clearToken();
    setToken(null);
    setCurrentUser(null);
    window.history.replaceState(null, '', '/login');
  }

  if (route === 'loading') {
    return <div className="screen-center">正在校验登录状态...</div>;
  }

  if (route === 'login') {
    return <AuthPage onLogin={handleLogin} />;
  }

  return (
    <ChatPage
      token={token as string}
      user={currentUser as User}
      onLogout={handleLogout}
    />
  );
}
