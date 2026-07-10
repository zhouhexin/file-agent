// 应用入口：管理登录态、首次引导跳转和三个轻量视图（login/chat/getting-started）。
// 不引入 React Router，仅使用 window.history + currentPath 状态。

import { useEffect, useMemo, useState } from 'react';
import { getCurrentUser } from './api/client';
import { clearToken, readToken, saveToken } from './auth/storage';
import { hasCompletedOnboarding, markOnboardingCompleted } from './auth/onboardingStorage';
import { AuthPage } from './features/auth/AuthPage';
import { ChatPage } from './features/chat/ChatPage';
import { OnboardingPage } from './features/onboarding/OnboardingPage';
import './features/chat/chat.css';
import type { User } from './types';

type AppPath = '/login' | '/chat' | '/getting-started';

function readInitialPath(): AppPath {
  // 直接读取当前 URL 以支持刷新 / 直接访问 /getting-started 的场景。
  const pathname = window.location.pathname;
  if (pathname === '/getting-started') {
    return '/getting-started';
  }
  if (pathname === '/login') {
    return '/login';
  }
  return '/chat';
}

function replacePath(path: AppPath): void {
  // 使用 replaceState 避免污染浏览器历史记录。
  window.history.replaceState(null, '', path);
}

function pushPath(path: AppPath): void {
  // pushState 让"返回聊天"等动作可以正常返回上一页。
  window.history.pushState(null, '', path);
}

export function App() {
  const [token, setToken] = useState(() => readToken());
  const [currentUser, setCurrentUser] = useState<User | null>(null);
  const [authChecked, setAuthChecked] = useState(false);
  const [currentPath, setCurrentPath] = useState<AppPath>(() => readInitialPath());
  const [pendingExample, setPendingExample] = useState('');

  useEffect(() => {
    // 监听浏览器前进后退，确保用户使用返回键也能回到正确页面。
    function handlePopState() {
      setCurrentPath(readInitialPath());
    }

    window.addEventListener('popstate', handlePopState);
    return () => window.removeEventListener('popstate', handlePopState);
  }, []);

  useEffect(() => {
    // 启动时校验 token，并根据是否完成过引导决定首屏页面。
    if (!token) {
      setAuthChecked(true);
      return;
    }

    getCurrentUser(token)
      .then((user) => {
        setCurrentUser(user);
        if (!hasCompletedOnboarding() && window.location.pathname !== '/getting-started') {
          replacePath('/getting-started');
          setCurrentPath('/getting-started');
        }
      })
      .catch(() => {
        clearToken();
        setToken(null);
        setCurrentUser(null);
        replacePath('/login');
        setCurrentPath('/login');
      })
      .finally(() => setAuthChecked(true));
  }, [token]);

  const route = useMemo(() => {
    // 'loading'：等待 /auth/me 校验；'login'：未登录或 token 失效；'protected'：已登录。
    if (!authChecked) {
      return 'loading';
    }
    return token && currentUser ? 'protected' : 'login';
  }, [authChecked, currentUser, token]);

  function handleLogin(nextToken: string, user: User) {
    saveToken(nextToken);
    setToken(nextToken);
    setCurrentUser(user);
    setPendingExample('');

    // 首次登录且未完成引导时跳转到引导页；否则直接进入聊天。
    if (!hasCompletedOnboarding()) {
      replacePath('/getting-started');
      setCurrentPath('/getting-started');
      return;
    }

    replacePath('/chat');
    setCurrentPath('/chat');
  }

  function handleLogout() {
    clearToken();
    setToken(null);
    setCurrentUser(null);
    setPendingExample('');
    replacePath('/login');
    setCurrentPath('/login');
  }

  function openChat() {
    replacePath('/chat');
    setCurrentPath('/chat');
  }

  function openOnboarding() {
    // 从聊天页再次进入引导页使用 pushState，让浏览器返回可以回到聊天。
    pushPath('/getting-started');
    setCurrentPath('/getting-started');
  }

  function completeOnboarding() {
    markOnboardingCompleted();
    setPendingExample('');
    openChat();
  }

  function openChatWithExample(example: string) {
    // 点击示例问题后写入标记、设置输入草稿、回到聊天。
    markOnboardingCompleted();
    setPendingExample(example);
    openChat();
  }

  if (route === 'loading') {
    return <p className="screen-center">正在校验登录状态...</p>;
  }

  if (route === 'login' || !token || !currentUser) {
    return <AuthPage onLogin={handleLogin} />;
  }

  if (currentPath === '/getting-started') {
    return (
      <OnboardingPage
        token={token}
        onStart={completeOnboarding}
        onBackToChat={completeOnboarding}
        onTryExample={openChatWithExample}
      />
    );
  }

  return (
    <ChatPage
      token={token}
      user={currentUser}
      onLogout={handleLogout}
      onOpenOnboarding={openOnboarding}
      initialDraft={pendingExample}
    />
  );
}
