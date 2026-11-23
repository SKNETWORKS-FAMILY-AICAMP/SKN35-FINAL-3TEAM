import React, { useState } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { FileDown } from 'lucide-react';

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

export function reportDocument(report) {
  const title = `${report.template?.name || '시장 보고서'}_${report.base_date}_${report.report_id.slice(0, 8)}`;
  return '<!doctype html>' + renderToStaticMarkup(<html lang="ko"><head>
    <meta charSet="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" />
    <meta httpEquiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; font-src 'none'; img-src 'none'" />
    <title>{title}</title>
    <style>{`
      @page { size: A4; margin: 18mm 16mm; }
      * { box-sizing: border-box; }
      body { margin: 0; color: #172033; background: white; font: 9.5pt/1.6 'Malgun Gothic', 'Apple SD Gothic Neo', sans-serif; overflow-wrap: anywhere; }
      h1 { font-size: 19pt; border-bottom: 2px solid #233b61; padding-bottom: 8pt; }
      h2 { font-size: 13pt; margin: 15pt 0 7pt; }
      h3 { font-size: 11pt; margin: 14pt 0 6pt; }
      h1, h2, h3, h4, h5, h6 { color: #233b61; line-height: 1.45; break-after: avoid; }
      p { display: block; margin: 7pt 0; orphans: 3; widows: 3; }
      table { width: 100%; border-collapse: collapse; font-size: 9pt; margin: 12pt 0; table-layout: fixed; }
      th, td { border: 1px solid #cbd3df; padding: 5pt; vertical-align: top; }
      th { background: #eef2f7; font-weight: 700; }
      thead { display: table-header-group; }
      tr { break-inside: avoid; }
      blockquote { margin: 10pt 0; border-left: 3px solid #7186a3; padding: 4pt 10pt; color: #45546a; }
      ul, ol { padding-left: 18pt; }
      pre { white-space: pre-wrap; border: 1px solid #cbd3df; padding: 8pt; }
      code { font-family: Consolas, 'Malgun Gothic', monospace; }
      a { color: #233b61; text-decoration: underline; }
      hr { border: 0; border-top: 1px solid #cbd3df; margin: 16pt 0; }
      * { print-color-adjust: exact; -webkit-print-color-adjust: exact; }
    `}</style>
  </head><body><main><ReportContent document={report.document} markdown={report.markdown} /></main></body></html>);
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function ReportExport({ report, user }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const basename = `report_${report.base_date}_${report.report_id.slice(0, 8)}`;
  async function downloadWord() {
    setBusy(true);
    setError('');
    try {
      const response = await fetch('/api/reports/export/docx', { method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-User-Id': user }, body: JSON.stringify(report.document) });
      if (!response.ok) throw new Error('Word 파일을 만들지 못했습니다. 로그인 상태와 FastAPI 서버를 확인해 주세요.');
      downloadBlob(await response.blob(), basename + '.docx');
    } catch (error) {
      setError(error.message || 'Word 파일을 내려받지 못했습니다.');
    } finally { setBusy(false); }
  }
  function printReport() {
    setBusy(true);
    setError('');
    const frame = document.createElement('iframe');
    frame.title = '보고서 PDF 출력';
    frame.setAttribute('sandbox', 'allow-same-origin allow-modals');
    frame.style.cssText = 'position:fixed;left:-10000px;top:0;width:210mm;height:297mm;border:0';
    let cleanupTimer;
    const cleanup = () => { clearTimeout(cleanupTimer); frame.remove(); setBusy(false); };
    frame.onload = async () => {
      try {
        await frame.contentDocument.fonts.ready;
        frame.contentWindow.addEventListener('afterprint', cleanup, { once: true });
        frame.contentWindow.focus();
        frame.contentWindow.print();
        // 일부 브라우저는 afterprint를 보내지 않는다. 저장 여부로 표시하지 않는다.
        if (frame.isConnected) cleanupTimer = setTimeout(cleanup, 120000);
        setBusy(false);
      } catch {
        cleanup();
        setError('인쇄 창을 열지 못했습니다. 브라우저의 인쇄 허용 설정을 확인해 주세요.');
      }
    };
    frame.srcdoc = reportDocument(report);
    document.body.appendChild(frame);
  }
  return <div className="report-export">
    <button type="button" className="quiet" disabled={busy} onClick={printReport}><FileDown size={16} />{busy ? '출력 준비 중…' : 'PDF로 저장'}</button>
    {isReportDocument(report.document) && <>
      <button type="button" className="quiet" disabled={busy} onClick={downloadWord}><FileDown size={16} />Word로 저장</button>
      <button type="button" className="quiet" onClick={() => downloadBlob(new Blob([JSON.stringify(report.document, null, 2)],
        { type: 'application/json;charset=utf-8' }), basename + '.json')}><FileDown size={16} />JSON 원본</button>
    </>}
    <small>인쇄 창에서 대상을 ‘PDF로 저장’으로 선택하세요.</small>
    {error && <span role="alert">{error}</span>}
  </div>;
}
