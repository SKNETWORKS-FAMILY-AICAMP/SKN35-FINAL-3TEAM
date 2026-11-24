import React, { useCallback, useEffect, useState } from 'react';
import { Play, RefreshCw } from 'lucide-react';
import { request } from './api';

// 예약 작업 (관리자). 평일 09:10 시장 데이터 수집(UC-02-1), 09:20 금리·환율 리포트(UC-02), 09:30 만기 알림(UC-03).
// "지금 실행" 은 시각을 기다리지 않고 바로 돌린다 (UC-02 의 "지금 만들기").
const STATUS = { ok: ['완료', ''], error: ['실패', 'off'] };

function Result({ run }) {
  if (!run) return <span className="muted">아직 실행 안 함</span>;
  const [label, tone] = STATUS[run.status] ?? [run.status, ''];
  return <span>
    <span className={'badge ' + tone}>{label}</span>{' '}
    <small className="muted">{new Date(run.started_at).toLocaleString()} · {run.trigger === 'schedule' ? '예약' : '지금 실행'}</small>
    <pre className="schedule-result">{JSON.stringify(run.result, null, 1)}</pre>
  </span>;
}

export function SchedulesPage({ user, notify }) {
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(null);

  const load = useCallback(async () => {
    try { setData(await request(user, '/admin/schedules')); } catch (error) { notify(error.message); }
  }, [user, notify]);
  useEffect(() => { load(); }, [load]);

  async function run(id) {
    setBusy(id);
    try { await request(user, `/admin/schedules/${id}/run`, { method: 'POST' }); notify('실행했습니다.', 'ok'); }
    catch (error) { notify(error.message); }
    finally { setBusy(null); await load(); }
  }

  if (!data) return <p className="muted">불러오는 중…</p>;
  return <>
    <div className="actions start">
      <button className="quiet" onClick={load}><RefreshCw size={15} />새로고침</button>
      <span className="muted">{data.running ? '예약 실행 켜짐 (평일 정해진 시각에 돕니다)' : '예약 실행 꺼짐 (SCHEDULER_ENABLED=true 로 켭니다). 지금 실행은 됩니다.'}</span>
    </div>
    <ul className="request-log">{data.jobs.map(job => <li key={job.id} className="panel">
      <div className="schedule-head">
        <strong>{job.name}</strong>
        <span className="muted">평일 {job.at} · 다음 {new Date(job.next_run).toLocaleString()}</span>
        <button disabled={busy !== null} onClick={() => run(job.id)}><Play size={15} />{busy === job.id ? '실행 중…' : '지금 실행'}</button>
      </div>
      <Result run={job.last_run} />
    </li>)}</ul>
  </>;
}
