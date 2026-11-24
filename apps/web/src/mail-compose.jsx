import React, { useEffect, useState } from 'react';
import { Mail, X } from 'lucide-react';
import { draftFromMessage } from './mail-draft';

export function MailComposer({ user, sessionId, text, api, busy, onBusyChange, onResult }) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState(() => draftFromMessage(text));
  const [people, setPeople] = useState([]);
  const [selected, setSelected] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState(null);
  // 응답 유실 시에도 같은 내용과 키로만 조회하여 이중 발송을 막는다.
  const [attempt, setAttempt] = useState(null);
  const [requestId, setRequestId] = useState(() => crypto.randomUUID());

  useEffect(() => {
    if (!open) return;
    let alive = true;
    setLoading(true);
    api(user, '/api/mail/recipients?session_id=' + encodeURIComponent(sessionId))
      .then(items => { if (alive) { setPeople(items); setError(''); } })
      .catch(e => { if (alive) setError(e.message); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [open, user, sessionId, api]);

  async function send(event) {
    event.preventDefault();
    if (busy) return;
    const request = attempt || { session_id: sessionId, request_id: requestId,
      recipient_ids: selected, subject: draft.subject.trim(), body: draft.body.trim() };
    setAttempt(request);
    setError('');
    onBusyChange(true);
    try {
      const response = await api(user, '/api/mail/send', { method: 'POST', body: request });
      setResult(response);
      onResult(response);
    } catch (e) {
      setError(e.message + (e.status === 0 || e.status >= 500 ? ' 같은 요청으로 결과를 확인하거나 보낸편지함을 확인해 주세요.' : ''));
      if (e.status === 422 || e.status === 404) { setAttempt(null); setRequestId(crypto.randomUUID()); }
    } finally { onBusyChange(false); }
  }

  if (!open) return <button className="quiet" disabled={busy} onClick={() => setOpen(true)}><Mail size={15} />메일로 보내기</button>;
  return <form className="mail-compose panel" onSubmit={send}>
    <div className="actions"><strong>메일 초안 확인</strong><button type="button" className="quiet" disabled={busy} onClick={() => setOpen(false)} aria-label="메일 초안 닫기"><X size={16} /></button></div>
    <p className="muted">본인 Gmail로 발송됩니다. 제목·본문을 확인하고 받는 직원을 선택해 주세요.</p>
    <label>제목<input required maxLength={200} value={draft.subject} disabled={busy || !!attempt} onChange={e => setDraft({ ...draft, subject: e.target.value })} /></label>
    <label>본문<textarea required rows={9} maxLength={20000} value={draft.body} disabled={busy || !!attempt} onChange={e => setDraft({ ...draft, body: e.target.value })} /></label>
    <fieldset disabled={busy || !!attempt}><legend>받는 사람 · 본인 제외</legend>
      {loading ? <p role="status">직원 목록을 불러오는 중입니다…</p> : people.length === 0 ? <p className="muted">선택할 Gmail 연결 직원이 없습니다.</p> : people.map(person =>
        <label key={person.user_id} className="mail-person"><input type="checkbox" checked={selected.includes(person.user_id)}
          disabled={!selected.includes(person.user_id) && selected.length >= 10}
          onChange={e => setSelected(old => e.target.checked ? [...old, person.user_id] : old.filter(id => id !== person.user_id))} />
          <span>{person.name} · {person.user_id}<small>{person.email}</small></span></label>)}
    </fieldset>
    {error && <p role="alert" className="mail-error">{error}</p>}
    {result ? <p role="status">{result.message}</p> : <button className="primary" type="submit" disabled={busy || loading || selected.length === 0 || !draft.subject.trim() || !draft.body.trim()}>
      {busy ? '승인·발송 결과를 기다리는 중…' : attempt ? '같은 요청 결과 확인' : '선택한 직원에게 발송 요청'}</button>}
    {attempt && !result && <p className="muted">발송 요청 후에는 내용을 바꿀 수 없습니다. 승인 카드에서 최종 확인해 주세요.</p>}
    {result?.status === 'not_sent' && <button type="button" className="quiet" onClick={() => { setAttempt(null); setRequestId(crypto.randomUUID()); setResult(null); setError(''); }}>초안 수정 후 다시 요청</button>}
  </form>;
}
