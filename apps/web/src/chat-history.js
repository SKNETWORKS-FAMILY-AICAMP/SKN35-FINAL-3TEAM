// 세션 ID는 서버와 대화를 이어갈 때, id는 화면에서 대화를 구별할 때 사용한다.
export function emptyChatHistory() {
  return { threads: {}, currentByAgent: {} };
}

const agentKey = agentId => agentId ?? 'default';

export function currentConversation(state, agentId) {
  return state.threads[state.currentByAgent[agentKey(agentId)]] ?? { sessionId: null, messages: [] };
}

export function updateConversation(state, agentId, change) {
  const current = currentConversation(state, agentId);
  const changed = change(current);
  const id = current.id ?? crypto.randomUUID();
  return {
    threads: {
      ...state.threads,
      [id]: { ...changed, id, agentId, updatedAt: Date.now() },
    },
    currentByAgent: { ...state.currentByAgent, [agentKey(agentId)]: id },
  };
}

export function startNewConversation(state, agentId) {
  // 기존 대화는 남겨두고, 다음 메시지를 보낼 때 새 대화를 만든다.
  return { ...state, currentByAgent: { ...state.currentByAgent, [agentKey(agentId)]: null } };
}

export function openConversation(state, id) {
  const thread = state.threads[id];
  if (!thread) return state;
  return { ...state, currentByAgent: { ...state.currentByAgent, [agentKey(thread.agentId)]: id } };
}

// 대화 지우기: 목록에서 빼고, 그 대화를 이어 쓰던 에이전트는 다음에 새 대화로 시작한다.
export function removeConversation(state, id) {
  if (!state.threads[id]) return state;
  const { [id]: _removed, ...threads } = state.threads;
  const currentByAgent = Object.fromEntries(Object.entries(state.currentByAgent).map(([agent, current]) => [agent, current === id ? null : current]));
  return { threads, currentByAgent };
}

export function conversationTitle(thread) {
  const message = thread.messages.find(item => item.role === 'user');
  return message?.content?.trim().replace(/\s+/g, ' ') || message?.attachments?.[0]?.filename || thread.title || '새 대화';
}

export function recentConversations(state) {
  return Object.values(state.threads)
    .filter(thread => thread.messages.length > 0 || thread.saved)
    .sort((a, b) => b.updatedAt - a.updatedAt);
}

// ── 서버에 저장된 대화 (GET /api/chat/sessions) ──
// 목록만 먼저 받아 두고(saved: true, 본문 없음), 누를 때 본문을 불러온다(loadConversation).

export function addSavedConversations(state, list) {
  const known = new Set(Object.values(state.threads).map(thread => thread.sessionId).filter(Boolean));
  const threads = { ...state.threads };
  list.forEach((item, index) => {
    if (known.has(item.session_id)) return;  // 목록을 받기 전에 이미 이어 쓰고 있는 대화
    threads[item.session_id] = {
      id: item.session_id, sessionId: item.session_id, agentId: item.agent_id, title: item.title,
      updatedAt: Date.parse(item.updated_at) || -index,  // 시각이 없으면(메모리 저장소) 받은 순서대로
      messages: [], saved: true,
    };
  });
  return { ...state, threads };
}

export function loadConversation(state, id, messages) {
  const thread = state.threads[id];
  if (!thread) return state;
  return { ...state, threads: { ...state.threads, [id]: { ...thread, messages, saved: false } } };
}

// 서버의 질문·답 묶음(GET /api/chat/sessions/{id}/turns)을 화면 메시지 모양으로 바꾼다
export function turnsToMessages(turns, agentName) {
  return turns.flatMap(turn => [
    { role: 'user', content: turn.question, attachments: turn.attachments },
    {
      role: 'agent', name: agentName, content: turn.message || '이 요청은 답을 끝까지 만들지 못했습니다.',
      events: turn.tool_events, denial: turn.denial ?? null, sources: turn.sources,
    },
  ]);
}
