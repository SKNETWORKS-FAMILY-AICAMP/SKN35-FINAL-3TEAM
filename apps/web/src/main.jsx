import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import {
  ArrowUp, Bot, Braces, Check, ChevronRight, FileText, LayoutTemplate,
  ListChecks, MessageSquare, Minus, Plus, Search, Settings2, Sparkles, Workflow, Wrench, X,
} from 'lucide-react';
import './style.css';

const navigation = [
  { id: 'chat', name: '작업 채팅', detail: '에이전트와 대화', icon: MessageSquare, color: '#78aaff' },
  { id: 'create', name: '에이전트 만들기', detail: '자연어로 초안 생성', icon: Sparkles, color: '#ba97ff' },
  { id: 'agents', name: '내 에이전트', detail: '저장된 설정 관리', icon: Settings2, color: '#65ddb0' },
  { id: 'templates', name: '시나리오 템플릿', detail: '사전 구성으로 시작', icon: LayoutTemplate, color: '#f6b273' },
  { id: 'tools', name: '도구 목록', detail: '등록된 도구 보기', icon: Wrench, color: '#74d5e4' },
];

// IDs and descriptions match the harness arithmetic tool specs.
const harnessTools = [
  { id: 'add', label: '덧셈', description: '두 숫자를 더합니다.', pattern: /덧셈|더하|더해|\badd\b/i, icon: Plus, operator: '+', calculate: (a, b) => a + b },
  { id: 'subtract', label: '뺄셈', description: '두 숫자를 뺍니다.', pattern: /뺄셈|빼기|빼는|빼줘|\bsubtract\b/i, icon: Minus, operator: '-', calculate: (a, b) => a - b },
  { id: 'multiply', label: '곱셈', description: '두 숫자를 곱합니다.', pattern: /곱셈|곱하|곱해|\bmultiply\b/i, icon: X, operator: '*', calculate: (a, b) => a * b },
];

const demoTools = [
  { id: 'text', label: '텍스트 정리', pattern: /요약|정리|문서|회의/, icon: FileText },
  { id: 'code', label: '코드 검토', pattern: /코드|개발|리뷰|프로그래밍/, icon: Braces },
  { id: 'plan', label: '체크리스트', pattern: /계획|일정|기획|체크리스트/, icon: ListChecks },
];
const arithmeticExpression = /^\s*(-?(?:\d+(?:\.\d+)?|\.\d+))\s*([+*×−-])\s*(-?(?:\d+(?:\.\d+)?|\.\d+))\s*$/;

const templates = [
  { name: '회의록 정리', description: '회의 메모에서 결정 사항과 후속 작업을 정리합니다.', prompt: '회의 메모를 읽고 핵심 논의, 결정 사항, 담당자와 기한이 포함된 후속 작업을 정리하세요.', tools: ['text', 'plan'], icon: FileText, example: '신규 기능 회의\n결정: 다음 배포에 검색 필터 추가\n후속 작업: 지민이 금요일까지 시안 작성' },
  { name: '코드 리뷰', description: '공유한 코드를 검토하는 흐름으로 시작합니다.', prompt: '코드의 정확성, 예외 처리, 가독성을 검토하고 수정 우선순위를 제시하세요.', tools: ['code'], icon: Braces, example: 'function divide(a, b) { return a / b; }' },
  { name: '업무 계획', description: '목표를 실행 가능한 작업 목록으로 나눕니다.', prompt: '업무 목표를 분석하고 작업 목록, 우선순위, 검토 항목을 제시하세요.', tools: ['plan'], icon: ListChecks, example: '온보딩 문서 초안 작성\n팀 피드백 수집\n최종본 공유' },
];

const starter = { id: 'starter', name: '워크스페이스 어시스턴트', description: '일반 작업을 위한 기본 에이전트', prompt: '사용자의 요청을 한국어로 간결하게 정리하세요.', tools: ['text'] };
const blank = { name: '', description: '', prompt: '', tools: [] };
const storageKey = 'agent-workspace.agents.v1';
const toolStorageKey = 'agent-workspace.custom-tools.v1';

function readCustomTools() {
  try {
    const saved = JSON.parse(localStorage.getItem(toolStorageKey));
    return Array.isArray(saved) ? saved : [];
  } catch {
    return [];
  }
}

function readAgents() {
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey));
    return Array.isArray(saved) && saved.length ? saved : [starter];
  } catch {
    return [starter];
  }
}

function currentPage() {
  const route = location.hash.slice(1);
  return navigation.some(({ id }) => id === route) ? route : 'chat';
}

function draftFrom(description) {
  const trimmed = description.trim();
  const selected = [...harnessTools, ...demoTools].filter(({ pattern }) => pattern.test(trimmed));
  const title = trimmed.split(/[.!?\n]/)[0]
    .replace(/\s*(에이전트(?:를)?\s*)?(만들어\s*줘|만들어\s*주세요)\s*$/, '').trim();
  return {
    name: (title || '새 에이전트').slice(0, 32),
    description: trimmed,
    prompt: '다음 역할에 따라 사용자의 요청을 처리하세요.\n\n' + trimmed +
      '\n\n정보가 부족하면 확인 질문을 하고, 결과를 한국어로 간결하게 정리하세요.',
    tools: selected.length ? selected.map(({ id }) => id) : ['text'],
  };
}

function runDemo(agent, input, customTools) {
  const arithmetic = harnessTools.filter(({ id }) => agent.tools.includes(id));
  const expression = input.match(arithmeticExpression);
  if (expression && arithmetic.length) {
    const [, left, symbol, right] = expression;
    const operator = symbol === '×' ? '*' : symbol === '−' ? '-' : symbol;
    const tool = arithmetic.find(candidate => candidate.operator === operator);
    if (!tool) return '이 에이전트에 해당 연산 도구가 설정되어 있지 않습니다.';
    const result = tool.calculate(Number(left), Number(right));
    return Number.isFinite(result) ? tool.id + ' · ' + left + ' ' + symbol + ' ' + right + ' = ' + result : '계산 결과가 지원 범위를 벗어났습니다.';
  }
  const items = input.split('\n').map(line => line.replace(/^\s*[-*]\s+/, '').trim()).filter(Boolean);
  const sections = agent.tools.filter(id => demoTools.some(tool => tool.id === id)).map(id => {
    if (id === 'text') return '텍스트 정리 · 입력 앞부분\n' + items.slice(0, 4).map(item => '• ' + item.slice(0, 160)).join('\n');
    if (id === 'plan') return '체크리스트 · 입력 줄을 작업으로 변환\n' + items.slice(0, 8).map(item => '☐ ' + item.slice(0, 160)).join('\n');
    return '코드 검토 · 수동 확인 항목\n• 입력과 반환값의 계약 확인\n• 잘못된 입력과 경계값 확인\n• 실패 경로와 호출부 확인';
  });
  if (arithmetic.length) sections.push('산술 도구 입력 예시\n' + arithmetic.map(({ operator }) => '12 ' + operator + ' 3').join('\n'));
  const custom = customTools.filter(({ id }) => agent.tools.includes(id));
  if (custom.length) sections.push('실행 미연결 도구: ' + custom.map(({ label }) => label).join(', '));
  return (sections.length ? sections.join('\n\n') : '도구가 선택되어 있지 않습니다.') +
    '\n\n설정된 시스템 프롬프트\n' + agent.prompt;
}

function AgentEditor({ draft, onSave, submitLabel, customTools }) {
  const [form, setForm] = useState(draft);
  const set = (key, value) => setForm(previous => ({ ...previous, [key]: value }));
  return <form className="panel editor" onSubmit={event => {
    event.preventDefault();
    if (form.name.trim() && form.prompt.trim()) {
      onSave({ ...form, name: form.name.trim(), description: form.description.trim(), prompt: form.prompt.trim() });
    }
  }}>
    <div className="form-grid">
      <label>이름<input required maxLength={60} value={form.name} onChange={event => set('name', event.target.value)} /></label>
      <label>설명<input maxLength={1200} value={form.description} onChange={event => set('description', event.target.value)} /></label>
    </div>
    <label>시스템 프롬프트<textarea required maxLength={6000} rows={6} value={form.prompt} onChange={event => set('prompt', event.target.value)} /></label>
    {[['등록된 도구', [...harnessTools, ...customTools]], ['데모 도구', demoTools]].map(([title, catalog]) => <fieldset key={title}>
      <legend>{title}</legend><div className="tool-row">
        {catalog.map(({ id, label, description, icon: Icon = Wrench }) => <label key={id} className={'tool ' + (form.tools.includes(id) ? 'checked' : '')}>
          <input type="checkbox" checked={form.tools.includes(id)} onChange={event =>
            set('tools', event.target.checked ? [...form.tools, id] : form.tools.filter(tool => tool !== id))
          } /><Icon size={16} /><span>{label}{description && <><code> {id}</code><small>{description}</small></>}</span>
        </label>)}
      </div>
    </fieldset>)}
    <div className="actions"><button className="primary" disabled={!form.name.trim() || !form.prompt.trim()}><Check size={16} />{submitLabel}</button></div>
  </form>;
}

function ToolCatalog({ customTools, onAdd }) {
  const [showForm, setShowForm] = useState(false);
  const [feedback, setFeedback] = useState('');

  function submit(event) {
    event.preventDefault();
    const fields = new FormData(event.currentTarget);
    const tool = {
      label: fields.get('label').trim(),
      id: fields.get('id').trim().toLowerCase(),
      description: fields.get('description').trim(),
    };
    if (!tool.label || !tool.description || !/^[a-z][a-z0-9_-]{0,39}$/.test(tool.id)) {
      setFeedback('이름과 설명을 입력하고, ID는 영문자로 시작해 영문자, 숫자, 밑줄, 하이픈을 사용하세요.');
      return;
    }
    if ([...harnessTools, ...demoTools, ...customTools].some(({ id }) => id === tool.id)) {
      setFeedback('이미 사용 중인 도구 ID입니다.');
      return;
    }
    if (!onAdd(tool)) {
      setFeedback('브라우저에 도구를 저장할 수 없습니다.');
      return;
    }
    setShowForm(false);
    setFeedback(tool.label + ' 도구를 추가했습니다.');
  }

  return <>
    {!showForm && <div className="actions"><button className="primary" onClick={() => {
      setFeedback('');
      setShowForm(true);
    }}><Plus size={16} />도구 추가</button></div>}
    {showForm && <form className="panel editor" onSubmit={submit}>
      <div className="form-grid">
        <label>도구 이름<input name="label" maxLength={60} required /></label>
        <label>도구 ID<input name="id" maxLength={40} required placeholder="예: my_tool" autoCapitalize="none" /></label>
      </div>
      <label>설명<textarea name="description" maxLength={400} rows={3} required /></label>
      <p className="muted">추가한 도구는 에이전트 설정에서 선택할 수 있습니다. 실제 실행은 백엔드 연결이 필요합니다.</p>
      <div className="actions"><button className="quiet" type="button" onClick={() => {
        setFeedback('');
        setShowForm(false);
      }}>취소</button><button className="primary"><Check size={16} />도구 저장</button></div>
    </form>}
    {feedback && <p className="notice" role="status">{feedback}</p>}
    <ul className="tool-catalog">{[...harnessTools, ...customTools].map(({ id, label, description, icon: Icon = Wrench }) =>
      <li className="panel catalog-tool" key={id}>
        <span className="hero-icon small"><Icon size={21} aria-hidden="true" /></span>
        <div><h2>{label} <code>{id}</code></h2><p>{description}</p></div>
      </li>
    )}</ul>
  </>;
}

function App() {
  const [page, setPage] = useState(currentPage);
  const [agents, setAgents] = useState(readAgents);
  const [customTools, setCustomTools] = useState(readCustomTools);
  const [activeId, setActiveId] = useState(() => agents[0].id);
  const [editingId, setEditingId] = useState(() => agents[0].id);
  const [draft, setDraft] = useState(null);
  const [draftVersion, setDraftVersion] = useState(0);
  const [brief, setBrief] = useState('');
  const [input, setInput] = useState('');
  const [messages, setMessages] = useState({});
  const [query, setQuery] = useState('');
  const [notice, setNotice] = useState('');
  const chatEnd = useRef(null);
  const active = agents.find(({ id }) => id === activeId) ?? agents[0];
  const editing = agents.find(({ id }) => id === editingId) ?? agents[0];
  const route = navigation.find(({ id }) => id === page);
  const conversation = messages[active.id];
  const filtered = agents.filter(({ name, description }) => (name + ' ' + description).toLowerCase().includes(query.trim().toLowerCase()));

  useEffect(() => {
    const updatePage = () => { setPage(currentPage()); setNotice(''); };
    addEventListener('hashchange', updatePage);
    return () => removeEventListener('hashchange', updatePage);
  }, []);

  useEffect(() => {
    if (page === 'chat' && conversation?.length) chatEnd.current?.scrollIntoView({ block: 'nearest' });
  }, [page, conversation]);

  function openDraft(value) {
    setDraft(value);
    setDraftVersion(version => version + 1);
  }

  function saveAgent(value) {
    const record = { ...value, id: value.id ?? crypto.randomUUID() };
    const updated = record.id === value.id
      ? agents.map(agent => agent.id === record.id ? record : agent)
      : [...agents, record];
    try {
      localStorage.setItem(storageKey, JSON.stringify(updated));
    } catch {
      return false;
    }
    setAgents(updated);
    setEditingId(record.id);
    if (page === 'create') {
      setDraft(null);
      setBrief('');
      location.hash = 'agents';
    }
    return true;
  }

  function useAgent(id, example = '') {
    setActiveId(id);
    setInput(example);
    location.hash = 'chat';
  }

  function send(event) {
    event.preventDefault();
    if (!input.trim()) return;
    const content = input.trim();
    const reply = runDemo(active, content, customTools);
    setMessages(previous => ({ ...previous, [active.id]: [
      ...(previous[active.id] ?? []), { role: 'user', content }, { role: 'agent', content: reply, name: active.name },
    ] }));
    setInput('');
  }

  function addTool(tool) {
    const registered = [...customTools, tool];
    try {
      localStorage.setItem(toolStorageKey, JSON.stringify(registered));
    } catch {
      return false;
    }
    setCustomTools(registered);
    return true;
  }

  const storageError = '브라우저에 설정을 저장할 수 없습니다.';

  return <div className="shell" style={{ '--accent': route.color }}>
    <aside className="sidebar">
      <div className="sidebar-heading"><span className="sidebar-mark"><Workflow size={19} /></span><strong>워크스페이스</strong></div>
      <nav aria-label="워크스페이스 메뉴">{navigation.map(({ id, name, detail, icon: Icon, color }) => <a key={id} href={'#' + id} aria-current={page === id ? 'page' : undefined} style={{ '--item-accent': color }}>
        <span className="nav-icon"><Icon size={19} /></span><span><strong>{name}</strong><small>{detail}</small></span><ChevronRight size={15} className="nav-chevron" />
      </a>)}</nav>
      <div className="sidebar-bottom"><span className="status-dot" />프론트엔드 데모</div>
    </aside>
    <main className={'workspace ' + (page === 'chat' ? 'chat-workspace' : '')}>
      <header className="topbar"><span className="mark"><Workflow size={19} /></span><strong>Agent Workspace</strong><span className="top-divider" /><span className="breadcrumb">{route.name}</span></header>
      {page === 'chat' ? <>
        <div className="chat-toolbar">
          <label className="agent-picker"><Bot size={17} /><select aria-label="작업 에이전트" value={active.id} onChange={event => { setActiveId(event.target.value); setInput(''); }}>
            {agents.map(agent => <option key={agent.id} value={agent.id}>{agent.name}</option>)}
          </select></label>
          <button className="quiet" onClick={() => {
            setMessages(previous => ({ ...previous, [active.id]: [] }));
            setInput('');
          }}><Plus size={17} />새 대화</button>
        </div>
        <div className="chat-body">
          {conversation?.length ? <div className="thread" aria-live="polite">
            {conversation.map((message, index) => <article className={'message ' + message.role} key={index}>
              <span className="message-label">{message.role === 'user' ? '나' : message.name + ' · 데모'}</span>
              <div>{message.content}</div>
            </article>)}<div ref={chatEnd} />
          </div> : <div className="welcome">
            <div className="hero-icon"><MessageSquare size={35} strokeWidth={1.5} /></div>
            <h1>무엇을 함께 작업할까요?</h1>
            <p>에이전트를 선택하고 자연어로 작업을 요청하세요.</p>
            <div className="quick-grid">
              {templates.map(({ name, icon: Icon, example }) => <button key={name} className="quick" onClick={() => setInput(example)}><Icon size={17} /><span>{name} 예시</span><ChevronRight size={15} /></button>)}
            </div>
          </div>}
        </div>
        <div className="compose-area"><form className="composer" onSubmit={send}>
          <textarea aria-label="메시지" rows={2} maxLength={4000} value={input} onChange={event => setInput(event.target.value)}
            placeholder="에이전트에게 작업을 요청하세요"
            onKeyDown={event => {
              if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229) {
                event.preventDefault();
                event.currentTarget.form.requestSubmit();
              }
            }} />
          <button className="send" aria-label="메시지 보내기" disabled={!input.trim()}><ArrowUp size={20} /></button>
        </form><p className="footnote">로컬 데모 · 응답은 선택한 도구의 예시 동작으로 생성됩니다.</p></div>
      </> : <div className="page">
        <div className="page-heading"><div className="hero-icon small"><route.icon size={25} /></div><h1>{route.name}</h1>
          {page !== 'tools' && <p>{{ create: '필요한 역할을 설명하고 생성된 초안을 다듬으세요.', agents: '저장한 에이전트의 도구와 시스템 프롬프트를 관리하세요.', templates: '자주 하는 작업을 미리 구성된 시나리오로 시작하세요.' }[page]}</p>}
        </div>
        {notice && <p role="status" className="notice">{notice}</p>}
        {page === 'create' && <>
          <form className="panel editor" onSubmit={event => { event.preventDefault(); if (brief.trim()) openDraft(draftFrom(brief)); }}>
            <label>어떤 에이전트가 필요한가요?<textarea rows={4} value={brief} maxLength={1200} onChange={event => setBrief(event.target.value)} placeholder="예: 회의 메모를 요약하고 후속 작업을 체크리스트로 정리하는 에이전트를 만들어 줘." /></label>
            <div className="actions"><button type="button" className="quiet" onClick={() => openDraft(blank)}>직접 입력</button><button className="primary" disabled={!brief.trim()}><Sparkles size={16} />초안 생성</button></div>
          </form>
          {draft && <><h2>에이전트 설정</h2><AgentEditor key={draftVersion} draft={draft} customTools={customTools} onSave={value => {
            if (!saveAgent(value)) setNotice(storageError);
          }} submitLabel="에이전트 저장" /></>}
        </>}
        {page === 'agents' && <>
          <label className="search"><Search size={18} /><input aria-label="에이전트 검색" placeholder="에이전트 검색" value={query} onChange={event => setQuery(event.target.value)} /></label>
          <div className="agent-list">{filtered.map(agent => <button className={'panel agent-card ' + (agent.id === editing.id ? 'selected' : '')} key={agent.id} onClick={() => { setEditingId(agent.id); setNotice(''); }}>
            <Bot size={20} /><span><strong>{agent.name}</strong><small>{agent.description || '설명 없음'}</small></span><ChevronRight size={16} />
          </button>)}{!filtered.length && <p className="muted">검색 결과가 없습니다.</p>}</div>
          {filtered.some(agent => agent.id === editing.id) && <>
            <div className="section-heading"><h2>에이전트 설정</h2><button className="quiet" onClick={() => useAgent(editing.id)}><MessageSquare size={16} />이 에이전트로 작업</button></div>
            <AgentEditor key={editing.id} draft={editing} customTools={customTools} onSave={value => setNotice(saveAgent(value) ? '변경사항을 저장했습니다.' : storageError)} submitLabel="변경사항 저장" />
          </>}
        </>}
        {page === 'tools' && <ToolCatalog customTools={customTools} onAdd={addTool} />}
        {page === 'templates' && <div className="template-grid">{templates.map(({ icon: Icon, example, ...template }) => <article className="panel template" key={template.name}>
          <div className="hero-icon small"><Icon size={21} /></div><h2>{template.name}</h2><p>{template.description}</p>
          <div className="tool-tags">{demoTools.filter(({ id }) => template.tools.includes(id)).map(({ id, label }) => <span key={id}>{label}</span>)}</div>
          <button className="primary" onClick={() => { setBrief(''); openDraft(template); location.hash = 'create'; }}><Plus size={16} />템플릿 사용</button>
        </article>)}</div>}
      </div>}
    </main>
  </div>;
}

createRoot(document.getElementById('root')).render(<App />);
