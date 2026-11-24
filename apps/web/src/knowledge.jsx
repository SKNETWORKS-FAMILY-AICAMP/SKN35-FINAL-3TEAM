import React, { useEffect, useState } from 'react';
import { BookOpen } from 'lucide-react';

// 사내 문서 목록 (서버 /api/knowledge/docs). 에이전트가 검색해서 답할 수 있는 문서가 무엇인지 보여 준다.
// 1단계: 전사 공개 문서만. 부서 전용 문서·문서 올리기·에이전트별 문서 연결은 아직 없다.
// '내 문서'(에이전트가 만든 PDF·DOCX) 와는 다른 화면이다.
export const KnowledgeIcon = BookOpen;

function formatDate(value) {
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? '-' : `${d.getFullYear()}.${String(d.getMonth() + 1).padStart(2, '0')}.${String(d.getDate()).padStart(2, '0')}`;
}

export function KnowledgePage({ load, notify }) {
  const [state, setState] = useState({ loading: true, enabled: true, docs: [] });

  useEffect(() => {
    let current = true;
    load()
      .then(data => { if (current) setState({ loading: false, enabled: data.enabled, docs: data.docs }); })
      .catch(error => { if (current) { setState({ loading: false, enabled: true, docs: [] }); notify(error.message); } });
    return () => { current = false; };
  }, [load, notify]);

  if (state.loading) return <p className="muted">불러오는 중…</p>;
  if (!state.enabled) return <p className="muted">팀 DB 문서를 쓰지 않는 설정입니다 (.env 의 DOC_STORE=db 로 켜면 보입니다).</p>;
  if (!state.docs.length) return <p className="muted">볼 수 있는 전사 문서가 없습니다.</p>;

  const total = state.docs.reduce((sum, doc) => sum + doc.chunks, 0);
  return <>
    <p className="muted">전사 공개 문서 {state.docs.length}개 · 검색 조각 {total.toLocaleString()}개</p>
    <ul className="doc-grid">{state.docs.map(doc => <li className="panel doc-card" key={doc.doc_id}>
      <span className="hero-icon small"><BookOpen size={20} aria-hidden="true" /></span>
      <div className="doc-body">
        <h2 title={doc.title}>{doc.title}</h2>
        <p className="doc-tags"><span className="badge dept">{doc.category}</span><span className="badge company">전사 공개</span></p>
        <p className="doc-file" title={doc.file_name}>{doc.file_name}</p>
        <p className="doc-meta"><span>조각 {doc.chunks.toLocaleString()}개</span><span>등록 {formatDate(doc.registered_at)}</span></p>
      </div>
    </li>)}</ul>
  </>;
}
