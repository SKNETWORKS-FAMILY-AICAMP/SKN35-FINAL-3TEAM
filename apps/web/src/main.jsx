import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import {
  ArrowUp, Bot, Calculator, Check, Code2, FileText, LayoutTemplate, ListChecks,
  MessageSquare, Plus, RefreshCw, Settings2, Sparkles, Workflow, Wrench,
} from 'lucide-react';
import { request } from './api.js';
import './style.css';

const navigation = [
  { id: 'chat', name: '작업 채팅', icon: MessageSquare, color: '#8ab6ff' },
  { id: 'create', name: '에이전트 만들기', icon: Sparkles, color: '#c4a7fa' },
  { id: 'agents', name: '내 에이전트', icon: Settings2, color: '#76d8b1' },
  { id: 'templates', name: '시나리오 템플릿', icon: LayoutTemplate, color: '#f5b484' },
  { id: 'tools', name: '도구 목록', icon: Wrench, color: '#80cfde' },
];

const templates = [
  { name: '계산 도우미', description: '등록된 산술 도구로 계산합니다.', system_prompt: '계산 요청에는 등록된 도구를 사용하고 결과를 한국어로 간결하게 답하세요.', tools: ['add', 'subtract', 'multiply'], icon: Calculator, example: '123 * 45' },
  { name: '회의록 정리', description: '회의 메모에서 결정 사항과 후속 작업을 정리합니다.', system_prompt: '회의 메모에서 논의 요점, 결정 사항, 담당자와 기한이 포함된 후속 작업을 정리하세요.', tools: [], icon: FileText, example: '회의 메모를 정리해 줘. 다음 배포에 검색 필터를 추가하고 지민이 금요일까지 시안을 작성해.' },
  { name: '코드 리뷰', description: '입력으로 받은 코드를 검토합니다.', system_prompt: '공유된 코드의 정확성, 예외 처리, 가독성을 검토하고 수정 우선순위를 제시하세요.', tools: [], icon: Code2 },
  { name: '업무 계획', description: '목표를 실행 가능한 작업 목록으로 나눕니다.', system_prompt: '업무 목표를 분석하고 작업 목록, 우선순위, 검토 항목을 한국어로 제시하세요.', tools: [], icon: ListChecks, example: '신규 팀원의 첫 주 온보딩 체크리스트를 만들어 줘.' },
];

const defaultAgent = { agent_id: '', name: '기본 에이전트', status: 'active' };
const blank = { name: '', description: '', system_prompt: '', tools: [] };
const users = ['kim', 'lee', 'park', 'admin'];

function currentPage() {
  const hash = location.hash.slice(1);
  return navigation.some(({ id }) => id === hash) ? hash : 'chat';
}

function makeDraft(brief, tools) {
  const patterns = { add: /덧셈|더하|더해|\badd\b/i, subtract: /뺄셈|빼기|빼줘|\bsubtract\b/i, multiply: /곱셈|곱하|곱해|\bmultiply\b/i };
  return {
    name: brief.split(/[.!?\n]/)[0].slice(0, 34),
    description: brief,
    system_prompt: '다음 역할에 따라 요청을 처리하세요.\n\n' + brief + '\n\n필요한 도구를 사용하고 결과를 한국어로 답하세요.',
    tools: tools.filter(({ name }) => patterns[name]?.test(brief)).map(({ name }) => name),
  };
}

function AgentEditor({ agent, tools, onSave, saving }) {
  const [form, setForm] = useState(agent);
  const editable = agent.can_edit !== false;
  const update = (key, value) => setForm(previous => ({ ...previous, [key]: value }));

  return <form className="panel editor" onSubmit={event => {
    event.preventDefault();
    if (editable && !saving) onSave({ ...form, name: form.name.trim(), description: form.description.trim(), system_prompt: form.system_prompt.trim() });
  }}>
    <fieldset disabled={!editable || saving} className="editor-fields">
      <div className="form-grid">
        <label>이름<input value={form.name} required maxLength={100} onChange={event => update('name', event.target.value)} /></label>
        <label>설명<input value={form.description} maxLength={1000} onChange={event => update('description', event.target.value)} /></label>
      </div>
      <label>시스템 프롬프트<textarea value={form.system_prompt} required maxLength={4000} rows={6} onChange={event => update('system_prompt', event.target.value)} /></label>
      <fieldset><legend>실행 도구</legend><div className="tool-row">
        {tools.map(tool => <label key={tool.name} className={'tool ' + (form.tools.includes(tool.name) ? 'checked' : '')}>
          <input type="checkbox" checked={form.tools.includes(tool.name)} disabled={form.tools.length >= 20 && !form.tools.includes(tool.name)}
            onChange={event => update('tools', event.target.checked ? [...form.tools, tool.name] : form.tools.filter(name => name !== tool.name))} />
          <span>{tool.display_name} <code>{tool.name}</code></span>
        </label>)}
        {!tools.length && <p className="muted">선택할 수 있는 도구가 없습니다.</p>}
      </div></fieldset>
    </fieldset>
    {editable && <div className="actions"><button className="primary" disabled={saving || !form.name.trim() || !form.system_prompt.trim()}>
      <Check size={16} />{saving ? '저장 중' : '에이전트 저장'}
    </button></div>}
  </form>;
}

function ToolCatalog({ tools }) {
  const storageKey = 'agent-workspace.custom-tools.v1';
  const [drafts, setDrafts] = useState(() => {
    try {
      const value = JSON.parse(localStorage.getItem(storageKey));
      return Array.isArray(value) ? value : [];
    } catch {
      return [];
    }
  });
  const [error, setError] = useState('');

  function addDraft(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const fields = new FormData(form);
    const label = fields.get('label').trim();
    const id = fields.get('id').trim().toLowerCase();
    const description = fields.get('description').trim();
    if (!label || !description || !/^[a-z][a-z0-9_-]{0,39}$/.test(id)) {
      setError('이름과 설명을 입력하고, ID는 영문자로 시작하는 40자 이내의 영문자·숫자·밑줄·하이픈을 사용하세요.');
      return;
    }
    if (tools.some(tool => tool.name === id) || drafts.some(draft => draft.id === id)) {
      setError('이미 사용 중인 도구 ID입니다.');
      return;
    }
    const next = [...drafts, { id, label, description }];
    try {
      localStorage.setItem(storageKey, JSON.stringify(next));
    } catch {
      setError('브라우저에 초안을 저장할 수 없습니다.');
      return;
    }
    setDrafts(next);
    setError('');
    form.reset();
  }

  return <><ul className="tool-catalog">{tools.map(tool => <li className="panel catalog-tool" key={tool.name}>
    <span className="hero-icon small"><Wrench size={19} aria-hidden="true" /></span>
    <div><h2>{tool.display_name} <code>{tool.name}</code></h2><p>{tool.description}</p></div>
  </li>)}</ul>
    {!tools.length && <p className="muted">서버에 등록된 도구가 없습니다.</p>}
    <details className="panel tool-drafts"><summary>도구 추가 초안</summary>
      <form className="editor" onSubmit={addDraft}>
        <p className="muted">이 브라우저에 초안을 저장합니다. 실행 도구는 서버 레지스트리에 등록합니다.</p>
        <div className="form-grid">
          <label>도구 이름<input name="label" maxLength={60} required /></label>
          <label>도구 ID<input name="id" maxLength={40} required /></label>
        </div>
        <label>설명<textarea name="description" maxLength={400} rows={3} required /></label>
        {error && <p role="alert">{error}</p>}
        <div className="actions"><button className="primary"><Plus size={16} />초안 저장</button></div>
      </form>
      {drafts.length > 0 && <ul className="tool-catalog draft-list" aria-live="polite">{drafts.map(({ id, label, description }) =>
        <li className="panel catalog-tool" key={id}><div><h2>{label} <code>{id}</code></h2><p>{description}</p></div></li>
      )}</ul>}
    </details>
  </>;
}

function ToolEvents({ events, denial }) {
  return <>
    {events?.length > 0 && <details className="activity"><summary>도구 실행 · {events.length}건</summary>
      <ul>{events.map((event, index) => <li key={event.call_id + index}>
        <strong>{event.display_name}</strong> <code>{event.tool_name}</code>
        <p>{event.result || (event.ok ? '완료' : '실패')}</p>
      </li>)}</ul>
    </details>}
    {denial && <p className="denial">{denial.display_name} · {denial.reason || { deny: '권한 거부', rejected: '승인 거절', timeout: '승인 시간 초과' }[denial.kind]}</p>}
  </>;
}

function Workspace({ user, setUser }) {
  const [page, setPage] = useState(currentPage);
  const [catalog, setCatalog] = useState({ tools: [], agents: [], state: 'loading', error: '' });
  const [refresh, setRefresh] = useState(0);
  const [activeId, setActiveId] = useState('');
  const [editingId, setEditingId] = useState('');
  const [brief, setBrief] = useState('');
  const [draft, setDraft] = useState(null);
  const [draftVersion, setDraftVersion] = useState(0);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState('');
  const [query, setQuery] = useState('');
  const [input, setInput] = useState('');
  const [chats, setChats] = useState([]);
  const [chatId, setChatId] = useState(null);
  const [sending, setSending] = useState(false);
  const [chatError, setChatError] = useState(null);
  const [approvals, setApprovals] = useState([]);
  const [approvalError, setApprovalError] = useState('');
  const [answering, setAnswering] = useState(null);
  const chatEnd = useRef(null);
  const route = navigation.find(({ id }) => id === page);
  const { agents, tools, state, error } = catalog;
  const current = chats.find(chat => chat.id === chatId);
  const available = [defaultAgent, ...agents.filter(agent => agent.status === 'active')];
  const active = available.find(agent => agent.agent_id === activeId) ?? defaultAgent;
  const filtered = agents.filter(agent => (agent.name + ' ' + agent.description).toLowerCase().includes(query.trim().toLowerCase()));
  const editing = filtered.find(agent => agent.agent_id === editingId) ?? filtered[0];
  const locked = sending || saving || Boolean(answering);

  useEffect(() => {
    const update = () => { setPage(currentPage()); setNotice(''); };
    addEventListener('hashchange', update);
    return () => removeEventListener('hashchange', update);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const options = { signal: controller.signal };
    Promise.all([request(user, '/tools', options), request(user, '/agents', options)])
      .then(([tools, agents]) => {
        if (!controller.signal.aborted) setCatalog({ tools, agents, state: 'ready', error: '' });
      })
      .catch(error => {
        if (!controller.signal.aborted) setCatalog(previous => ({ ...previous, state: 'error', error: error.message }));
      });
    return () => controller.abort();
  }, [user, refresh]);

  useEffect(() => {
    if (page === 'chat' && current?.messages.length) chatEnd.current?.scrollIntoView({ block: 'nearest' });
  }, [page, current, approvals]);

  useEffect(() => {
    if (!sending) return;
    const controller = new AbortController();
    let timer;
    let stopped = false;
    async function poll() {
      try {
        const pending = await request(user, '/approvals', { signal: controller.signal });
        if (!stopped) setApprovals(pending);
      } catch {
        // The chat request surfaces connection failures.
      } finally {
        if (!stopped) timer = setTimeout(poll, 1200);
      }
    }
    poll();
    return () => {
      stopped = true;
      controller.abort();
      clearTimeout(timer);
    };
  }, [user, sending]);

  function openDraft(value) {
    setDraft(value);
    setDraftVersion(version => version + 1);
    setNotice('');
  }

  async function saveAgent(value) {
    const { agent_id, name, description, system_prompt, tools } = value;
    setSaving(true);
    setNotice('');
    try {
      const saved = await request(user, agent_id ? '/agents/' + encodeURIComponent(agent_id) : '/agents', {
        method: agent_id ? 'PATCH' : 'POST', body: { name, description, system_prompt, tools },
      });
      setCatalog(previous => ({ ...previous, agents: agent_id
        ? previous.agents.map(agent => agent.agent_id === agent_id ? saved : agent)
        : [...previous.agents, saved] }));
      setEditingId(saved.agent_id);
      setQuery('');
      if (agent_id) setNotice('에이전트 설정을 저장했습니다.');
      else {
        setDraft(null);
        setBrief('');
        location.hash = 'agents';
      }
    } catch (error) {
      setNotice(error.message);
    } finally {
      setSaving(false);
    }
  }

  function newChat(agentId = active.agent_id) {
    if (sending) return;
    setChatId(null);
    setActiveId(agentId);
    setInput('');
    setChatError(null);
    location.hash = 'chat';
  }

  function openChat(chat) {
    setChatId(chat.id);
    setActiveId(chat.agentId);
    setInput('');
    setChatError(null);
    location.hash = 'chat';
  }

  async function send(event) {
    event.preventDefault();
    const content = input.trim();
    if (!content || sending || state !== 'ready' || chatError?.stale) return;
    const id = current?.id ?? crypto.randomUUID();
    const agent = current ? { agent_id: current.agentId, name: current.agentName } : active;
    const userMessage = { role: 'user', content };
    const entry = current ?? { id, agentId: agent.agent_id, agentName: agent.name, title: content.slice(0, 48), messages: [] };
    setChats(previous => [ { ...entry, messages: [...entry.messages, userMessage] }, ...previous.filter(chat => chat.id !== id) ]);
    setChatId(id);
    setInput('');
    setSending(true);
    setChatError(null);
    setApprovalError('');
    try {
      const body = current?.sessionId ? { content, session_id: current.sessionId } : { content, agent_id: agent.agent_id || null };
      const result = await request(user, '/chat/messages', { method: 'POST', body });
      setChats(previous => previous.map(chat => chat.id === id ? {
        ...chat, sessionId: result.session_id, messages: [...chat.messages, {
          role: 'assistant', content: result.message, events: result.tool_events, denial: result.denial,
        }],
      } : chat));
    } catch (error) {
      setChats(previous => previous.map(chat => chat.id === id ? { ...chat, messages: chat.messages.slice(0, -1) } : chat));
      setInput(content);
      setChatError({ message: error.message, stale: Boolean(current?.sessionId && (error.status === 404 || error.status === 409)) });
    } finally {
      setSending(false);
      setApprovals([]);
    }
  }

  async function answerApproval(callId, approved) {
    setAnswering(callId);
    setApprovalError('');
    try {
      await request(user, '/approvals/' + encodeURIComponent(callId), { method: 'POST', body: { approved } });
      setApprovals(previous => previous.filter(item => item.call_id !== callId));
    } catch (error) {
      setApprovalError(error.message);
    } finally {
      setAnswering(null);
    }
  }

  return <div className="shell" style={{ '--accent': route.color }}>
    <aside className="sidebar">
      <div className="sidebar-heading"><span className="sidebar-mark"><Workflow size={19} /></span><strong>Agent Workspace</strong></div>
      <button className="new-chat" onClick={() => newChat()} disabled={sending}><Plus size={18} /><span>새 대화</span></button>
      <nav aria-label="워크스페이스 메뉴">{navigation.map(({ id, name, icon: Icon, color }) => <a
        key={id} href={'#' + id} aria-current={page === id ? 'page' : undefined} style={{ '--item-accent': color }}>
        <span className="nav-icon"><Icon size={18} /></span><strong>{name}</strong>
      </a>)}</nav>
      {chats.some(chat => chat.messages.length) && <div className="sidebar-history">
        <h2>최근 대화</h2>
        {chats.filter(chat => chat.messages.length).map(chat => <button key={chat.id} title={chat.title} disabled={sending}
          className={chat.id === chatId && page === 'chat' ? 'selected' : ''} onClick={() => openChat(chat)}>
          <MessageSquare size={15} /><span>{chat.title}</span>
        </button>)}
      </div>}
      <div className="sidebar-bottom"><label><span>개발 사용자</span><select aria-label="개발 사용자" value={user} disabled={locked} onChange={event => setUser(event.target.value)}>
        {users.map(name => <option value={name} key={name}>{name}</option>)}
      </select></label></div>
    </aside>
    <main className={'workspace ' + (page === 'chat' ? 'chat-workspace' : '')}>
      <header className="topbar">
        {page === 'chat' ? <label className="agent-picker"><Bot size={18} /><select aria-label="작업 에이전트"
          value={current?.agentId ?? active.agent_id} disabled={sending} onChange={event => newChat(event.target.value)}>
          {available.map(agent => <option value={agent.agent_id} key={agent.agent_id}>{agent.name}</option>)}
          {current && !available.some(agent => agent.agent_id === current.agentId) && <option value={current.agentId}>{current.agentName}</option>}
        </select></label> : <strong>{route.name}</strong>}
        <div className="connection"><span className={'status-dot ' + state} />{state === 'ready' ? 'API 연결됨' : state === 'error' ? 'API 연결 오류' : '연결 중'}
          {state === 'error' && <button className="icon-button" aria-label="API 다시 연결" onClick={() => {
            setCatalog(previous => ({ ...previous, state: 'loading', error: '' }));
            setRefresh(value => value + 1);
          }}><RefreshCw size={16} /></button>}
        </div>
      </header>
      {page === 'chat' ? <>
        <div className="chat-body">
          {error && <p className="notice chat-notice" role="alert">{error}</p>}
          {current?.messages.length ? <div className="thread" aria-live="polite">
            {current.messages.map((message, index) => <article className={'message ' + message.role} key={index}>
              <span className="message-label">{message.role === 'user' ? '나' : current.agentName}</span>
              <div>{message.content}</div><ToolEvents events={message.events} denial={message.denial} />
            </article>)}
            {sending && <p className="muted" role="status">응답을 기다리는 중…</p>}
            {sending && approvals.filter(item => !current.sessionId || item.session_id === current.sessionId).map(item => <article className="panel editor approval" key={item.call_id}>
              <h2>{item.display_name} 실행 승인</h2><p>{item.reason}</p>
              {!current.sessionId && <small>대기 요청 세션 {item.session_id}</small>}
              <pre>{JSON.stringify(item.arguments, null, 2)}</pre>
              <div className="actions">
                <button className="quiet" disabled={Boolean(answering)} onClick={() => answerApproval(item.call_id, false)}>거절</button>
                <button className="primary" disabled={Boolean(answering)} onClick={() => answerApproval(item.call_id, true)}>승인</button>
              </div>
            </article>)}
            {approvalError && <p role="alert">{approvalError}</p>}
            <div ref={chatEnd} />
          </div> : <div className="welcome">
            <div className="hero-icon"><Workflow size={32} strokeWidth={1.65} /></div>
            <h1>어떤 작업부터 시작할까요?</h1>
            <p>에이전트를 선택하고 작업을 요청하세요.</p>
            <div className="quick-grid">{templates.filter(template => template.example).map(({ name, icon: Icon, example }) =>
              <button key={name} className="quick" onClick={() => setInput(example)}><Icon size={18} /><span>{name}</span></button>)}</div>
          </div>}
        </div>
        <div className="compose-area">
          {chatError && <p className="notice compose-notice" role="alert">{chatError.message}{chatError.stale && ' 왼쪽에서 새 대화를 시작하세요.'}</p>}
          <form className="composer" onSubmit={send}>
            <textarea aria-label="메시지" rows={2} maxLength={4000} value={input} onChange={event => setInput(event.target.value)}
              readOnly={sending} placeholder="에이전트에게 메시지 보내기"
              onKeyDown={event => {
                if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229) {
                  event.preventDefault();
                  event.currentTarget.form.requestSubmit();
                }
              }} />
            <button className="send" aria-label="메시지 보내기" disabled={!input.trim() || sending || state !== 'ready' || chatError?.stale}><ArrowUp size={20} /></button>
          </form>
          <p className="footnote">Enter로 전송 · Shift+Enter로 줄바꿈</p>
        </div>
      </> : <div className="page">
        <div className="page-heading"><div className="hero-icon small"><route.icon size={23} /></div><h1>{route.name}</h1></div>
        {error && <p className="notice" role="alert">{error}</p>}
        {notice && <p className="notice" role="status">{notice}</p>}
        {page === 'create' && <>
          <form className="panel editor" onSubmit={event => { event.preventDefault(); if (brief.trim()) openDraft(makeDraft(brief.trim(), tools)); }}>
            <label>어떤 에이전트가 필요한가요?<textarea rows={4} maxLength={1000} value={brief} onChange={event => setBrief(event.target.value)}
              placeholder="예: 곱셈을 담당하고 결과를 짧게 알려주는 에이전트" /></label>
            <div className="actions">
              <button className="quiet" type="button" disabled={saving || state !== 'ready'} onClick={() => openDraft(blank)}>직접 입력</button>
              <button className="primary" disabled={saving || state !== 'ready' || !brief.trim()}><Sparkles size={16} />초안 생성</button>
            </div>
          </form>
          {draft && state === 'ready' && <AgentEditor key={draftVersion} agent={draft} tools={tools} onSave={saveAgent} saving={saving} />}
        </>}
        {page === 'agents' && state === 'ready' && <>
          <label className="search"><input aria-label="에이전트 검색" placeholder="에이전트 검색" value={query} onChange={event => setQuery(event.target.value)} disabled={saving} /></label>
          <div className="agent-list">{filtered.map(agent => <button key={agent.agent_id} disabled={saving}
            className={'panel agent-card ' + (agent.agent_id === editing?.agent_id ? 'selected' : '')}
            onClick={() => { setEditingId(agent.agent_id); setNotice(''); }}>
            <Bot size={19} /><span><strong>{agent.name}</strong><small>{agent.description || agent.scope}</small></span>
          </button>)}</div>
          {!filtered.length && <p className="muted">{query ? '검색 결과가 없습니다.' : '저장된 에이전트가 없습니다.'}</p>}
          {editing && <>
            <div className="section-heading"><h2>에이전트 설정</h2>
              <button className="quiet" disabled={sending || saving || editing.status !== 'active'} onClick={() => newChat(editing.agent_id)}>
                <MessageSquare size={16} />{editing.status === 'active' ? '이 에이전트로 작업' : '비활성 에이전트'}
              </button>
            </div>
            <AgentEditor key={editing.agent_id} agent={editing} tools={tools} onSave={saveAgent} saving={saving} />
          </>}
        </>}
        {page === 'tools' && state === 'ready' && <ToolCatalog tools={tools} />}
        {page === 'templates' && <div className="template-grid">{templates.map(({ icon: Icon, example, ...template }) => <article className="panel template" key={template.name}>
          <div className="hero-icon small"><Icon size={21} /></div><h2>{template.name}</h2><p>{template.description}</p>
          <div className="tool-tags">{tools.filter(tool => template.tools.includes(tool.name)).map(tool => <span key={tool.name}>{tool.display_name}</span>)}</div>
          <button className="primary" disabled={saving || state !== 'ready'} onClick={() => {
            setBrief('');
            openDraft({ ...template, tools: template.tools.filter(name => tools.some(tool => tool.name === name)) });
            location.hash = 'create';
          }}><Plus size={16} />템플릿 사용</button>
        </article>)}</div>}
      </div>}
    </main>
  </div>;
}

function App() {
  const [user, setUser] = useState('kim');
  return <Workspace key={user} user={user} setUser={setUser} />;
}

createRoot(document.getElementById('root')).render(<App />);
