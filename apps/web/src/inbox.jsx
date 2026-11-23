import React, { useCallback, useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { request } from './api';

// 내 알림함 (UC-03). 서버가 본인 알림만 돌려준다.
const BUCKETS = ['만기 경과', '7일 이내', '8~30일'];
const BUCKET_TONE = { '만기 경과': 'off', '7일 이내': 'wait', '8~30일': '' };

function Counts({ counts }) {
  return <span className="inbox-counts">{BUCKETS.map(bucket =>
    <span key={bucket} className={'badge ' + BUCKET_TONE[bucket]}>{bucket} {counts?.[bucket] ?? 0}</span>)}</span>;
}

function AlertDetail({ alert }) {
  const { payload } = alert;
  const first = new Set(payload.first_look ?? []);
  return <article className="panel inbox-detail">
    <h2>{alert.title}</h2>
    <p className="muted">받은 시각 {new Date(alert.created_at).toLocaleString()} · 기준일 {payload.as_of}</p>
    {alert.summary
      ? <p className="inbox-summary">{alert.summary}</p>
      : alert.summary_dropped && <p className="muted">안내 문장에 목록에 없는 숫자가 있어 문장을 빼고 목록만 받았습니다.</p>}
    <Counts counts={payload.counts} />
    {payload.items?.length ? <div className="answer-table answer-markdown"><table>
      <thead><tr><th>구분</th><th>고객</th><th>상품</th><th>잔액</th><th>만기일</th><th>남은 날</th><th>할 일</th></tr></thead>
      <tbody>{payload.items.map(item => <tr key={item.loan_id}>
        <td><span className={'badge ' + BUCKET_TONE[item.bucket]}>{item.bucket}</span>{first.has(item.loan_id) && <span className="badge suggest">먼저 볼 것</span>}</td>
        <td>{item.customer} <small className="muted">{item.customer_type}</small></td>
        <td>{item.product}</td>
        <td>{item.balance}</td>
        <td>{item.maturity_date}</td>
        <td>{item.days_left < 0 ? `${-item.days_left}일 지남` : `D-${item.days_left}`}</td>
        <td>{item.follow_up}</td>
      </tr>)}</tbody>
    </table></div> : <p className="muted">기간 안에 만기가 오는 대출이 없습니다.</p>}
  </article>;
}

export function InboxPage({ user, notify }) {
  const [items, setItems] = useState([]);
  const [selected, setSelected] = useState(null);

  const load = useCallback(async () => {
    try { setItems(await request(user, '/inbox')); } catch (error) { notify(error.message); }
  }, [user, notify]);
  useEffect(() => { load(); setSelected(null); }, [load]);

  async function open(id) {
    try { setSelected(await request(user, '/inbox/' + id)); await load(); } catch (error) { notify(error.message); }
  }

  return <>
    <div className="actions start"><button className="quiet" onClick={load}><RefreshCw size={15} />새로고침</button></div>
    {items.length ? <ul className="request-log">{items.map(item => <li key={item.alert_id}
      className={'panel inbox-item' + (selected?.alert_id === item.alert_id ? ' selected' : '')}>
      <button className="inbox-open" onClick={() => open(item.alert_id)}>
        <strong>{!item.read_at && <span className="status-dot" />}{item.title}</strong>
        <span className="muted">{new Date(item.created_at).toLocaleString()}</span>
        <Counts counts={item.counts} />
      </button>
    </li>)}</ul> : <p className="muted">받은 알림이 없습니다. 채팅에서 "대출 만기 알림" 에이전트에게 요청해 보세요.</p>}
    {selected && <AlertDetail alert={selected} />}
  </>;
}
