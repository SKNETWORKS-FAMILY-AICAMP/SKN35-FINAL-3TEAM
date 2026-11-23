import test from 'node:test';
import assert from 'node:assert/strict';
import { failedToolSummary } from '../src/tool-status.js';

test('approval rejection, approval timeout, and policy denial have distinct summaries', () => {
  const event = { call_id: 'c1', result: 'error: permission denied: reason', ok: false };
  assert.equal(failedToolSummary(event, { call_id: 'c1', kind: 'rejected' }), '승인을 거절해 실행하지 않음');
  assert.equal(failedToolSummary(event, { call_id: 'c1', kind: 'timeout' }), '승인 시간이 지나 실행하지 않음');
  assert.equal(failedToolSummary(event, { call_id: 'c1', kind: 'deny' }), '권한이 없어 실행하지 않음');
});

test('denial only describes its own call; skipped and other failures keep their meaning', () => {
  const denial = { call_id: 'blocked', kind: 'rejected' };
  assert.equal(failedToolSummary({ call_id: 'later', result: 'error: not executed: skipped' }, denial), '앞선 작업이 중단되어 실행하지 않음');
  assert.equal(failedToolSummary({ call_id: 'earlier', result: 'error: 파일을 읽지 못했습니다.' }, denial), '파일을 읽지 못했습니다.');
  assert.equal(failedToolSummary({ call_id: 'old', result: 'error: permission not approved (user rejected): reason' }), '승인을 거절해 실행하지 않음');
});
