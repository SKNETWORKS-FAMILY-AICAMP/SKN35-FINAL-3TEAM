import React, { useState } from 'react';
import { LogIn, Workflow } from 'lucide-react';

// 로그인 화면 (서버가 AUTH_MODE=session 일 때만 보인다). 기존 워크스페이스 화면과 같은 색·모양을 쓴다.
// 비밀번호 확인과 로그인 상태 기억은 서버가 한다 (/api/auth/login → HttpOnly 쿠키).
// 화면은 비밀번호도 토큰도 저장하지 않는다.
export default function LoginScreen({ onLogin, message }) {
  const [loginId, setLoginId] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(message ?? '');

  async function submit(event) {
    event.preventDefault();
    if (!loginId.trim() || !password) { setError('사번과 비밀번호를 입력해 주세요.'); return; }
    setBusy(true); setError('');
    let res;
    try {
      res = await fetch('/api/auth/login', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ login_id: loginId.trim(), password }),
      });
    } catch {
      setError('서버에 연결할 수 없습니다. 백엔드(uvicorn)가 켜져 있는지 확인하세요.');
      setBusy(false);
      return;
    }
    const data = await res.json().catch(() => null);
    if (!res.ok) {
      setError(typeof data?.detail === 'string' ? data.detail
        : res.status >= 500 ? '서버 오류로 로그인하지 못했습니다. DB(WSL PostgreSQL)가 켜져 있는지 확인하세요.'
        : '로그인하지 못했습니다.');
      setBusy(false);
      return;
    }
    setPassword('');
    onLogin(data);
  }

  return <div className="login-page" style={{ '--accent': '#78aaff' }}>
    <form className="panel login-panel" onSubmit={submit}>
      <div className="login-heading"><span className="sidebar-mark"><Workflow size={19} /></span><strong>Agent Workspace</strong></div>
      <h1>로그인</h1>
      <p className="muted">사내 계정으로 로그인하세요.</p>
      <label className="login-field"><span>사번</span>
        <input value={loginId} onChange={event => { setLoginId(event.target.value); setError(''); }}
          autoComplete="username" autoFocus spellCheck={false} disabled={busy} placeholder="예: E1001" />
      </label>
      <label className="login-field"><span>비밀번호</span>
        <input type="password" value={password} onChange={event => { setPassword(event.target.value); setError(''); }}
          autoComplete="current-password" disabled={busy} />
      </label>
      {error && <p className="login-error" role="alert">{error}</p>}
      <button className="primary login-submit" type="submit" disabled={busy}><LogIn size={17} />{busy ? '확인 중' : '로그인'}</button>
    </form>
  </div>;
}
