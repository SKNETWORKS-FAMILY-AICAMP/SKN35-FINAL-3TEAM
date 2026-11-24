import test from 'node:test';
import assert from 'node:assert/strict';
import { draftFromMessage } from '../src/mail-draft.js';

test('메일 초안의 제목과 본문을 나눈다', () => {
  assert.deepEqual(draftFromMessage('제목: 업무 안내\n\n안녕하세요.\n검토 부탁드립니다.'),
    { subject: '업무 안내', body: '안녕하세요.\n검토 부탁드립니다.' });
  assert.deepEqual(draftFromMessage('**제목:** 업무 안내\n본문: 안녕하세요.'),
    { subject: '업무 안내', body: '안녕하세요.' });
});

test('제목 없는 답변은 사용자가 제목을 입력한다', () => {
  assert.deepEqual(draftFromMessage('문서 요약입니다.'), { subject: '', body: '문서 요약입니다.' });
});
