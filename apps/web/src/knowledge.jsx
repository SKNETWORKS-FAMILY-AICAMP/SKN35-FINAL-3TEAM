import React, { useEffect, useState } from 'react';
import { BookOpen } from 'lucide-react';

// 사내 문서 목록 (서버 /api/knowledge/docs). 에이전트가 나 대신 찾아올 수 있는 문서 = 여기서 보이는 문서.
//  - 직원(부서장 포함): 전사 공개 + 내 부서 전용
//  - 관리자: 문서 관리용으로 목록 전체. 다른 부서 전용 문서는 '목록만'(내용은 에이전트 검색에 안 나옴)
// 문서 올리기·에이전트별 문서 연결은 아직 없다.
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
      .then(data => { if (current) setState({ loading: false, enabled: data.enabled, docs: data.docs, admin_view: Boolean(data.admin_view) }); })
      .catch(error => { if (current) { setState({ loading: false, enabled: true, docs: [] }); notify(error.message); } });
    return () => { current = false; };
  }, [load, notify]);

  if (state.loading) return <p className="muted">불러오는 중…</p>;
  if (!state.enabled) return <p className="muted">팀 DB 문서를 쓰지 않는 설정입니다 (.env 의 DOC_STORE=db 로 켜면 보입니다).</p>;
  if (!state.docs.length) return <p className="muted">볼 수 있는 문서가 없습니다.</p>;

  const usable = state.docs.filter(doc => doc.searchable !== false);
  const company = usable.filter(doc => doc.scope !== 'dept').length;
  const total = usable.reduce((sum, doc) => sum + doc.chunks, 0);
  return <>
    <p className="muted">
      {state.admin_view ? `관리자 전체 목록 ${state.docs.length}개 · ` : ''}
      에이전트가 찾아올 수 있는 문서 {usable.length}개 (전사 공개 {company} · 우리 부서 {usable.length - company}) · 검색 조각 {total.toLocaleString()}개
    </p>
    {state.admin_view && <p className="muted">'목록만' 표시는 다른 부서 전용 문서입니다. 관리를 위해 목록에는 보이지만, 내용은 에이전트 검색에 나오지 않습니다.</p>}
    <ul className="tool-catalog">{state.docs.map(doc => <li className="panel catalog-tool" key={doc.doc_id}>
      <span className="hero-icon small"><BookOpen size={21} aria-hidden="true" /></span>
      <div>
        <h2>{doc.title}<span style={tag}>{doc.category}</span>
          <span style={tag}>{doc.scope === 'dept' ? (doc.dept_name || `부서 ${doc.dept_id}`) + ' 전용' : '전사 공개'}</span>
          {doc.searchable === false && <span style={{ ...tag, opacity: .7 }} title="다른 부서 전용 문서라 내용은 에이전트 검색에 나오지 않습니다">목록만</span>}</h2>
        <p>{doc.file_name} · 조각 {doc.chunks}개 · 등록 {new Date(doc.registered_at).toLocaleDateString()}</p>
      </div>
    </li>)}</ul>
  </>;
}
