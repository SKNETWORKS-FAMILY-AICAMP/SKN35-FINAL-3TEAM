import React, { useEffect, useMemo, useRef, useState } from 'react';
import { ArrowRight, Bot, Check, ChevronDown, Search, X } from 'lucide-react';

// 작업 에이전트 고르기: 에이전트가 많아져도 찾기 쉽게
//  - 맨 위 검색칸 (이름·설명·만든 사람)
//  - 검색하지 않을 때는 묶음: 최근 쓴 것 / 기본 / 내 에이전트 / 부서 공유 / 전사 공유 (묶음마다 5개까지, 나머지는 '더 보기')
//  - 맨 아래 '모든 에이전트 관리'는 에이전트 관리 화면으로
// 키보드: ↑↓ 이동, Enter 선택, Esc 닫기. 바깥을 누르면 닫힌다.
const PER_GROUP = 5;

export default function AgentSelect({ agents, value, onChange, disabled, user, recentIds = [] }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [expanded, setExpanded] = useState(() => new Set());
  const [cursor, setCursor] = useState(0);
  const root = useRef(null);
  const search = useRef(null);
  const current = agents.find(agent => (agent.agent_id ?? null) === (value ?? null)) ?? agents[0];

  const q = query.trim().toLowerCase();
  const groups = useMemo(() => {
    if (q) {
      const words = q.split(/\s+/);
      const hits = agents.filter(agent => words.every(word =>
        `${agent.name} ${agent.description ?? ''} ${agent.owner_user_id ?? ''}`.toLowerCase().includes(word)));
      return [{ key: 'result', title: `검색 결과 ${hits.length}개`, items: hits }];
    }
    const byId = new Map(agents.map(agent => [agent.agent_id ?? null, agent]));
    const recent = [...new Set(recentIds)].map(id => byId.get(id ?? null)).filter(Boolean).slice(0, 3);
    const rest = agents.filter(agent => !recent.includes(agent));
    const list = [
      { key: 'recent', title: '최근 쓴 것', items: recent },
      { key: 'default', title: '기본', items: rest.filter(agent => agent.agent_id === null) },
      { key: 'mine', title: '내 에이전트', items: rest.filter(agent => agent.agent_id !== null && agent.owner_user_id === user) },
      { key: 'dept', title: '부서 공유', items: rest.filter(agent => agent.agent_id !== null && agent.owner_user_id !== user && agent.scope === 'dept') },
      { key: 'company', title: '전사 공유', items: rest.filter(agent => agent.agent_id !== null && agent.owner_user_id !== user && agent.scope === 'company') },
      { key: 'other', title: '그 밖에 볼 수 있는 것', items: rest.filter(agent => agent.agent_id !== null && agent.owner_user_id !== user && !['dept', 'company'].includes(agent.scope)) },
    ];
    return list.filter(group => group.items.length);
  }, [agents, q, recentIds, user]);

  // 화면에 보이는 항목 (묶음마다 5개까지, '더 보기'를 누른 묶음은 전부)
  const shown = groups.map(group => ({
    ...group,
    visible: q || expanded.has(group.key) ? group.items : group.items.slice(0, PER_GROUP),
  }));
  const flat = shown.flatMap(group => group.visible);

  useEffect(() => {
    if (!open) return undefined;
    setQuery(''); setExpanded(new Set());
    setTimeout(() => search.current?.focus(), 0);
    const close = event => { if (!root.current?.contains(event.target)) setOpen(false); };
    addEventListener('mousedown', close);
    return () => removeEventListener('mousedown', close);
  }, [open]);
  useEffect(() => { setCursor(0); }, [q]);

  function pick(agent) { setOpen(false); if (agent && agent !== current) onChange(agent.agent_id ?? null); }
  function onKey(event) {
    if (!open && (event.key === 'ArrowDown' || event.key === 'Enter')) { event.preventDefault(); setOpen(true); return; }
    if (!open) return;
    if (event.key === 'Escape') { event.preventDefault(); setOpen(false); }
    else if (event.key === 'ArrowDown') { event.preventDefault(); setCursor(i => Math.min(flat.length - 1, i + 1)); }
    else if (event.key === 'ArrowUp') { event.preventDefault(); setCursor(i => Math.max(0, i - 1)); }
    else if (event.key === 'Enter') { event.preventDefault(); pick(flat[cursor]); }
  }

  let index = -1;
  return <div className="agent-select" ref={root} onKeyDown={onKey}>
    <button type="button" className="agent-picker" disabled={disabled} aria-haspopup="listbox" aria-expanded={open}
      aria-label={'작업 에이전트: ' + current.name} onClick={() => setOpen(value => !value)}>
      <Bot size={17} /><span className="agent-select-name">{current.name}</span><ChevronDown size={15} className="agent-select-chev" />
    </button>
    {open && <div className="agent-menu">
      <label className="agent-menu-search"><Search size={15} />
        <input ref={search} value={query} onChange={event => setQuery(event.target.value)}
          placeholder={`에이전트 ${agents.length}개 중 검색`} aria-label="에이전트 검색" />
        {query && <button type="button" aria-label="검색어 지우기" onClick={() => { setQuery(''); search.current?.focus(); }}><X size={14} /></button>}
      </label>
      <div className="agent-menu-list" role="listbox" aria-label="작업 에이전트">
        {shown.map(group => <section key={group.key} className="agent-menu-group">
          <p className="agent-menu-title">{group.title}{!q && <span>{group.items.length}</span>}</p>
          {group.visible.map(agent => {
            index += 1;
            const at = index;
            const selected = agent === current;
            const other = agent.agent_id && agent.owner_user_id !== user;
            return <div key={group.key + (agent.agent_id ?? 'default')} role="option" aria-selected={selected}
              className={'agent-menu-item ' + (at === cursor ? 'active ' : '') + (selected ? 'selected' : '')}
              onMouseEnter={() => setCursor(at)} onClick={() => pick(agent)}>
              <span className="agent-menu-icon"><Bot size={15} /></span>
              <span className="agent-menu-text"><strong>{agent.name}</strong>
                <small>{other ? `만든 사람 ${agent.owner_user_id} · ` : ''}{agent.description || '설명 없음'}</small></span>
              {selected && <Check size={16} className="agent-menu-check" />}
            </div>;
          })}
          {!q && group.items.length > PER_GROUP && <button type="button" className="agent-menu-more" onClick={() => setExpanded(prev => {
            const next = new Set(prev); next.has(group.key) ? next.delete(group.key) : next.add(group.key); return next;
          })}>{expanded.has(group.key) ? '접기' : `${group.items.length - PER_GROUP}개 더 보기`}</button>}
        </section>)}
        {q && !flat.length && <p className="agent-menu-empty">"{query}"에 맞는 에이전트가 없습니다.</p>}
      </div>
      <a className="agent-menu-foot" href="#agents" onClick={() => setOpen(false)}>모든 에이전트 관리<ArrowRight size={14} /></a>
    </div>}
  </div>;
}
