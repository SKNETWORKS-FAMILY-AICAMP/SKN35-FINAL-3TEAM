import test from 'node:test';
import assert from 'node:assert/strict';
import {
  emptyChatHistory, currentConversation, updateConversation, startNewConversation,
  openConversation, conversationTitle, recentConversations,
} from '../src/chat-history.js';

const userMessage = content => ({ role: 'user', content });
const addMessage = content => current => ({ ...current, messages: [...current.messages, userMessage(content)] });

test('new conversations preserve previous sessions and can resume independently', () => {
  let state = updateConversation(emptyChatHistory(), 'agent-a', addMessage('첫 대화'));
  state = updateConversation(state, 'agent-a', current => ({ ...current, sessionId: 'session-a' }));
  const first = currentConversation(state, 'agent-a');
  state = startNewConversation(state, 'agent-a');
  assert.equal(currentConversation(state, 'agent-a').messages.length, 0);
  assert.equal(recentConversations(state).length, 1);
  state = startNewConversation(state, 'agent-a');
  assert.equal(recentConversations(state).length, 1); // 빈 대화는 목록에 쌓지 않는다.
  state = updateConversation(state, 'agent-a', addMessage('두 번째 대화'));
  const secondId = currentConversation(state, 'agent-a').id;
  state = openConversation(state, first.id);
  assert.equal(currentConversation(state, 'agent-a').sessionId, 'session-a');
  state = updateConversation(state, 'agent-a', addMessage('첫 대화 이어쓰기'));
  assert.equal(state.threads[first.id].messages.length, 2);
  assert.deepEqual(state.threads[secondId].messages, [userMessage('두 번째 대화')]);
  assert.equal(recentConversations(state).length, 2);
});

test('agent conversations remain separate, including the default agent', () => {
  let state = updateConversation(emptyChatHistory(), null, addMessage('기본 대화'));
  const defaultId = currentConversation(state, null).id;
  state = updateConversation(state, 'agent-a', addMessage('에이전트 대화'));
  state = openConversation(state, defaultId);
  assert.equal(currentConversation(state, null).agentId, null);
  assert.equal(currentConversation(state, 'agent-a').messages[0].content, '에이전트 대화');
  assert.equal(openConversation(state, 'missing'), state);
});

test('recent list uses updated messages without duplicates and orders by latest activity', () => {
  const state = { threads: {
    a: { id: 'a', messages: [userMessage('  첫 질문\n내용  ')], updatedAt: 1 },
    b: { id: 'b', messages: [userMessage('다음 질문')], updatedAt: 2 },
    empty: { id: 'empty', messages: [], updatedAt: 3 },
  }, currentByAgent: {} };
  assert.deepEqual(recentConversations(state).map(thread => thread.id), ['b', 'a']);
  assert.equal(conversationTitle(state.threads.a), '첫 질문 내용');
  assert.equal(conversationTitle({ messages: [{ role: 'user', content: '', attachments: [{ filename: '보고서.hwpx' }] }] }), '보고서.hwpx');
});
