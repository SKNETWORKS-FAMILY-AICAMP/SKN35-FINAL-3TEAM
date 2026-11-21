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

export function conversationTitle(thread) {
  const message = thread.messages.find(item => item.role === 'user');
  return message?.content?.trim().replace(/\s+/g, ' ') || message?.attachments?.[0]?.filename || '새 대화';
}

export function recentConversations(state) {
  return Object.values(state.threads)
    .filter(thread => thread.messages.length > 0)
    .sort((a, b) => b.updatedAt - a.updatedAt);
}
