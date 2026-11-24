// 도구 아이콘·색과, 도구가 수백~수천 개로 늘어도 정리되는 분류별 접기 목록.
// 아이콘: 도구 이름 → 이름·설명의 낱말 → 업무 분류 → 하는 일(조회/등록/발송) 순으로 고른다.
// 색: 업무 분류마다 하나 (아는 분류는 고정, 처음 보는 분류는 이름으로 팔레트에서 고른다).
import React, { useMemo, useState } from 'react';
import {
  Asterisk, BookOpenText, Calculator, CalendarDays, ChevronRight, ClipboardCheck, ClipboardList,
  Eye, FileSearch, FileText, FileUp, Globe, Mail, MailOpen, MailSearch, Minus, PenLine, Plus, Search, Send, Wrench, X,
} from 'lucide-react';

const BY_NAME = {
  add: Plus, subtract: Minus, multiply: Asterisk,
  search_doc: FileSearch, get_doc_chunk: BookOpenText, parse_doc: FileUp,
  today: CalendarDays,
  get_review_case: ClipboardList, submit_review_request: ClipboardCheck,
  search_mail: MailSearch, read_mail: MailOpen, send_mail: Send,
};
const BY_WORD = [
  [/mail|메일/i, Mail], [/doc|문서|규정/i, FileText], [/date|day|날짜|기한/i, CalendarDays],
  [/review|심사/i, ClipboardList], [/calc|계산|sum|rate|금리|환율/i, Calculator], [/web|search|검색/i, Globe],
];
const BY_DOMAIN = { 계산: Calculator, 문서: FileText, 날짜: CalendarDays, 심사: ClipboardList, 메일: Mail };
const BY_EFFECT = { read: Eye, write: PenLine, send: Send };

// Bezier 액센트 hue (라이트 400 / 다크 300 은 CSS 가 섞어서 맞춘다)
const HUES = { 계산: '#e67f2b', 문서: '#3292e3', 날짜: '#09b2ac', 심사: '#8e57e7', 메일: '#d64bb5', 기타: '#7b7b7b' };
const PALETTE = ['#5e56f0', '#20ab55', '#e1535d', '#a9b110', '#424fab', '#edae0d', '#068e95'];

export function toolHue(domain) {
  const key = domain || '기타';
  if (HUES[key]) return HUES[key];
  let h = 0;
  for (const ch of key) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return PALETTE[h % PALETTE.length];
}

export function toolIcon({ name, display_name = '', description = '', domain, effect }) {
  if (BY_NAME[name]) return BY_NAME[name];
  const text = `${name} ${display_name} ${description}`;
  const word = BY_WORD.find(([re]) => re.test(text));
  return word?.[1] ?? BY_DOMAIN[domain] ?? BY_EFFECT[effect] ?? Wrench;
}

export function ToolIcon({ tool, size = 16, box = 28 }) {
  const Icon = toolIcon(tool);
  return <span className="tool-icon" style={{ '--hue': toolHue(tool.domain), width: box, height: box }} aria-hidden="true"><Icon size={size} /></span>;
}

// 도구 설명은 LLM 에게 주는 사용 지침이라 길다. 화면에는 첫 문장만 보여 주고, 나머지는 '자세히'로 펼친다.
export function splitDesc(text = '') {
  const t = text.trim();
  const m = t.match(/^(.+?[.。!?])(\s+|$)/);
  const first = m ? m[1] : t;
  return { short: first.length > 90 ? first.slice(0, 88) + '…' : first, more: t.length > first.length || first.length > 90 };
}

function Desc({ text }) {
  const [open, setOpen] = useState(false);
  const { short, more } = splitDesc(text);
  return <p className="tool-desc">{open ? text : short}
    {more && <button type="button" className="desc-more" onClick={event => { event.preventDefault(); event.stopPropagation(); setOpen(value => !value); }}>
      {open ? '접기' : '자세히'}</button>}</p>;
}

const norm = text => (text ?? '').toLowerCase();
function matches(tool, q) {
  if (!q) return true;
  const hay = norm(`${tool.name} ${tool.display_name} ${tool.description} ${tool.domain}`);
  return q.split(/\s+/).filter(Boolean).every(word => hay.includes(word));
}
function group(tools) {
  const groups = new Map();
  for (const tool of tools) {
    const key = tool.domain || '기타';
    groups.set(key, [...(groups.get(key) ?? []), tool]);
  }
  return [...groups];
}

// 에이전트 편집용: 검색 + 고른 도구 칩 + 분류별 접기
export function ToolPicker({ tools, selected, suggested = [], onChange, effectLabel, Badge }) {
  const [query, setQuery] = useState('');
  const q = norm(query.trim());
  const visible = useMemo(() => tools.filter(tool => matches(tool, q)), [tools, q]);
  const groups = group(visible);
  // 처음에는 고른 도구·추천 도구가 있는 분류만 펼친다. 검색 중에는 맞는 분류를 모두 펼친다.
  const [open, setOpen] = useState(() => new Set(group(tools)
    .filter(([, items]) => items.some(t => selected.includes(t.name) || suggested.includes(t.name))).map(([d]) => d)));
  const toggleGroup = domain => setOpen(prev => { const next = new Set(prev); next.has(domain) ? next.delete(domain) : next.add(domain); return next; });
  const allOpen = groups.length > 0 && groups.every(([d]) => open.has(d));
  const toggle = (name, on) => onChange(on ? [...selected, name] : selected.filter(item => item !== name));
  const chosen = tools.filter(tool => selected.includes(tool.name));

  return <div className="tool-picker">
    <div className="picker-bar">
      <label className="search"><Search size={16} /><input aria-label="도구 검색" placeholder={`도구 ${tools.length}개 중 검색 (이름·설명·분류)`}
        value={query} onChange={event => setQuery(event.target.value)} />
        {query && <button type="button" className="icon" aria-label="검색어 지우기" onClick={() => setQuery('')}><X size={14} /></button>}</label>
      {!q && groups.length > 1 && <button type="button" className="quiet small" onClick={() => setOpen(allOpen ? new Set() : new Set(groups.map(([d]) => d)))}>
        {allOpen ? '모두 접기' : '모두 펼치기'}</button>}
    </div>
    <div className="chosen" aria-live="polite">
      <span className="muted">선택 {chosen.length}개</span>
      {chosen.map(tool => <span className="chosen-chip" key={tool.name} style={{ '--hue': toolHue(tool.domain) }}>
        {React.createElement(toolIcon(tool), { size: 13 })}{tool.display_name}
        <button type="button" aria-label={tool.display_name + ' 빼기'} onClick={() => toggle(tool.name, false)}><X size={12} /></button>
      </span>)}
    </div>
    {groups.map(([domain, items]) => {
      const isOpen = Boolean(q) || open.has(domain);
      const picked = items.filter(t => selected.includes(t.name)).length;
      return <section key={domain} className={'tool-group' + (isOpen ? ' open' : '')} style={{ '--hue': toolHue(domain) }}>
        <button type="button" className="group-head" aria-expanded={isOpen} onClick={() => !q && toggleGroup(domain)}>
          <ChevronRight size={15} className="chev" />
          <span className="group-dot" />
          <strong>{domain}</strong><span className="muted">{items.length}개</span>
          {picked > 0 && <span className="group-picked">{picked}개 선택</span>}
          {items.some(t => suggested.includes(t.name) && !selected.includes(t.name)) && <span className="group-suggest">추천 있음</span>}
        </button>
        {isOpen && <div className="tool-row">
          {items.map(tool => {
            const on = selected.includes(tool.name);
            const hint = suggested.includes(tool.name) && !on;
            return <label key={tool.name} className={'tool ' + (on ? 'checked ' : '') + (hint ? 'suggested' : '')} title={tool.description}>
              <input type="checkbox" checked={on} onChange={event => toggle(tool.name, event.target.checked)} />
              <ToolIcon tool={tool} />
              <span className="tool-text"><span className="tool-title">{tool.display_name}<code> {tool.name}</code>
                <span className="tool-badges"><Badge tone={'effect-' + tool.effect}>{effectLabel[tool.effect] ?? tool.effect}</Badge>
                  {hint && <Badge tone="suggest">추천</Badge>}</span></span>
                <small>{splitDesc(tool.description).short}</small></span>
            </label>;
          })}
        </div>}
      </section>;
    })}
    {q && !visible.length && <p className="muted">"{query}"에 맞는 도구가 없습니다.</p>}
    {!tools.length && <p className="muted">등록된 도구가 없습니다.</p>}
  </div>;
}

// 도구 목록 화면: 검색 + 분류별 접기
export function ToolCatalog({ tools, effectLabel, Badge }) {
  const [query, setQuery] = useState('');
  const [closed, setClosed] = useState(() => new Set());
  const q = norm(query.trim());
  const groups = group(tools.filter(tool => matches(tool, q)));
  return <>
    <div className="picker-bar">
      <label className="search"><Search size={16} /><input aria-label="도구 검색" placeholder={`도구 ${tools.length}개 중 검색`} value={query} onChange={event => setQuery(event.target.value)} /></label>
    </div>
    {groups.map(([domain, items]) => {
      const isOpen = Boolean(q) || !closed.has(domain);
      return <section key={domain} className={'tool-group catalog' + (isOpen ? ' open' : '')} style={{ '--hue': toolHue(domain) }}>
        <button type="button" className="group-head" aria-expanded={isOpen} onClick={() => !q && setClosed(prev => {
          const next = new Set(prev); next.has(domain) ? next.delete(domain) : next.add(domain); return next;
        })}><ChevronRight size={15} className="chev" /><span className="group-dot" /><strong>{domain}</strong><span className="muted">{items.length}개</span></button>
        {isOpen && <ul className="tool-catalog">{items.map(tool => <li className="panel catalog-tool" key={tool.name}>
          <ToolIcon tool={tool} size={20} box={42} />
          <div><h2>{tool.display_name} <code>{tool.name}</code> <Badge tone={'effect-' + tool.effect}>{effectLabel[tool.effect] ?? tool.effect}</Badge></h2><Desc text={tool.description} /></div>
        </li>)}</ul>}
      </section>;
    })}
    {q && !groups.length && <p className="muted">"{query}"에 맞는 도구가 없습니다.</p>}
    {!tools.length && <p className="muted">등록된 도구가 없습니다.</p>}
  </>;
}

