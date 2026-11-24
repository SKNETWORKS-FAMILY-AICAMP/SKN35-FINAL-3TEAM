import React, { useEffect, useState } from 'react';
import { BookOpen } from 'lucide-react';

// 사내 문서 목록 (서버 /api/knowledge/docs). 에이전트가 검색해서 답할 수 있는 문서가 무엇인지 보여 준다.
// 1단계: 전사 공개 문서만. 부서 전용 문서·문서 올리기·에이전트별 문서 연결은 아직 없다.
// '내 문서'(에이전트가 만든 PDF·DOCX) 와는 다른 화면이다.
export const KnowledgeIcon = BookOpen;

// 앱에 공통 .badge 모양이 없어서 이 화면 안에서만 쓴다 (style.css 는 다른 사람 작업과 겹치기 쉬워 건드리지 않음)
const tag = {
  display: 'inline-block', marginLeft: 6, padding: '1px 7px', borderRadius: 999, verticalAlign: 'middle',
  border: '1px solid var(--accent-line, #2c3f5e)', color: 'var(--accent-ink, #9ec1ff)',
  background: 'var(--accent-soft, transparent)', fontSize: '.75rem', fontWeight: 500,
};

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
    <ul className="tool-catalog">{state.docs.map(doc => <li className="panel catalog-tool" key={doc.doc_id}>
      <span className="hero-icon small"><BookOpen size={21} aria-hidden="true" /></span>
      <div>
        <h2>{doc.title}<span style={tag}>{doc.category}</span><span style={tag}>전사 공개</span></h2>
        <p>{doc.file_name} · 조각 {doc.chunks}개 · 등록 {new Date(doc.registered_at).toLocaleDateString()}</p>
      </div>
    </li>)}</ul>
  </>;
}
