import React, { useCallback, useEffect, useState } from 'react';
import { Download, FileText, LoaderCircle, Trash2 } from 'lucide-react';
import { request } from './api';

// 서류가 나오는 모든 작업이 같은 내려받기 버튼을 쓴다.
// 규칙: 툴 결과(JSON)에 document_id 가 있으면 문서가 만들어진 것 → [PDF] [DOCX] 버튼.
// 변환은 서버(GET /api/documents/{id}/download?format=…)가 한다. LLM 을 부르지 않는다.
export const DOCUMENT_FORMATS = [
  { id: 'pdf', label: 'PDF' },
  { id: 'docx', label: 'DOCX' },
];

export function documentsFromEvents(events = []) {
  const found = [];
  for (const event of events) {
    if (!event.ok) continue;
    try {
      const data = JSON.parse(event.result);
      if (data?.document_id) found.push({ document_id: data.document_id, title: data.document_title ?? '문서' });
    } catch { /* JSON 이 아닌 결과 */ }
  }
  return found;
}

function savedName(response, fallback) {
  const header = response.headers.get('Content-Disposition') ?? '';
  const encoded = header.match(/filename\*=UTF-8''([^;]+)/i);
  return encoded ? decodeURIComponent(encoded[1]) : fallback;
}

async function download(user, document, format) {
  const response = await fetch(`/api/documents/${encodeURIComponent(document.document_id)}/download?format=${format}`, {
    headers: { 'X-User-Id': user },
  });
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    throw new Error(typeof data?.detail === 'string' ? data.detail : '문서를 받지 못했습니다.');
  }
  const url = URL.createObjectURL(await response.blob());
  const link = Object.assign(window.document.createElement('a'), { href: url, download: savedName(response, `${document.title}.${format}`) });
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function DocumentDownloads({ user, documents, notify }) {
  const [busy, setBusy] = useState('');
  if (!documents?.length) return null;
  async function get(document, format) {
    setBusy(document.document_id + format);
    try { await download(user, document, format); } catch (error) { notify?.(error.message); } finally { setBusy(''); }
  }
  return <div className="document-downloads">{documents.map(document => <div key={document.document_id} className="document-download">
    <FileText size={15} /><span className="document-title">{document.title}</span>
    {DOCUMENT_FORMATS.map(({ id, label }) => <button key={id} type="button" className="quiet" disabled={!!busy}
      onClick={() => get(document, id)}>
      {busy === document.document_id + id ? <LoaderCircle size={14} className="spin" /> : <Download size={14} />}{label}
    </button>)}
  </div>)}</div>;
}

export function DocumentsPage({ user, notify }) {
  const [items, setItems] = useState(null);
  const [deleting, setDeleting] = useState('');
  const load = useCallback(async () => {
    try { setItems(await request(user, '/documents')); } catch (error) { notify(error.message); setItems([]); }
  }, [user, notify]);
  useEffect(() => { load(); }, [load]);
  // 지우기: 내 문서함에서만 빠진다 (서버는 지운 시각만 남기고, 내려받은 기록은 그대로)
  async function remove(item) {
    if (!confirm(`"${item.title}" 문서를 내 문서함에서 지울까요?`)) return;
    setDeleting(item.document_id);
    try {
      await request(user, '/documents/' + encodeURIComponent(item.document_id), { method: 'DELETE' });
      setItems(previous => previous.filter(other => other.document_id !== item.document_id));
      notify('문서를 지웠습니다.', 'ok');
    } catch (error) {
      notify(error.message);
    } finally {
      setDeleting('');
    }
  }
  if (items === null) return <p className="muted">불러오는 중…</p>;
  if (!items.length) return <p className="muted">만든 문서가 없습니다. 채팅에서 "만기 대출 정리표" 에이전트에게 요청해 보세요.</p>;
  return <ul className="request-log">{items.map(item => <li key={item.document_id} className="panel inbox-item document-item">
    <DocumentDownloads user={user} documents={[item]} notify={notify} />
    <small className="muted">{new Date(item.created_at).toLocaleString()}</small>
    <button type="button" className="icon document-delete" disabled={!!deleting} onClick={() => remove(item)}
      aria-label={`${item.title} 지우기`} title="지우기">
      {deleting === item.document_id ? <LoaderCircle size={15} className="spin" /> : <Trash2 size={15} />}</button>
  </li>)}</ul>;
}
