import React, { useCallback, useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import {
  ArrowUp, Bell, Bot, CalendarClock, FolderOpen, Braces, Check, ChevronRight, CircleAlert, FileText, Inbox, LayoutTemplate, ListChecks,
  LoaderCircle, Lock, Pencil, MessageSquare, Paperclip, Plus, Power, PowerOff, Search, Settings2, Share2, ShieldCheck,
  LogOut, Moon, PanelLeftClose, PanelLeftOpen, Sparkles, Sun, Trash2, Undo2, UserRound, Workflow, Wrench, X,
} from 'lucide-react';
import './style.css';
import LoginScreen from './LoginScreen';
import { ToolCatalog, ToolPicker } from './tool-visual';
import AgentSelect from './AgentSelect';
import { failedToolSummary } from './tool-status';
import { request } from './api';
import { InboxPage } from './inbox';
import { SchedulesPage } from './schedules';
import { DocumentDownloads, DocumentsPage, documentsFromEvents } from './documents';
import { savedReports, ReportContent } from './report-export';
import { MailComposer } from './mail-compose';
import {
  emptyChatHistory, currentConversation, updateConversation,
  startNewConversation, openConversation, conversationTitle, recentConversations,
  addSavedConversations, loadConversation, turnsToMessages, removeConversation,
} from './chat-history';
import { KnowledgeIcon, KnowledgePage } from './knowledge';

// ── 백엔드 API ──────────────────────────────────────────────
// /api 는 vite.config.js 의 프록시가 FastAPI 서버(기본 http://localhost:8000)로 넘긴다.
// 서버가 AUTH_MODE=dev 면 X-User-Id 헤더로 사용자를 고른다 (config/dev_users.yaml).
// AUTH_MODE=session 이면 로그인 쿠키로 누구인지 정하고, 서버는 X-User-Id 를 무시한다.

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function api(user, path, { method = 'GET', body, signal } = {}) {
  let res;
  try {
    res = await fetch(path, {
      method, signal,
      headers: { 'X-User-Id': user, ...(body !== undefined && !(body instanceof FormData) && { 'Content-Type': 'application/json' }) },
      body: body instanceof FormData ? body : body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError(0, '서버에 연결할 수 없습니다. 백엔드(uvicorn)가 켜져 있는지 확인하세요.');
  }
  if (res.status === 204) return null;
  const data = await res.json().catch(() => null);
  if (res.status === 401) dispatchEvent(new Event('auth-expired'));  // 로그인 모드: 로그인 화면으로
  if (!res.ok) {
    const detail = data?.detail;
    const text = typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.map(d => d.msg).join(', ') : res.statusText;
    throw new ApiError(res.status, text);
  }
  return data;
}

// ── 화면 정의 ───────────────────────────────────────────────

const navigation = [
  { id: 'chat', name: '작업 채팅', detail: '에이전트와 대화', icon: MessageSquare, color: '#78aaff' },
  { id: 'create', name: '에이전트 만들기', detail: '자연어로 초안 생성', icon: Sparkles, color: '#ba97ff' },
  { id: 'agents', name: '에이전트 관리', detail: '내 것·공유받은 것', icon: Settings2, color: '#65ddb0' },
  { id: 'inbox', name: '알림함', detail: '만기 알림·고객 안내 결과', icon: Bell, color: '#ffd166' },
  { id: 'documents', name: '내 문서', detail: '만든 문서 PDF·DOCX 받기', icon: FolderOpen, color: '#9be38c' },
  { id: 'shares', name: '공유 요청', detail: '승인·요청 내역', icon: Inbox, color: '#f17fa8' },
  { id: 'templates', name: '시나리오 템플릿', detail: '사전 구성으로 시작', icon: LayoutTemplate, color: '#f6b273' },
  { id: 'tools', name: '도구 목록', detail: '등록된 도구 보기', icon: Wrench, color: '#74d5e4' },
  { id: 'knowledge', name: '사내 문서', detail: '검색할 수 있는 문서 보기', icon: KnowledgeIcon, color: '#a8c7fa' },
  { id: 'schedules', name: '예약 작업', detail: '수집·리포트·만기 알림 (관리자)', icon: CalendarClock, color: '#c3a6ff', adminOnly: true },
];

// 채팅 첫 화면의 빠른 예시 (시나리오 템플릿은 서버의 /api/templates)
const quickExamples = [
  { name: '회의록 정리', description: '회의 메모에서 결정 사항과 후속 작업을 정리합니다.', system_prompt: '회의 메모를 읽고 핵심 논의, 결정 사항, 담당자와 기한이 포함된 후속 작업을 정리하세요.', tools: [], icon: FileText, example: '신규 기능 회의\n결정: 다음 배포에 검색 필터 추가\n후속 작업: 지민이 금요일까지 시안 작성' },
  { name: '코드 리뷰', description: '공유한 코드를 검토하는 흐름으로 시작합니다.', system_prompt: '코드의 정확성, 예외 처리, 가독성을 검토하고 수정 우선순위를 제시하세요.', tools: [], icon: Braces, example: 'function divide(a, b) { return a / b; }' },
  { name: '계산 도우미', description: '사칙연산 도구로 숫자를 계산합니다.', system_prompt: '숫자 계산은 반드시 도구로 하고, 계산식과 결과를 짧게 답하세요.', tools: ['add', 'subtract', 'multiply'], icon: ListChecks, example: '123 곱하기 45 계산해줘' },
];

const SCOPE = { personal: '개인', dept: '부서 공유', company: '전사 공유' };
const STATUS = { active: '사용 중', disabled: '비활성' };
const REQUEST_STATUS = { pending: '대기', approved: '승인', rejected: '반려' };
const ROLE = { admin: '관리자', dept_head: '부서장', member: '팀원' };
const blank = { name: '', description: '', system_prompt: '', tools: [] };
const EFFECT = { read: '조회', write: '등록·변경', send: '발송' };

// 업무 분류(domain)별로 묶는다. 분류가 없는 툴은 '기타'.
function groupTools(tools) {
  const groups = new Map();
  for (const tool of tools) {
    const key = tool.domain || '기타';
    groups.set(key, [...(groups.get(key) ?? []), tool]);
  }
  return [...groups];
}
const DEFAULT_AGENT = { agent_id: null, name: '기본 에이전트', description: '모든 도구를 쓰는 기본 에이전트' };
const themeKey = 'agent-workspace.theme.v1';

// 라이트·다크: 저장한 값이 없으면 OS 설정을 따른다 (로그인 화면도 같은 테마)
function readTheme() {
  try { const saved = localStorage.getItem(themeKey); if (saved === 'light' || saved === 'dark') return saved; } catch { /* 없음 */ }
  return matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}
document.documentElement.dataset.theme = readTheme();
const userKey = 'agent-workspace.user.v2';  // v2: 아이디가 사번(E1001…)으로 바뀜. v1 에 남은 kim 등은 무시

function currentPage() {
  const route = location.hash.slice(1);
  return navigation.some(({ id }) => id === route) ? route : 'chat';
}

function readUser() {
  try { return localStorage.getItem(userKey) || 'E1001'; } catch { return 'E1001'; }
}

function lockReason(agent, user) {
  if (agent.owner_user_id !== user && !agent.can_delete) return '다른 사람의 에이전트라 수정할 수 없습니다.';
  if (agent.scope !== 'personal') return '공유 중인 에이전트는 수정할 수 없습니다. 공유를 해제하면 수정할 수 있어요.';
  if (agent.pending_share_scope) return '공유 승인 대기 중이라 수정할 수 없습니다.';
  return '수정 권한이 없습니다.';
}

// LLM 답의 **굵게**, `코드` 만 표시한다 (HTML 은 해석하지 않는다)
function RichText({ text }) {
  return text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).map((part, index) => {
    if (part.startsWith('**') && part.endsWith('**') && part.length > 4) return <strong key={index}>{part.slice(2, -2)}</strong>;
    if (part.startsWith('`') && part.endsWith('`') && part.length > 2) return <code key={index}>{part.slice(1, -1)}</code>;
    return part;
  });
}

// 답 본문: 줄 단위로 목록(- , 1.)을 묶고, "근거:" 줄은 따로 표시한다. HTML 은 해석하지 않는다.
// 답에 걸린 시간: 보낸 순간부터 답이 올 때까지 (계획·도구 실행·답 쓰기 전부 포함)
function formatSeconds(seconds) {
  if (seconds < 60) return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)}초`;
  return `${Math.floor(seconds / 60)}분 ${Math.round(seconds % 60)}초`;
}

function Elapsed({ since }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  const seconds = Math.max(0, Math.floor((now - since) / 1000));
  return <span className="answer-time live">{seconds < 60 ? `${seconds}초` : formatSeconds(seconds)} 지남</span>;
}

const NAV_KEY = 'agent-workspace.nav-collapsed.v1';

// 답 본문 표시: 문단·목록·제목(#)·표(| 칸 |)를 그린다. 표는 마크다운 표 그대로 (시장 조회·정리 답 등).
function tableCells(line) {
  return line.replace(/^\|/, '').replace(/\|$/, '').split('|').map(cell => cell.trim());
}

function Formatted({ text }) {
  const blocks = [];
  for (const raw of text.split('\n')) {
    const line = raw.trim();
    const last = blocks[blocks.length - 1];
    if (/^\|.*\|$/.test(line)) {  // 표 한 줄
      if (last?.kind === 'table') last.items.push(line); else blocks.push({ kind: 'table', items: [line] });
      continue;
    }
    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) { blocks.push({ kind: 'h', level: heading[1].length, items: [heading[2]] }); continue; }
    const bullet = line.match(/^[-*•]\s+(.*)$/);
    const numbered = line.match(/^\d+[.)]\s+(.*)$/);
    const kind = bullet ? 'ul' : numbered ? 'ol' : line ? 'p' : 'gap';
    const body = bullet?.[1] ?? numbered?.[1] ?? line;
    if ((kind === 'ul' || kind === 'ol') && last?.kind === kind) last.items.push(body);
    else if (kind !== 'gap') blocks.push({ kind, items: [body] });
  }
  return <div className="formatted">{blocks.map((block, index) => {
    if (block.kind === 'ul') return <ul key={index}>{block.items.map((item, i) => <li key={i}><RichText text={item} /></li>)}</ul>;
    if (block.kind === 'ol') return <ol key={index}>{block.items.map((item, i) => <li key={i}><RichText text={item} /></li>)}</ol>;
    if (block.kind === 'h') return <p key={index} className={'answer-heading level-' + block.level}><RichText text={block.items[0]} /></p>;
    if (block.kind === 'table') {
      const rows = block.items.map(tableCells);
      const ruleAt = rows.findIndex(cells => cells.every(cell => /^:?-{2,}:?$/.test(cell)));
      const align = ruleAt >= 0 ? rows[ruleAt].map(cell => cell.endsWith(':') ? 'right' : 'left') : [];
      const head = ruleAt === 1 ? rows[0] : null;
      const body = rows.filter((_, i) => i !== ruleAt && !(head && i === 0));
      return <div key={index} className="answer-table"><table>
        {head && <thead><tr>{head.map((cell, i) => <th key={i} style={{ textAlign: align[i] }}><RichText text={cell} /></th>)}</tr></thead>}
        <tbody>{body.map((cells, r) => <tr key={r}>{cells.map((cell, i) => <td key={i} style={{ textAlign: align[i] }}><RichText text={cell} /></td>)}</tr>)}</tbody>
      </table></div>;
    }
    const [line] = block.items;
    if (/^\**근거\**\s*[:：]/.test(line)) return <p key={index} className="cite-line"><RichText text={line} /></p>;
    return <p key={index}><RichText text={line} /></p>;
  })}</div>;
}

function Badge({ children, tone = '' }) {
  return <span className={'badge ' + tone}>{children}</span>;
}

// ── 에이전트 편집 폼 ────────────────────────────────────────

function AgentEditor({ draft, tools, onSave, submitLabel, disabled = false, busy = false, suggested = [], withExample = false, onCancel }) {
  const [form, setForm] = useState(draft);
  const set = (key, value) => setForm(previous => ({ ...previous, [key]: value }));
  const ready = form.name.trim() && form.system_prompt.trim();
  return <form className="panel editor" onSubmit={event => {
    event.preventDefault();
    if (ready && !disabled) onSave({ ...form, name: form.name.trim(), description: form.description.trim(), system_prompt: form.system_prompt.trim() });
  }}>
    <fieldset disabled={disabled} className="editor-fields">
      <div className="form-grid">
        <label>이름<input required maxLength={100} value={form.name} onChange={event => set('name', event.target.value)} /></label>
        <label>설명<input maxLength={1000} value={form.description} onChange={event => set('description', event.target.value)} /></label>
      </div>
      <label>시스템 프롬프트<textarea required maxLength={4000} rows={6} value={form.system_prompt} onChange={event => set('system_prompt', event.target.value)} /></label>
      {withExample && <label>요청 예시 <small className="muted">· 이 템플릿으로 만든 에이전트에게 보낼 만한 요청</small><input maxLength={500} value={form.example ?? ''} onChange={event => set('example', event.target.value)} /></label>}
      <fieldset>
        <legend>도구 <small className="muted">· 관리자가 등록한 도구 중에서 고릅니다. 실제 실행 여부는 실행할 때 권한 정책이 판정합니다.</small></legend>
        {suggested.some(name => !form.tools.includes(name)) && <p className="suggest-note">
          <CircleAlert size={15} /> <b>추천</b> 표시된 도구는 데이터를 등록·변경하거나 무언가를 보냅니다. 이 에이전트에 정말 필요한지 확인하고 직접 체크하세요.
        </p>}
        <ToolPicker tools={tools} selected={form.tools} suggested={suggested} onChange={value => set('tools', value)} effectLabel={EFFECT} Badge={Badge} />
      </fieldset>
    </fieldset>
    {!disabled && <div className="actions">{onCancel && <button type="button" className="quiet" onClick={onCancel}>취소</button>}<button className="primary" disabled={!ready || busy}>
      {busy ? <LoaderCircle size={16} className="spin" /> : <Check size={16} />}{submitLabel}
    </button></div>}
  </form>;
}

// ── 작업 채팅 ───────────────────────────────────────────────

// 툴 실행 내역을 사람이 읽는 한 줄로. 모르는 툴은 결과 앞부분만 보여 준다.
const OPERATORS = { add: '+', subtract: '−', multiply: '×' };

function parseJson(text) {
  try { return JSON.parse(text); } catch { return null; }
}

function summarize(event, denial) {
  if (!event.ok) return failedToolSummary(event, denial);
  const data = parseJson(event.result);
  const args = event.arguments ?? {};
  if (event.tool_name === 'search_report_templates' && data?.matches) return `${data.matches.length}개 양식 찾음`;
  if (event.tool_name === 'get_report_template' && data?.template) return `${data.template.name} · 버전 ${data.template.version} · 전체 항목 확인`;
  if (event.tool_name === 'market_snapshot' && data?.fx) {
    return `${data.base_date} · ${data.fx.filter(item => !item.missing).length}개 통화${data.mode === 'replay' ? ' · 기록 재생' : ''}`;
  }
  if (event.tool_name === 'save_morning_report' && data?.report_id) {
    return `${data.template?.name ? data.template.name + ' · ' : ''}${data.base_date} · 저장 완료${data.check?.passed ? ' · 요약 검사 통과' : ' · 요약 제외'}${data.mode === 'replay' ? ' · 기록 재생' : ''}`;
  }
  if (event.tool_name === 'get_morning_report' && data?.report_id) return `${data.base_date} · 저장한 리포트 확인`;
  if (event.tool_name === 'parse_doc' && data?.filename) {
    return `${data.filename} · ${data.read_status?.scope === 'selected_passages' ? '관련 문단 확인' : '읽기 완료'}${data.tables ? ` · 표 ${data.tables}개` : ''}${data.truncated ? ' · 일부만 읽음' : ''}`;
  }
  if (event.tool_name === 'search_doc' && data?.matches) {
    return `"${args.query ?? data.query}" · ${data.matches.length ? data.matches.length + '건 찾음' : '찾지 못함'}`;
  }
  if (event.tool_name === 'get_doc_chunk' && data?.title) return `${data.title} ${data.location ?? ''}`.trim();
  if (event.tool_name === 'today' && data?.date) return `${data.date} ${data.weekday ?? ''}`.trim();
  if (OPERATORS[event.tool_name]) return `${args.a} ${OPERATORS[event.tool_name]} ${args.b} = ${event.result}`;
  return event.result.length > 60 ? event.result.slice(0, 60) + '…' : event.result;
}

function ToolEvents({ events, denial }) {
  if (!events?.length && !denial) return null;
  return <details className="tool-events">
    <summary>
      {events?.map(event => <span key={event.call_id} className={'event ' + (event.ok ? 'ok' : 'fail')}>
        {event.ok ? <Check size={13} /> : <X size={13} />}<strong>{event.display_name}</strong>{summarize(event, denial)}
      </span>)}
      {denial && <span className="event fail"><CircleAlert size={13} />{{ deny: '권한 없음', rejected: '승인 거절', timeout: '승인 시간 초과' }[denial.kind] ?? denial.kind} · {denial.display_name}</span>}
      <span className="more">자세히</span>
    </summary>
    <div className="event-raw">{events?.map(event => <div key={event.call_id}>
      <strong>{event.display_name}</strong> <code>{event.tool_name}</code>
      <pre>{JSON.stringify(event.arguments, null, 2)}{'\n→ '}{event.result}</pre>
    </div>)}</div>
  </details>;
}

// 문서를 검색했거나 답에 "근거:"를 달았는데 원문을 하나도 열지 않았으면,
// 모델이 제목만 보거나 아예 지어낸 내용일 수 있다 (qwen3:8b 에서 실제로 자주 생김)
function Sources({ items = [], searched = false, cited = false }) {
  const unread = (searched || cited) && !items.some(item => item.opened || item.previewed);
  if (!items.length && !unread) return null;
  return <div className="sources">
    {unread && <p className="unread-warning"><CircleAlert size={14} />{items.length
      ? '문서 원문을 확인하지 않고 만든 답입니다. 내용이 실제 규정과 다를 수 있으니 원문을 확인해 주세요.'
      : searched
        ? '관련 문서를 찾지 못했습니다. 위 내용은 사내 규정에 근거하지 않았을 수 있습니다.'
        : '근거가 적혀 있지만 실제로 확인한 문서가 없습니다. 내용과 근거를 그대로 믿지 마세요.'}</p>}
    {!!items.length && <>
    <span className="sources-title"><FileText size={14} />참고 문서</span>
    <ul>{items.map((item, index) => <li key={index} className={item.opened ? '' : 'muted'}>
      {item.title}{item.location && <span className="loc"> · {item.location}</span>}{item.partial && <span className="loc"> (일부 문단)</span>}{!item.opened && !item.previewed && <span className="loc"> (검색 결과)</span>}
    </li>)}</ul></>}
  </div>;
}

function ApprovalCards({ user, approvals, onAnswered }) {
  const [busy, setBusy] = useState('');
  async function answer(callId, approved) {
    setBusy(callId);
    try { await api(user, '/api/approvals/' + callId, { method: 'POST', body: { approved } }); } catch { /* 이미 처리됨 */ }
    setBusy('');
    onAnswered();
  }
  return approvals.map(item => <div key={item.call_id} className="panel approval">
    <div><strong><ShieldCheck size={16} /> 승인이 필요합니다 · {item.display_name}</strong>
      <p className="muted">{item.reason}</p><code>{JSON.stringify(item.arguments)}</code></div>
    <div className="actions">
      <button className="quiet" disabled={busy === item.call_id} onClick={() => answer(item.call_id, false)}><X size={16} />거절</button>
      <button className="primary" disabled={busy === item.call_id} onClick={() => answer(item.call_id, true)}><Check size={16} />승인</button>
    </div>
  </div>);
}

// 대화 메시지 목록 (채팅 화면과 읽기 전용 화면이 같이 쓴다)
function ThreadMessages({ messages }) {
  return messages.map((message, index) => <article className={'message ' + message.role} key={index}>
    <span className="message-label">{message.role === 'user' ? '나' : message.role === 'error' ? '오류' : message.name}</span>
    {message.attachments?.length > 0 && <div className="attachment-list">{message.attachments.map(file =>
      <span className="attachment-chip" key={file.file_id}><FileText size={15} />{file.filename}</span>
    )}</div>}
    <ToolEvents events={message.events} denial={message.denial} />
    {message.role === 'agent' ? <Formatted text={message.content} /> : <div>{message.content}</div>}
    {message.role === 'agent' && <Sources items={message.sources}
      searched={message.events?.some(event => event.tool_name === 'search_doc')}
      cited={/(^|\n)\s*\**근거\**\s*[:：]/.test(message.content)} />}
  </article>);
}

// 에이전트가 삭제·비활성화됐거나 더 쓸 수 없는 지난 대화: 기록은 보여 주고 이어서 대화하지는 못하게 한다
function ReadOnlyConversation({ thread, agentName, reason, onLeave }) {
  return <>
    <div className="chat-toolbar">
      <span className="agent-picker readonly"><Bot size={17} /><span>{agentName}</span></span>
      <button className="quiet" onClick={onLeave}><Plus size={17} />새 대화</button>
    </div>
    <div className="chat-body"><div className="thread"><ThreadMessages messages={thread.messages} /></div></div>
    <div className="compose-area">
      <p className="readonly-note" role="status"><CircleAlert size={15} />{reason} 기록만 볼 수 있고, 이어서 대화할 수는 없습니다.</p>
    </div>
  </>;
}

function ChatPage({ user, tools, agents, activeId, setActiveId, chatHistory, changeConversation, newConversation, notify, onBusyChange }) {
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  const [sentAt, setSentAt] = useState(0);  // 답을 기다리기 시작한 시각 (걸린 시간 표시)
  const [mailBusy, setMailBusy] = useState(false);
  const [approvals, setApprovals] = useState([]);
  const [attachments, setAttachments] = useState([]);
  const [uploading, setUploading] = useState(false);
  const fileInput = useRef(null);
  const alive = useRef(true);
  const controller = useRef(new AbortController());
  const chatEnd = useRef(null);
  const usable = [DEFAULT_AGENT, ...agents.filter(agent => agent.status === 'active')];
  const active = usable.find(agent => agent.agent_id === activeId) ?? DEFAULT_AGENT;
  const canParse = tools.some(tool => tool.name === 'parse_doc') &&
    (active.agent_id === null || active.tools?.includes('parse_doc'));
  const busy = sending || uploading || mailBusy;
  const canMail = tools.some(tool => tool.name === 'send_mail') &&
    (active.agent_id === null || active.tools?.includes('send_mail'));
  const conversation = currentConversation(chatHistory, active.agent_id);

  useEffect(() => {
    onBusyChange(busy);
  }, [busy, onBusyChange]);
  useEffect(() => () => {
    alive.current = false;
    controller.current.abort();
    onBusyChange(false);
  }, [onBusyChange]);

  useEffect(() => { chatEnd.current?.scrollIntoView({ block: 'nearest' }); }, [conversation.messages.length, sending, approvals.length]);

  // 답을 기다리는 동안 승인 요청(ASK)이 오는지 1초마다 확인한다
  const pollApprovals = useCallback(async () => {
    try { setApprovals(await api(user, '/api/approvals')); } catch { /* 다음 차례에 다시 */ }
  }, [user]);
  useEffect(() => {
    if (!sending && !mailBusy) { setApprovals([]); return undefined; }
    const timer = setInterval(pollApprovals, 1000);
    return () => clearInterval(timer);
  }, [sending, mailBusy, pollApprovals]);

  function update(fn) { changeConversation(active.agent_id, fn); }

  async function attachFiles(event) {
    const files = Array.from(event.target.files || []);
    event.target.value = '';
    if (!files.length || !canParse || busy) return;
    if (attachments.length + files.length > 5) {
      notify('한 번에 파일을 5개까지 첨부할 수 있습니다.');
      return;
    }
    if (files.some(file => file.size === 0 || file.size > 20 * 1024 * 1024)) {
      notify('비어 있지 않은 20MB 이하 파일을 선택해 주세요.');
      return;
    }
    setUploading(true);
    try {
      for (const file of files) {
        const form = new FormData();
        form.append('file', file);
        const uploaded = await api(user, '/api/uploads', { method: 'POST', body: form, signal: controller.current.signal });
        if (!alive.current) return;
        setAttachments(previous => [...previous, uploaded]);
      }
    } catch (error) {
      if (alive.current) notify(error.message);
    } finally {
      if (alive.current) setUploading(false);
    }
  }

  async function send(event) {
    event.preventDefault();
    const content = input.trim();
    if ((!content && !attachments.length) || busy) return;
    const sentAttachments = attachments;
    setInput('');
    setAttachments([]);
    update(c => ({ ...c, messages: [...c.messages, { role: 'user', content, attachments: sentAttachments }] }));
    const started = Date.now();
    setSentAt(started);
    setSending(true);
    try {
      const body = conversation.sessionId ? { content, session_id: conversation.sessionId } : { content, agent_id: active.agent_id };
      body.file_ids = sentAttachments.map(file => file.file_id);
      const reply = await api(user, '/api/chat/messages', { method: 'POST', body, signal: controller.current.signal });
      if (!alive.current) return;
      update(c => ({ sessionId: reply.session_id, messages: [...c.messages, {
        role: 'agent', name: active.name, content: reply.message, events: reply.tool_events, denial: reply.denial, sources: reply.sources,
        seconds: (Date.now() - started) / 1000,
      }] }));
    } catch (error) {
      if (!alive.current) return;
      setInput(content);
      setAttachments(sentAttachments);
      if (error.status === 404 && conversation.sessionId) update(c => ({ ...c, sessionId: null })); // 서버 재시작 등으로 세션이 사라짐
      update(c => ({ ...c, messages: [...c.messages, { role: 'error', content: error.message }] }));
    } finally {
      if (alive.current) setSending(false);
    }
  }

  return <>
    <div className="chat-toolbar">
      <AgentSelect agents={usable} value={active.agent_id} user={user} disabled={busy}
        recentIds={recentConversations(chatHistory).map(thread => thread.agentId)}
        onChange={id => { setActiveId(id); setInput(''); setAttachments([]); }} />
      <button className="quiet" disabled={busy} onClick={() => { newConversation(active.agent_id); setInput(''); setAttachments([]); }}><Plus size={17} />새 대화</button>
    </div>
    <div className="chat-body">
      {conversation.messages.length ? <div className="thread" aria-live="polite">
        {conversation.messages.map((message, index) => <article className={'message ' + message.role} key={index}>
          <span className="message-label">{message.role === 'user' ? '나' : message.role === 'error' ? '오류' : message.name}
            {message.seconds !== undefined && <span className="answer-time" title="질문을 보낸 뒤 답이 올 때까지 걸린 시간">{formatSeconds(message.seconds)} 걸림</span>}</span>
          {message.attachments?.length > 0 && <div className="attachment-list">{message.attachments.map(file =>
            <span className="attachment-chip" key={file.file_id}><FileText size={15} />{file.filename}</span>
          )}</div>}
          <ToolEvents events={message.events} denial={message.denial} />
          {message.role === 'agent' && savedReports(message.events).length > 0
            ? savedReports(message.events).map(report => <section key={report.report_id} className="saved-report">
              <ReportContent document={report.document} markdown={report.markdown} />
            </section>)
            : message.role === 'agent' ? <Formatted text={message.content} /> : <div>{message.content}</div>}
          {message.role === 'agent' && <DocumentDownloads user={user} documents={documentsFromEvents(message.events)} notify={notify} />}
          {message.role === 'agent' && canMail && conversation.sessionId && message.content && !message.events?.some(event => event.tool_name === 'send_mail') &&
            <MailComposer key={conversation.sessionId + ':' + index} user={user} sessionId={conversation.sessionId} text={message.content} api={api} busy={busy}
              onBusyChange={setMailBusy} onResult={response => notify(response.message, response.status === 'sent' ? 'ok' : response.status === 'simulated' ? 'info' : 'error')} />}
          {message.role === 'agent' && <Sources items={message.sources}
            searched={message.events?.some(event => event.tool_name === 'search_doc')}
            cited={/(^|\n)\s*\**근거\**\s*[:：]/.test(message.content)} />}
        </article>)}
        {sending && <article className="message agent pending"><LoaderCircle size={16} className="spin" /> 답을 만드는 중입니다… <Elapsed since={sentAt} /></article>}
        <ApprovalCards user={user} approvals={approvals} onAnswered={pollApprovals} />
        <div ref={chatEnd} />
      </div> : <div className="welcome">
        {/* 첫 화면: 색 면 + 큰 제목 + 은은한 이펙트 */}
        <section className="welcome-hero">
          <div className="hero-fx" aria-hidden="true"><span className="glow g1" /><span className="glow g2" /><span className="dots" /></div>
          <p className="hero-eyebrow"><Bot size={14} />{active.name}</p>
          <h1>무엇을<br /><em>맡겨 볼까요?</em></h1>
          <p className="hero-desc">{active.description || '설명 없음'}</p>
        </section>
        <p className="quick-title">이렇게 시작해 보세요</p>
        <div className="quick-grid">
          {quickExamples.map(({ name, icon: Icon, example }) => <button key={name} className="quick" onClick={() => setInput(example)}>
            <span className="quick-head"><span className="quick-icon"><Icon size={17} /></span><strong>{name}</strong><ChevronRight size={15} /></span>
            <span className="quick-example">{example.split('\n')[0]}</span>
          </button>)}
        </div>
      </div>}
    </div>
    <div className="compose-area">
      {attachments.length > 0 && <div className="attachment-list" aria-label="첨부된 파일">{attachments.map(file =>
        <span className="attachment-chip" key={file.file_id}><FileText size={15} /><span>{file.filename}</span>
          <button type="button" disabled={busy} aria-label={file.filename + ' 첨부 제거'} onClick={() => setAttachments(previous => previous.filter(item => item.file_id !== file.file_id))}><X size={14} /></button>
        </span>
      )}</div>}
      {uploading && <p role="status" className="attachment-help">파일을 업로드하는 중입니다…</p>}
      <form className="composer" onSubmit={send}>
      <input type="file" ref={fileInput} hidden multiple onChange={attachFiles} aria-label="문서 파일 선택"
        accept=".hwp,.hwpx,.pdf,.docx,.xlsx,.pptx,.html,.htm,.eml,.csv,.tsv,.md,.markdown,.png,.jpg,.jpeg,.tif,.tiff,.gif,.bmp" />
      <button className="attach-button" type="button" aria-label="파일 첨부" disabled={!canParse || busy || attachments.length >= 5}
        title={canParse ? '파일 첨부 · 최대 5개, 각 20MB' : '에이전트에 업로드 문서 읽기 도구를 추가해 주세요.'} onClick={() => fileInput.current.click()}><Paperclip size={20} /></button>
      <textarea aria-label="메시지" readOnly={sending} rows={2} maxLength={4000} value={input} onChange={event => setInput(event.target.value)}
        placeholder="에이전트에게 작업을 요청하세요"
        onKeyDown={event => {
          if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229) {
            event.preventDefault();
            event.currentTarget.form.requestSubmit();
          }
        }} />
      <button className="send" aria-label="메시지 보내기" disabled={(!input.trim() && !attachments.length) || busy}><ArrowUp size={20} /></button>
    </form><p className="footnote">{canParse ? '문서·이미지 첨부 가능 · 파일당 20MB, 최대 5개' : '파일을 읽으려면 에이전트에 업로드 문서 읽기 도구를 추가하세요.'}</p></div>
  </>;
}

// ── 에이전트 만들기 ─────────────────────────────────────────

function CreatePage({ user, tools, draft, setDraft, onCreated, notify }) {
  const [brief, setBrief] = useState('');
  const [drafting, setDrafting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [info, setInfo] = useState('');
  const [missing, setMissing] = useState([]);

  async function makeDraft(event) {
    event.preventDefault();
    if (!brief.trim() || drafting) return;
    setDrafting(true); setInfo(''); setMissing([]);
    try {
      const d = await api(user, '/api/agents/draft', { method: 'POST', body: { description: brief.trim() } });
      setDraft({ value: { name: d.name, description: d.description, system_prompt: d.system_prompt, tools: d.tools }, suggested: d.suggested_tools || [], version: Date.now() });
      const notes = [];
      if (d.source === 'fallback') notes.push('LLM 답을 쓸 수 없어 기본 초안을 만들었습니다. 내용을 직접 다듬어 주세요.');
      if (d.dropped_tools.length) notes.push('등록되지 않은 도구는 뺐습니다: ' + d.dropped_tools.join(', '));
      setInfo(notes.join(' '));
      setMissing(d.missing || []);
    } catch (error) {
      notify(error.message);
    } finally {
      setDrafting(false);
    }
  }

  async function save(value) {
    setSaving(true);
    try {
      const agent = await api(user, '/api/agents', { method: 'POST', body: value });
      setDraft(null); setBrief(''); setInfo(''); setMissing([]);
      onCreated(agent);
    } catch (error) {
      notify(error.message);
    } finally {
      setSaving(false);
    }
  }

  return <>
    <form className="panel editor" onSubmit={makeDraft}>
      <label>어떤 에이전트가 필요한가요?<textarea rows={4} value={brief} maxLength={1200} onChange={event => setBrief(event.target.value)} placeholder="예: 회의 메모를 요약하고 후속 작업을 체크리스트로 정리하는 에이전트를 만들어 줘." /></label>
      <div className="actions">
        <button type="button" className="quiet" onClick={() => { setDraft({ value: blank, version: Date.now() }); setInfo(''); }}>직접 입력</button>
        <button className="primary" disabled={!brief.trim() || drafting}>{drafting ? <LoaderCircle size={16} className="spin" /> : <Sparkles size={16} />}{drafting ? 'LLM이 초안을 만드는 중… (20~40초)' : '초안 생성'}</button>
      </div>
    </form>
    {info && <p className="notice" role="status">{info}</p>}
    {missing.length > 0 && <div className="missing-tools" role="status">
      <strong>등록된 도구로는 아직 할 수 없는 일</strong>
      <ul>{missing.map(item => <li key={item}>{item}</li>)}</ul>
      <p className="muted">이 일은 에이전트가 할 수 없다고 안내합니다. 필요하면 관리자에게 도구 등록을 요청하세요.</p>
    </div>}
    {draft && <><h2>에이전트 설정</h2><p className="muted">저장하면 개인용으로 만들어집니다. 부서·전사 공유는 저장 후 상사 승인을 받아야 합니다.</p>
      <AgentEditor key={draft.version} draft={draft.value} suggested={draft.suggested || []} tools={tools} onSave={save} submitLabel="에이전트 저장" busy={saving} /></>}
  </>;
}

// ── 에이전트 관리 ───────────────────────────────────────────

function ShareForm({ agent, onRequest }) {
  const [scope, setScope] = useState(agent.scope === 'dept' ? 'company' : 'dept');
  const [comment, setComment] = useState('');
  return <form className="share-form" onSubmit={event => { event.preventDefault(); onRequest(scope, comment.trim()); }}>
    <select value={scope} onChange={event => setScope(event.target.value)} aria-label="공유 범위">
      {agent.scope === 'personal' && <option value="dept">부서 공유</option>}
      <option value="company">전사 공유</option>
    </select>
    <input value={comment} maxLength={1000} onChange={event => setComment(event.target.value)} placeholder="요청 사유 (선택)" />
    <button className="quiet"><Share2 size={16} />공유 요청</button>
  </form>;
}

function AgentsPage({ user, agents, tools, editingId, setEditingId, refresh, openChat, notify }) {
  const [query, setQuery] = useState('');
  const [mineOnly, setMineOnly] = useState(false);
  const [saving, setSaving] = useState(false);
  const filtered = agents
    .filter(agent => !mineOnly || agent.owner_user_id === user)
    .filter(({ name, description }) => (name + ' ' + description).toLowerCase().includes(query.trim().toLowerCase()));
  const editing = agents.find(agent => agent.agent_id === editingId);

  async function run(path, options, message) {
    try {
      await api(user, path, options);
      notify(message, 'ok');
    } catch (error) {
      notify(error.message);
    }
    await refresh();
  }

  async function save(value) {
    setSaving(true);
    await run('/api/agents/' + editing.agent_id, { method: 'PATCH', body: value }, '변경사항을 저장했습니다. 변경된 설정은 새 대화에서 테스트해 주세요');
    setSaving(false);
  }

  return <>
    <div className="section-heading">
      <label className="search"><Search size={18} /><input aria-label="에이전트 검색" placeholder="에이전트 검색" value={query} onChange={event => setQuery(event.target.value)} /></label>
      <label className="check"><input type="checkbox" checked={mineOnly} onChange={event => setMineOnly(event.target.checked)} />내가 만든 것만</label>
    </div>
    <div className="agent-list">{filtered.map(agent => <button className={'panel agent-card ' + (agent.agent_id === editingId ? 'selected' : '')} key={agent.agent_id} onClick={() => setEditingId(agent.agent_id)}>
      <Bot size={20} /><span><strong>{agent.name}</strong><small>{agent.description || '설명 없음'}</small>
        <span className="badges"><Badge tone={agent.scope}>{SCOPE[agent.scope]}</Badge>{agent.status !== 'active' && <Badge tone="off">{STATUS[agent.status]}</Badge>}
          {agent.pending_share_scope && <Badge tone="wait">{SCOPE[agent.pending_share_scope]} 승인 대기</Badge>}
          {agent.owner_user_id !== user && <Badge>만든 사람 {agent.owner_user_id}</Badge>}</span></span><ChevronRight size={16} />
    </button>)}{!filtered.length && <p className="muted">볼 수 있는 에이전트가 없습니다. 에이전트 만들기에서 시작하세요.</p>}</div>

    {editing && <>
      <div className="section-heading"><h2>{editing.name}</h2>
        <div className="actions wrap">
          {editing.status === 'active' && <button className="quiet" onClick={() => openChat(editing.agent_id)}><MessageSquare size={16} />이 에이전트로 작업</button>}
          {editing.can_unshare && <button className="quiet" onClick={() => run(`/api/agents/${editing.agent_id}/unshare`, { method: 'POST' }, '공유를 해제했습니다. 이제 수정할 수 있습니다.')}><Undo2 size={16} />공유 해제</button>}
          {editing.can_disable && <button className="quiet" onClick={() => run(`/api/agents/${editing.agent_id}/disable`, { method: 'POST' }, '비활성화했습니다.')}><PowerOff size={16} />비활성화</button>}
          {editing.can_enable && <button className="quiet" onClick={() => run(`/api/agents/${editing.agent_id}/enable`, { method: 'POST' }, '다시 활성화했습니다.')}><Power size={16} />활성화</button>}
          {editing.can_delete && <button className="quiet danger" onClick={() => {
            if (confirm(`'${editing.name}' 에이전트를 삭제할까요? 되돌릴 수 없습니다.`)) run('/api/agents/' + editing.agent_id, { method: 'DELETE' }, '삭제했습니다.');
          }}><Trash2 size={16} />삭제</button>}
        </div>
      </div>
      {editing.can_request_share && <ShareForm key={editing.agent_id + editing.scope} agent={editing} onRequest={(scope, comment) =>
        run(`/api/agents/${editing.agent_id}/share-requests`, { method: 'POST', body: { scope, comment } }, '공유 요청을 보냈습니다. 바로 위 관리자가 승인하면 공유됩니다.')} />}
      {!editing.can_edit && <p className="notice lock"><Lock size={15} />{lockReason(editing, user)}</p>}
      <AgentEditor key={editing.agent_id + JSON.stringify(editing)} draft={editing} tools={tools} onSave={save}
        submitLabel="변경사항 저장" disabled={!editing.can_edit} busy={saving} />
    </>}
  </>;
}

// ── 공유 요청 ───────────────────────────────────────────────

// ── 시나리오 템플릿 ─────────────────────────────────────────
// 관리자가 만든 템플릿을 골라 내 에이전트로 시작한다. 툴이 아직 없는 템플릿은 '준비 중'.

const emptyTemplate = { ...blank, example: '' };

function TemplatesPage({ user, me, tools, setDraft, notify }) {
  const [items, setItems] = useState([]);
  const [editing, setEditing] = useState(null); // null | 'new' | 템플릿
  const [busy, setBusy] = useState(false);
  const isAdmin = me?.roles.includes('admin');
  const label = name => tools.find(tool => tool.name === name)?.display_name ?? name;

  const refresh = useCallback(async () => {
    try { setItems(await api(user, '/api/templates')); } catch (error) { notify(error.message); }
  }, [user, notify]);
  useEffect(() => { refresh(); setEditing(null); }, [refresh]);

  function use(template) {
    setDraft({ value: { name: template.name, description: template.description, system_prompt: template.system_prompt, tools: template.tools }, version: Date.now() });
    location.hash = 'create';
  }

  async function save(value) {
    setBusy(true);
    const body = { name: value.name, description: value.description, system_prompt: value.system_prompt, tools: value.tools, example: (value.example ?? '').trim() };
    try {
      if (editing === 'new') await api(user, '/api/templates', { method: 'POST', body });
      else await api(user, '/api/templates/' + editing.template_id, { method: 'PATCH', body });
      setEditing(null); await refresh(); notify(`'${body.name}' 템플릿을 저장했습니다.`, 'ok');
    } catch (error) { notify(error.message); } finally { setBusy(false); }
  }

  async function remove(template) {
    if (!confirm(`'${template.name}' 템플릿을 지울까요? 이미 이 템플릿으로 만든 에이전트는 그대로 남습니다.`)) return;
    try { await api(user, '/api/templates/' + template.template_id, { method: 'DELETE' }); await refresh(); }
    catch (error) { notify(error.message); }
  }

  if (editing) return <>
    <h2>{editing === 'new' ? '새 템플릿' : '템플릿 수정'}</h2>
    <p className="muted">화면에서 만든 템플릿은 등록된 도구만 넣을 수 있습니다. 서버를 다시 켜면 기본 템플릿(config/agent_templates.yaml)으로 돌아갑니다.</p>
    <AgentEditor key={editing === 'new' ? 'new' : editing.template_id} draft={editing === 'new' ? emptyTemplate : editing}
      tools={tools} onSave={save} submitLabel="템플릿 저장" busy={busy} withExample onCancel={() => setEditing(null)} />
  </>;

  return <>
    {isAdmin && <div className="actions start"><button className="primary" onClick={() => setEditing('new')}><Plus size={16} />새 템플릿</button></div>}
    <div className="template-grid">{items.map(template => <article className={'panel template' + (template.available ? '' : ' unavailable')} key={template.template_id}>
      <div className="template-head">
        <div className="hero-icon small"><LayoutTemplate size={21} /></div>
        {!template.available && <Badge tone="wait">준비 중</Badge>}
      </div>
      <h2>{template.name}</h2><p>{template.description}</p>
      {template.example && <p className="template-example">예: {template.example}</p>}
      <div className="tool-tags">{template.tools.map(name => <span key={name} className={template.missing_tools.includes(name) ? 'missing' : ''}>
        {template.missing_tools.includes(name) ? name + ' (없음)' : label(name)}</span>)}</div>
      <div className="actions">
        {isAdmin && <>
          <button className="quiet" title="수정" onClick={() => setEditing(template)}><Pencil size={15} />수정</button>
          <button className="quiet danger" title="삭제" onClick={() => remove(template)}><Trash2 size={15} />삭제</button>
        </>}
        <button className="primary" disabled={!template.available} onClick={() => use(template)}><Plus size={16} />{template.available ? '템플릿 사용' : '도구 준비 중'}</button>
      </div>
    </article>)}</div>
    {!items.length && <p className="muted">등록된 템플릿이 없습니다.</p>}
  </>;
}

function SharesPage({ user, notify, refreshAgents }) {
  const [items, setItems] = useState([]);
  const [comments, setComments] = useState({});
  const load = useCallback(async () => {
    try { setItems(await api(user, '/api/share-requests')); } catch (error) { notify(error.message); }
  }, [user, notify]);
  useEffect(() => { load(); }, [load]);

  async function decide(id, approve) {
    try {
      await api(user, `/api/share-requests/${id}/${approve ? 'approve' : 'reject'}`, { method: 'POST', body: { comment: comments[id] ?? '' } });
      notify(approve ? '승인했습니다.' : '반려했습니다.', 'ok');
    } catch (error) { notify(error.message); }
    await load(); await refreshAgents();
  }

  const inbox = items.filter(item => item.can_decide);
  const others = items.filter(item => !item.can_decide);
  return <>
    <h2>받은 요청 <small className="muted">· 내가 승인할 대기 요청</small></h2>
    {inbox.length ? inbox.map(item => <div key={item.request_id} className="panel request">
      <div><strong>{item.agent_name}</strong> <Badge tone="wait">{SCOPE[item.requested_scope]}</Badge>
        <p className="muted">요청자 {item.requester_id} · {new Date(item.requested_at).toLocaleString()}</p>
        {item.comment && <p>사유: {item.comment}</p>}</div>
      <div className="request-actions">
        <input placeholder="의견 (선택)" value={comments[item.request_id] ?? ''} onChange={event => setComments(previous => ({ ...previous, [item.request_id]: event.target.value }))} />
        <button className="quiet" onClick={() => decide(item.request_id, false)}><X size={16} />반려</button>
        <button className="primary" onClick={() => decide(item.request_id, true)}><Check size={16} />승인</button>
      </div>
    </div>) : <p className="muted">승인할 요청이 없습니다.</p>}
    <h2>요청 내역</h2>
    {others.length ? <ul className="request-log">{others.map(item => <li key={item.request_id} className="panel">
      <strong>{item.agent_name}</strong> <Badge tone={item.status === 'approved' ? 'ok' : item.status === 'rejected' ? 'off' : 'wait'}>{SCOPE[item.requested_scope]} · {REQUEST_STATUS[item.status]}</Badge>
      <p className="muted">요청 {item.requester_id} → 승인자 {item.approver_id}{item.decided_by ? ` · 처리 ${item.decided_by}` : ''}{item.decision_comment ? ` · "${item.decision_comment}"` : ''}</p>
    </li>)}</ul> : <p className="muted">내역이 없습니다.</p>}
  </>;
}

// ── 앱 ─────────────────────────────────────────────────────

function App({ session, theme, setTheme }) {
  const [chatBusy, setChatBusy] = useState(false);
  const [page, setPage] = useState(currentPage);
  const [user, setUser] = useState(() => session?.userId ?? readUser());
  const [users, setUsers] = useState([]);
  const [me, setMe] = useState(null);
  const [tools, setTools] = useState([]);
  const [agents, setAgents] = useState([]);
  const [activeId, setActiveId] = useState(null);
  const [editingId, setEditingId] = useState(null);
  const [draft, setDraft] = useState(null);
  const [chatHistory, setChatHistory] = useState(emptyChatHistory);
  // 왼쪽 메뉴 접기: 이 브라우저에만 기억한다 (저장이 막혀 있으면 펼친 채로 시작)
  const [navCollapsed, setNavCollapsed] = useState(() => { try { return localStorage.getItem(NAV_KEY) === '1'; } catch { return false; } });
  function toggleNav() {
    setNavCollapsed(previous => {
      try { localStorage.setItem(NAV_KEY, previous ? '0' : '1'); } catch { /* 저장 못 해도 화면은 바뀜 */ }
      return !previous;
    });
  }
  const [deletingThread, setDeletingThread] = useState('');
  const [chatView, setChatView] = useState(0);
  const [viewingId, setViewingId] = useState(null);  // 읽기 전용으로 연 지난 대화
  const [notice, setNotice] = useState(null);
  const [unread, setUnread] = useState(0);  // 알림함의 안 읽은 알림 수 (메뉴 배지)
  const route = navigation.find(({ id }) => id === page);

  const notify = useCallback((text, tone = 'error') => setNotice(text ? { text, tone } : null), []);

  const refreshAgents = useCallback(async () => {
    try { setAgents(await api(user, '/api/agents')); } catch (error) { notify(error.message); }
  }, [user, notify]);
  const loadKnowledge = useCallback(() => api(user, '/api/knowledge/docs'), [user]);

  useEffect(() => {
    if (session) return;  // 로그인 모드에서는 사용자 목록을 열 수 없다
    api(user, '/api/dev/users').then(setUsers).catch(error => notify(error.message));
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!session) try { localStorage.setItem(userKey, user); } catch { /* 저장 못 해도 동작 */ }
    setChatHistory(emptyChatHistory()); setActiveId(null); setEditingId(null); setNotice(null); setViewingId(null);
    api(user, '/api/me').then(setMe).catch(error => { setMe(null); notify(error.message); });
    api(user, '/api/tools').then(setTools).catch(() => setTools([]));
    refreshAgents();
    // 서버에 저장된 내 대화 목록 (STORE=db 면 나갔다 와도, 서버를 다시 켜도 남아 있다)
    let current = true;
    api(user, '/api/chat/sessions')
      .then(list => { if (current) setChatHistory(previous => addSavedConversations(previous, list)); })
      .catch(() => { /* 목록을 못 받아도 새 대화는 할 수 있다 */ });
    return () => { current = false; };
  }, [user]); // eslint-disable-line react-hooks/exhaustive-deps

  // 안 읽은 알림 수: 화면을 옮길 때와 30초마다 다시 센다 (채팅에서 알림을 보낸 뒤에도 바로 보이게)
  useEffect(() => {
    let current = true;
    // 배지는 뒤에서 도는 조회라 실패해도 로그인 화면으로 보내지 않는다 (request 는 auth-expired 를 보내지 않음)
    const count = () => request(user, '/inbox/unread-count')
      .then(data => { if (current) setUnread(data.unread ?? 0); }).catch(() => { /* 못 세도 메뉴는 그대로 */ });
    count();
    const timer = setInterval(count, 30000);
    return () => { current = false; clearInterval(timer); };
  }, [user, page, chatBusy]);

  useEffect(() => {
    const updatePage = () => {
      setPage(currentPage()); setNotice(null);
      if (currentPage() !== 'chat') setViewingId(null);
    };
    addEventListener('hashchange', updatePage);
    return () => removeEventListener('hashchange', updatePage);
  }, []);

  function openChat(id) { setActiveId(id); location.hash = 'chat'; }

  function changeConversation(agentId, change) {
    setChatHistory(previous => updateConversation(previous, agentId, change));
  }

  function newConversation(agentId) {
    if (chatBusy) return;
    setViewingId(null);
    setChatHistory(previous => startNewConversation(previous, agentId));
    setChatView(previous => previous + 1);
  }

  // 대화 지우기: 서버 기록까지 지운다 (그 대화로 만든 문서·알림은 남음). 서버에 없는 대화는 화면에서만 뺀다.
  async function deleteConversation(thread) {
    if (chatBusy || deletingThread) return;
    if (!confirm(`"${conversationTitle(thread)}" 대화를 지울까요?
지운 대화는 되돌릴 수 없습니다. 이 대화로 만든 문서와 알림은 남습니다.`)) return;
    setDeletingThread(thread.id);
    try {
      if (thread.sessionId) {
        try {
          await api(user, `/api/chat/sessions/${encodeURIComponent(thread.sessionId)}`, { method: 'DELETE' });
        } catch (error) {
          if (error.status !== 404) throw error;  // 서버에 이미 없으면 화면에서만 뺀다
        }
      }
      const wasOpen = viewingId === thread.id || currentConversation(chatHistory, thread.agentId).id === thread.id;
      setChatHistory(previous => removeConversation(previous, thread.id));
      if (wasOpen) { setViewingId(null); setChatView(previous => previous + 1); }
      notify('대화를 지웠습니다.', 'ok');
    } catch (error) {
      notify(error.message);
    } finally {
      setDeletingThread('');
    }
  }

  async function openSavedConversation(thread) {
    if (chatBusy) return;
    const agent = agents.find(item => item.agent_id === thread.agentId);
    const usable = thread.agentId === null || agent?.status === 'active';
    if (thread.saved) {  // 서버에만 있는 대화: 본문을 불러온다
      try {
        const turns = await api(user, `/api/chat/sessions/${encodeURIComponent(thread.sessionId)}/turns`);
        const name = thread.agentId === null ? DEFAULT_AGENT.name : agent?.name ?? '사용할 수 없는 에이전트';
        setChatHistory(previous => loadConversation(previous, thread.id, turnsToMessages(turns, name)));
      } catch (error) {
        notify(error.message);
        return;
      }
    }
    if (!usable) {  // 에이전트를 더 쓸 수 없으면 기록만 보여 준다
      setViewingId(thread.id);
      location.hash = 'chat';
      return;
    }
    setViewingId(null);
    setChatHistory(previous => openConversation(previous, thread.id));
    setChatView(previous => previous + 1);
    setActiveId(thread.agentId);
    location.hash = 'chat';
  }

  const recent = recentConversations(chatHistory);
  const viewing = viewingId ? chatHistory.threads[viewingId] : null;
  const viewingAgent = viewing && agents.find(item => item.agent_id === viewing.agentId);
  const currentThreadId = viewing ? viewing.id : currentConversation(chatHistory, activeId).id;

  return <div className={'shell' + (navCollapsed ? ' nav-collapsed' : '')} style={{ '--accent': route.color }}>
    <aside className="sidebar">
      <div className="sidebar-heading"><span><strong>Agent Argus</strong><small>사내 업무 에이전트</small></span>
        <button type="button" className="nav-toggle" onClick={toggleNav} aria-expanded={!navCollapsed}
          aria-label={navCollapsed ? '메뉴 펼치기' : '메뉴 접기'} title={navCollapsed ? '메뉴 펼치기' : '메뉴 접기'}>
          {navCollapsed ? <PanelLeftOpen size={17} /> : <PanelLeftClose size={17} />}</button></div>
      <nav aria-label="워크스페이스 메뉴">{navigation.filter(item => !item.adminOnly || me?.roles?.includes('admin')).map(({ id, name, detail, icon: Icon, color }) => <a key={id} href={'#' + id} aria-current={page === id ? 'page' : undefined} style={{ '--item-accent': color }} title={navCollapsed ? name : undefined}>
        <span className="nav-icon"><Icon size={19} /></span><span><strong>{name}</strong><small>{detail}</small></span>
        {id === 'inbox' && unread > 0 && <span className="nav-count" aria-label={`안 읽은 알림 ${unread}개`}>{unread > 99 ? '99+' : unread}</span>}
        <ChevronRight size={15} className="nav-chevron" />
      </a>)}</nav>
      <section className="sidebar-history" aria-label="최근 대화">
        <h2>최근 대화</h2>
        {!recent.length && <p className="history-empty">시작한 대화가 여기에 표시됩니다.</p>}
        {recent.map(thread => {
          const title = conversationTitle(thread);
          const agentName = thread.agentId === null ? DEFAULT_AGENT.name : agents.find(item => item.agent_id === thread.agentId)?.name ?? '사용할 수 없는 에이전트';
          const selected = page === 'chat' && currentThreadId === thread.id;
          return <div key={thread.id} className={'history-row' + (selected ? ' selected' : '')}>
            <button type="button" disabled={chatBusy || deletingThread === thread.id}
              className={selected ? 'selected' : ''} aria-current={selected ? 'true' : undefined}
              aria-label={`대화 열기: ${title}`} title={`${title} · ${agentName}`}
              onClick={() => openSavedConversation(thread)}>
              <MessageSquare size={15} />
              <span><strong>{title}</strong><small>{agentName}</small></span>
            </button>
            <button type="button" className="history-delete" disabled={chatBusy || !!deletingThread}
              aria-label={`대화 지우기: ${title}`} title="대화 지우기" onClick={() => deleteConversation(thread)}>
              {deletingThread === thread.id ? <LoaderCircle size={14} className="spin" /> : <Trash2 size={14} />}</button>
          </div>;
        })}
      </section>
      {session && <div className="sidebar-bottom user-card">
        <span className="avatar" aria-hidden="true">{(me?.attributes?.name ?? session.userId).charAt(0)}</span>
        <span className="user-meta"><strong>{me?.attributes?.name ?? session.userId}</strong>
          <small>{me ? [session.userId, me.roles.map(role => ROLE[role] ?? role).join(', '), me.attributes?.dept_id !== undefined && `부서 ${me.attributes.dept_id}`].filter(Boolean).join(' · ') : session.userId}</small></span>
        <button type="button" className="logout-button" disabled={chatBusy} onClick={session.logout} title="로그아웃" aria-label="로그아웃"><LogOut size={16} /></button>
      </div>}
      {!session && <div className="sidebar-bottom user-switch">
        <span className="avatar" aria-hidden="true"><UserRound size={16} /></span>
        <select aria-label="개발용 사용자" disabled={chatBusy} value={user} onChange={event => setUser(event.target.value)}>
          {(users.length ? users : [{ user_id: user, roles: [] }]).map(u => <option key={u.user_id} value={u.user_id}>
            {u.user_id} · {u.roles.map(role => ROLE[role] ?? role).join(', ')}{u.attributes?.dept_id !== undefined ? ` · 부서 ${u.attributes.dept_id}` : ''}
          </option>)}
        </select>
      </div>}
    </aside>
    <main className={'workspace ' + (page === 'chat' ? 'chat-workspace' : '')}>
      <header className="topbar"><span className="breadcrumb"><route.icon size={16} />{route.name}</span>
        <span className="topbar-right">
          {me && <span className="whoami"><span className="status-dot" />{me.attributes?.name ?? me.user_id} · {me.roles.map(role => ROLE[role] ?? role).join(', ')}</span>}
          <button type="button" className="theme-toggle" onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
            aria-label={theme === 'dark' ? '라이트 모드로' : '다크 모드로'} title={theme === 'dark' ? '라이트 모드로' : '다크 모드로'}>
            {theme === 'dark' ? <Sun size={16} /> : <Moon size={16} />}</button>
        </span></header>
      {notice && page === 'chat' && <p role="status" className={'notice floating ' + notice.tone}>{notice.text}<button className="icon" onClick={() => setNotice(null)} aria-label="닫기"><X size={14} /></button></p>}
      {page === 'chat' && viewing ? <ReadOnlyConversation thread={viewing}
        agentName={viewingAgent?.name ?? '사용할 수 없는 에이전트'}
        reason={viewingAgent ? '이 대화의 에이전트가 비활성화되었습니다.' : '이 대화의 에이전트가 삭제되었거나 사용 권한이 없습니다.'}
        onLeave={() => newConversation(activeId)} />
      : page === 'chat' ? <ChatPage key={`${user}:${chatView}`} user={user} tools={tools} agents={agents} activeId={activeId} setActiveId={setActiveId} onBusyChange={setChatBusy}
        chatHistory={chatHistory} changeConversation={changeConversation} newConversation={newConversation} notify={notify} />
      : <div className="page">
        <div className="page-heading"><div className="hero-icon small"><route.icon size={25} /></div><h1>{route.name}</h1>
          <p>{{
            create: '필요한 역할을 설명하면 LLM이 이름, 프롬프트, 도구를 골라 초안을 만듭니다.',
            agents: '내가 만든 에이전트와 공유받은 에이전트를 관리하세요. 부서장은 부서원의 에이전트를 비활성화할 수 있습니다.',
            inbox: '에이전트가 보낸 알림입니다. 본인 알림만 보입니다.',
            schedules: '평일 정해진 시각에 도는 작업입니다. 지금 실행으로 바로 돌릴 수 있습니다.',
            documents: '에이전트가 양식으로 만든 문서입니다. 본인 문서만 보이고, PDF·DOCX 중 골라 받습니다.',
            shares: '부서·전사 공유는 바로 위 관리자가 승인해야 적용됩니다.',
            templates: '관리자가 미리 만든 업무 에이전트로 시작하세요. 고른 뒤 내용을 고쳐 내 에이전트로 저장합니다.',
            tools: '관리자가 등록한 도구입니다. 에이전트를 만들 때 이 중에서 고릅니다.',
            knowledge: '에이전트가 검색해서 답할 수 있는 사내 문서입니다. 지금은 전사 공개 문서만 보여 줍니다.',
          }[page]}</p>
        </div>
        {notice && <p role="status" className={'notice ' + notice.tone}>{notice.text}</p>}
        {page === 'create' && <CreatePage user={user} tools={tools} draft={draft} setDraft={setDraft} notify={notify}
          onCreated={agent => { refreshAgents(); setEditingId(agent.agent_id); location.hash = 'agents'; setTimeout(() => notify(`'${agent.name}' 에이전트를 만들었습니다.`, 'ok')); }} />}
        {page === 'agents' && <AgentsPage user={user} agents={agents} tools={tools} editingId={editingId} setEditingId={setEditingId}
          refresh={refreshAgents} openChat={openChat} notify={notify} />}
        {page === 'inbox' && <InboxPage user={user} notify={notify} onChange={setUnread} />}
        {page === 'schedules' && <SchedulesPage user={user} notify={notify} />}
        {page === 'documents' && <DocumentsPage user={user} notify={notify} />}
        {page === 'shares' && <SharesPage user={user} notify={notify} refreshAgents={refreshAgents} />}
        {page === 'tools' && <>
          <ToolCatalog tools={tools} effectLabel={EFFECT} Badge={Badge} />
          {me?.roles.includes('admin') && <p className="muted">도구 등록(MCP 연결)은 관리자 기능으로 준비 중입니다.</p>}
        </>}
        {page === 'templates' && <TemplatesPage user={user} me={me} tools={tools} setDraft={setDraft} notify={notify} />}
        {page === 'knowledge' && <KnowledgePage key={user} load={loadKnowledge} notify={notify} />}
      </div>}
    </main>
  </div>;
}

// 서버의 로그인 방식을 먼저 묻는다: dev 면 지금처럼 사용자 선택, session 이면 로그인 화면 → 로그인한 사람으로 App
function Root() {
  const [auth, setAuth] = useState({ mode: null, userId: null, message: '' });
  const [theme, setTheme] = useState(readTheme);
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem(themeKey, theme); } catch { /* 저장 못 해도 동작 */ }
  }, [theme]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const { mode } = await (await fetch('/api/auth/mode')).json();
        if (mode !== 'session') { if (!cancelled) setAuth({ mode: 'dev', userId: null, message: '' }); return; }
        const res = await fetch('/api/me');  // 쿠키가 살아 있으면 다시 로그인하지 않는다
        const me = res.ok ? await res.json() : null;
        if (!cancelled) setAuth({ mode, userId: me?.user_id ?? null, message: '' });
      } catch {
        if (!cancelled) setAuth({ mode: 'dev', userId: null, message: '' });  // 서버가 꺼져 있으면 기존 화면이 오류를 보여 준다
      }
    })();
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (auth.mode !== 'session' || !auth.userId) return undefined;
    const expired = () => setAuth({ mode: 'session', userId: null, message: '로그인이 만료되었습니다. 다시 로그인해 주세요.' });
    addEventListener('auth-expired', expired);
    return () => removeEventListener('auth-expired', expired);
  }, [auth.mode, auth.userId]);

  async function logout() {
    await fetch('/api/auth/logout', { method: 'POST' }).catch(() => null);
    setAuth({ mode: 'session', userId: null, message: '로그아웃했습니다.', tone: 'ok' });
  }

  if (!auth.mode) return null;
  if (auth.mode === 'session' && !auth.userId) {
    return <LoginScreen key={auth.message} message={auth.message} tone={auth.tone} theme={theme} setTheme={setTheme}
      onLogin={user => setAuth({ mode: 'session', userId: user.user_id, message: '' })} />;
  }
  return <App key={auth.userId ?? 'dev'} theme={theme} setTheme={setTheme} session={auth.mode === 'session' ? { userId: auth.userId, logout } : null} />;
}

createRoot(document.getElementById('root')).render(<Root />);
