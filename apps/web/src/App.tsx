import {
  ArrowRight,
  Eye,
  EyeOff,
  GraduationCap,
  LogOut,
  MessageSquare,
  Paperclip,
  Send,
  ShieldCheck,
  Trash2,
  UserRound,
} from 'lucide-react';
import { ChangeEvent, FormEvent, useEffect, useMemo, useState } from 'react';

import { ApiError, getCurrentUser, loginUser, registerUser, sendAgentMessage, uploadFile } from './api/client';
import { clearToken, readToken, saveToken } from './auth/storage';
import type { SendMessageResponse, UploadedFile, User } from './types';

type AuthMode = 'login' | 'register';

export function App() {
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

function AuthPage({ onLogin }: { onLogin: (token: string, user: User) => void }) {
  const [mode, setMode] = useState<AuthMode>('login');
  const [username, setUsername] = useState('');
  const [email, setEmail] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [password, setPassword] = useState('');
  const [passwordVisible, setPasswordVisible] = useState(false);
  const [error, setError] = useState('');
  const [info, setInfo] = useState('');
  const [submitting, setSubmitting] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError('');
    setInfo('');
    setSubmitting(true);

    try {
      if (mode === 'register') {
        // 注册成功后不自动登录，避免用户混淆注册和登录两个动作。
        await registerUser({ username, password, display_name: displayName, email: email || undefined });
        setInfo('注册成功，请登录。');
        setMode('login');
        return;
      }
      const response = await loginUser({ username, password });
      onLogin(response.access_token, response.user);
    } catch (err) {
      setError(formatError(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="auth-shell">
      <header className="auth-header">
        <div className="school-mark">
          <GraduationCap size={30} />
        </div>
        <h1>西安理工大学</h1>
        <p>文件智能体系统 · v1.0</p>
      </header>

      <section className="auth-panel">
        <div className="segmented-control" aria-label="认证模式">
          <button
            className={mode === 'login' ? 'active' : ''}
            type="button"
            onClick={() => setMode('login')}
          >
            账号登录
          </button>
          <button
            className={mode === 'register' ? 'active' : ''}
            type="button"
            onClick={() => setMode('register')}
          >
            申请注册
          </button>
        </div>

        <form className="auth-form" onSubmit={submit}>
          <p className="auth-hint">
            {mode === 'login' ? '请使用工号和密码登录' : '请填写账号信息提交注册'}
          </p>
          <label>
            工号
            <input
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              autoComplete="username"
              placeholder="例：2024010001"
              required
            />
          </label>
          {mode === 'register' ? (
            <>
              <label>
                邮箱
                <input
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                  autoComplete="email"
                  placeholder="请输入邮箱"
                  type="email"
                />
              </label>
              <label>
                显示名称
                <input
                  value={displayName}
                  onChange={(event) => setDisplayName(event.target.value)}
                  autoComplete="name"
                  placeholder="请输入姓名"
                />
              </label>
            </>
          ) : null}
          <label className="password-label">
            <span>
              {mode === 'login' ? '登录密码' : '设置密码'}
              {mode === 'login' ? (
                <button className="text-button" type="button">
                  忘记密码?
                </button>
              ) : null}
            </span>
            <div className="password-field">
              <input
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
                minLength={mode === 'register' ? 6 : 1}
                placeholder={mode === 'login' ? '输入系统密码' : '至少 6 位密码'}
                type={passwordVisible ? 'text' : 'password'}
                required
              />
              <button
                className="password-toggle"
                type="button"
                onClick={() => setPasswordVisible((visible) => !visible)}
                title={passwordVisible ? '隐藏密码' : '显示密码'}
              >
                {passwordVisible ? <EyeOff size={20} /> : <Eye size={20} />}
              </button>
            </div>
          </label>

          {error ? <p className="form-message error">{error}</p> : null}
          {info ? <p className="form-message success">{info}</p> : null}

          <button className="primary-button auth-submit" disabled={submitting} type="submit">
            {submitting ? '处理中...' : mode === 'login' ? '安全登录' : '提交注册'}
            <ArrowRight size={20} />
          </button>
        </form>

        <div className="security-note">
          <ShieldCheck size={18} />
          <span>连接已加密 · 请勿在公共设备保存密码</span>
        </div>
      </section>

      <footer className="auth-footer">
        <nav aria-label="登录页辅助链接">
          <a href="#help">使用帮助</a>
          <a href="#support">技术支持</a>
          <a href="#privacy">隐私政策</a>
        </nav>
        <p>© 2026</p>
      </footer>
    </main>
  );
}

function ChatPage({
  token,
  user,
  onLogout,
}: {
  token: string;
  user: User;
  onLogout: () => void;
}) {
  const [message, setMessage] = useState('帮我读取并分类这批文件');
  const [uploadedFiles, setUploadedFiles] = useState<UploadedFile[]>([]);
  const [response, setResponse] = useState<SendMessageResponse | null>(null);
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [uploading, setUploading] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError('');
    setSubmitting(true);

    try {
      // conversation id 先固定为浏览器调试用会话，后续接会话列表后再由用户选择。
      const result = await sendAgentMessage(
        token,
        'web-chat',
        message,
        uploadedFiles.map((file) => file.document_id),
      );
      setResponse(result);
    } catch (err) {
      setError(formatError(err));
    } finally {
      setSubmitting(false);
    }
  }

  async function handleFileChange(event: ChangeEvent<HTMLInputElement>) {
    // 选择文件后立即上传，发送消息时只引用后端返回的 document_id。
    const files = Array.from(event.target.files ?? []);
    if (files.length === 0) {
      return;
    }

    setError('');
    setUploading(true);
    try {
      const results: UploadedFile[] = [];
      for (const file of files) {
        results.push(await uploadFile(token, file));
      }
      setUploadedFiles((current) => [...current, ...results]);
    } catch (err) {
      setError(formatError(err));
    } finally {
      setUploading(false);
      event.target.value = '';
    }
  }

  function removeUploadedFile(documentId: string) {
    // 当前后端还没有删除文件接口，这里只移除本次消息附件引用。
    setUploadedFiles((current) => current.filter((file) => file.document_id !== documentId));
  }

  return (
    <main className="app-shell">
      <header className="topbar">
        <div className="topbar-title">
          <MessageSquare size={22} />
          <span>File Agent</span>
        </div>
        <div className="user-box">
          <UserRound size={18} />
          <span>{user.display_name || user.username}</span>
          <button className="icon-button" type="button" onClick={onLogout} title="退出登录">
            <LogOut size={18} />
          </button>
        </div>
      </header>

      <section className="workspace">
        <div className="chat-column">
          <div className="section-heading">
            <h2>对话工作台</h2>
            <p>会话：web-chat</p>
          </div>

          <form className="composer" onSubmit={submit}>
            <textarea
              value={message}
              onChange={(event) => setMessage(event.target.value)}
              rows={4}
              required
            />
            <div className="composer-actions">
              <label className="file-picker">
                <Paperclip size={18} />
                <span>{uploading ? '上传中...' : '选择文件'}</span>
                <input disabled={uploading} multiple type="file" onChange={handleFileChange} />
              </label>
              <button className="primary-button send-button" disabled={submitting || uploading} type="submit">
                <Send size={18} />
                {submitting ? '发送中...' : '发送'}
              </button>
            </div>
          </form>

          {error ? <p className="form-message error">{error}</p> : null}

          {uploadedFiles.length > 0 ? (
            <div className="uploaded-files">
              {uploadedFiles.map((file) => (
                <div className="uploaded-file" key={file.document_id}>
                  <div>
                    <strong>{file.filename}</strong>
                    <span>{formatFileSize(file.size_bytes)} · {file.status}</span>
                  </div>
                  <button
                    className="icon-button"
                    type="button"
                    onClick={() => removeUploadedFile(file.document_id)}
                    title="移除附件"
                  >
                    <Trash2 size={16} />
                  </button>
                </div>
              ))}
            </div>
          ) : null}

          {response ? (
            <div className="result-panel">
              <div className="result-grid">
                <Metric label="AgentRun" value={response.agent_run.status} />
                <Metric label="Intent" value={response.agent_run.intent ?? '-'} />
                <Metric label="Tools" value={String(response.agent_run.tool_invocations.length)} />
              </div>
              <h3>Tool 调用</h3>
              <ul className="tool-list">
                {response.agent_run.tool_invocations.map((tool) => (
                  <li key={tool.id}>
                    <span>{tool.tool_name}</span>
                    <strong>{tool.status}</strong>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </div>
      </section>
    </main>
  );
}

function formatFileSize(sizeBytes: number): string {
  // 文件大小只用于界面展示，后端仍保存精确字节数。
  if (sizeBytes < 1024) {
    return `${sizeBytes} B`;
  }
  if (sizeBytes < 1024 * 1024) {
    return `${(sizeBytes / 1024).toFixed(1)} KB`;
  }
  return `${(sizeBytes / 1024 / 1024).toFixed(1)} MB`;
}

function Metric({ label, value }: { label: string; value: string }) {
  // 小指标组件保持固定结构，避免结果区布局随内容变化跳动。
  return (
    <div className="metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function formatError(error: unknown): string {
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
