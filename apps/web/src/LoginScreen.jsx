import React, { useState } from 'react';
import { Eye, EyeOff, LoaderCircle, LogIn, Lock, Moon, Sun } from 'lucide-react';
import LoginFlow from './LoginFlow';

// 로그인 화면 (서버가 AUTH_MODE=session 일 때만 보인다). 왼쪽 브랜드 영역 + 오른쪽 입력 폼.
// 비밀번호 확인과 로그인 상태 기억은 서버가 한다 (/api/auth/login → HttpOnly 쿠키).
// 화면은 비밀번호도 토큰도 저장하지 않는다.
export default function LoginScreen({ onLogin, message, tone = 'error', theme, setTheme }) {
  const [loginId, setLoginId] = useState('');
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(tone === 'error' ? message ?? '' : '');
  const [info, setInfo] = useState(tone === 'ok' ? message ?? '' : '');

  async function submit(event) {
    event.preventDefault();
    setInfo('');
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

  return <div className="login-page with-flow" style={{ '--accent': '#78aaff' }}>
    <aside className="login-brand-side">
      {/* 이펙트: 천천히 떠도는 빛 번짐 + 점 격자 (CSS 만, '움직임 줄이기' 설정이면 멈춤) */}
      <div className="login-fx" aria-hidden="true"><span className="glow g1" /><span className="glow g2" /><span className="glow g3" /><span className="dots" /></div>
      <div className="login-heading"><span><strong>Agent Argus</strong><small>사내 업무 에이전트</small></span></div>
      <div className="login-brand-copy">
        <p className="login-eyebrow">AGENT ARGUS</p>
        <h2>믿고 맡기는<br /><em>사내 AI 에이전트</em></h2>
        <p>말로 설명하면 에이전트가 만들어지고, 회사가 허락한 도구와 문서 안에서만 일합니다.</p>
      </div>
      {/* 소재: 요청 하나가 플랫폼 안에서 지나가는 순서 (노드 그림, 불 켜진 단계가 차례로 옮겨 감) */}
      <LoginFlow />
      <p className="login-brand-foot"><Lock size={14} />권한이 확인된 사용자만 접속할 수 있습니다. 접속과 작업은 기록됩니다.</p>
    </aside>
    <main className="login-form-side">
      {setTheme && <button type="button" className="theme-toggle login-theme" onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
        aria-label={theme === 'dark' ? '라이트 모드로' : '다크 모드로'} title={theme === 'dark' ? '라이트 모드로' : '다크 모드로'}>
        {theme === 'dark' ? <Sun size={16} /> : <Moon size={16} />}</button>}
      <form className="panel login-panel" onSubmit={submit}>
        <span className="login-chip"><Lock size={12} />사내망 전용</span>
        <h1>로그인</h1>
        <p className="muted">사내 계정(사번)으로 로그인하세요.</p>
        {info && <p className="notice ok" role="status">{info}</p>}
        <label className="login-field"><span>사번</span>
          <input value={loginId} onChange={event => { setLoginId(event.target.value); setError(''); }}
            autoComplete="username" autoFocus spellCheck={false} disabled={busy} placeholder="예: E1001" />
        </label>
        <label className="login-field"><span>비밀번호</span>
          <span className="password-wrap">
            <input type={showPassword ? 'text' : 'password'} value={password} onChange={event => { setPassword(event.target.value); setError(''); }}
              autoComplete="current-password" disabled={busy} />
            <button type="button" className="password-eye" onClick={() => setShowPassword(value => !value)}
              aria-label={showPassword ? '비밀번호 숨기기' : '비밀번호 보기'}>{showPassword ? <EyeOff size={16} /> : <Eye size={16} />}</button>
          </span>
        </label>
        {error && <p className="login-error" role="alert">{error}</p>}
        <button className="primary login-submit" type="submit" disabled={busy}>
          {busy ? <LoaderCircle size={17} className="spin" /> : <LogIn size={17} />}{busy ? '확인 중' : '로그인'}</button>
        <p className="login-help muted">비밀번호를 잊었거나 계정이 잠기면(5회 실패 시 10분) 관리자에게 문의하세요.</p>
      </form>
    </main>
  </div>;
}
