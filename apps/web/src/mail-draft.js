// 제목이 포함된 초안은 제목/본문으로 나누고, 일반 답변도 편집해서 보낼 수 있다.
export function draftFromMessage(text) {
  const lines = String(text || '').split('\n');
  const titleIndex = lines.findIndex(line => /^\s*(?:\*\*)?제목\s*(?:\*\*)?\s*[:：]/.test(line));
  if (titleIndex < 0) return { subject: '', body: String(text || '').trim() };
  const subject = lines[titleIndex].replace(/^\s*(?:\*\*)?제목\s*(?:\*\*)?\s*[:：]\s*/, '').replace(/\*\*/g, '').trim();
  const body = lines.slice(titleIndex + 1).join('\n').replace(/^\s*(?:\*\*)?본문\s*(?:\*\*)?\s*[:：]\s*/, '').trim();
  return { subject, body };
}
