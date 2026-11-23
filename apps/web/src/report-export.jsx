import React from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

// 모델 답변 대신 저장/열람 도구가 돌려준 원문만 출력한다.
export function savedReports(events = []) {
  const reports = new Map();
  for (const event of events ?? []) {
    if (!event.ok || !['save_morning_report', 'get_morning_report'].includes(event.tool_name)) continue;
    try {
      const data = JSON.parse(event.result);
      if (/^[0-9a-f]{32}$/.test(data.report_id) &&
        (isReportDocument(data.document) || (typeof data.markdown === 'string' && data.markdown.trim()))) {
        reports.set(data.report_id, data);
      }
    } catch { /* 오류 결과나 이전 형식에는 출력 버튼을 만들지 않는다. */ }
  }
  return [...reports.values()];
}

export function isReportDocument(document) {
  if (document?.schema_version !== 1 || !Array.isArray(document.blocks) || document.blocks.length > 200) return false;
  return document.blocks.every(block => {
    if (!block || typeof block !== 'object') return false;
    if (['paragraph', 'notice'].includes(block.type)) return typeof block.text === 'string';
    if (block.type === 'heading') return [1, 2, 3].includes(block.level) && typeof block.text === 'string';
    if (block.type === 'field') return typeof block.label === 'string' && typeof block.value === 'string';
    if (block.type === 'list') return Array.isArray(block.items) && block.items.every(item => typeof item === 'string');
    return block.type === 'table' && Array.isArray(block.columns) && block.columns.length > 0 &&
      block.columns.every(cell => typeof cell === 'string') && Array.isArray(block.rows) &&
      block.rows.every(row => Array.isArray(row) && row.length === block.columns.length && row.every(cell => typeof cell === 'string'));
  });
}

export function ReportContent({ document, markdown }) {
  if (isReportDocument(document)) return <div className="answer-markdown structured-report">{document.blocks.map((block, index) => {
    if (block.type === 'heading') return React.createElement('h' + block.level, { key: index }, block.text);
    if (block.type === 'paragraph') return <p key={index}>{block.text}</p>;
    if (block.type === 'notice') return <blockquote key={index}><p>{block.text}</p></blockquote>;
    if (block.type === 'field') return <p key={index}><strong>{block.label}{block.label ? ': ' : ''}</strong>{block.value}</p>;
    if (block.type === 'list') return <ul key={index}>{block.items.map((item, i) => <li key={i}>{item}</li>)}</ul>;
    return <div className="report-table" key={index}><table><thead><tr>{block.columns.map((cell, i) => <th scope="col" key={i}>{cell}</th>)}</tr></thead>
      <tbody>{block.rows.map((row, i) => <tr key={i}>{row.map((cell, j) => <td key={j}>{cell}</td>)}</tr>)}</tbody></table></div>;
  })}</div>;
  // 과거 대화/보고서만 Markdown으로 표시한다.
  return <div className="answer-markdown"><ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml
    components={{ img: ({ alt }) => <span>[이미지: {alt || '첨부 이미지'}]</span> }}
  >{markdown}</ReactMarkdown></div>;
}

// 내려받기(PDF·DOCX)는 다른 서류와 같은 버튼을 쓴다: documents.jsx 의 DocumentDownloads.
// 서버가 저장된 문서를 id 로 찾아 변환한다 (브라우저 인쇄 창·브라우저가 보낸 JSON 변환은 쓰지 않는다).
