import { ArrowRight, Eye, EyeOff, GraduationCap, ShieldCheck } from 'lucide-react';
import { FormEvent, useState } from 'react';

import { loginUser, registerUser } from '../../api/client';
import { formatError } from '../../api/errors';
import type { User } from '../../types';

type AuthMode = 'login' | 'register';

type AuthPageProps = {
  onLogin: (token: string, user: User) => void;
};

export function AuthPage({ onLogin }: AuthPageProps) {
  // 登录页内部管理表单状态，App 只关心认证成功后的用户和 token。
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
