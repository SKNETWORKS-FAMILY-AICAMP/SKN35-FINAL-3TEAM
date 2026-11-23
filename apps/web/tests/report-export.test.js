import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'vite';

const server = await createServer({ server: { middlewareMode: true }, appType: 'custom' });
const { savedReports, reportDocument, isReportDocument } = await server.ssrLoadModule('/src/report-export.jsx');
await server.close();

const report = { report_id: 'a'.repeat(32), base_date: '2026-11-18', template: { name: '테스트 보고서' },
  markdown: '# 환율 보고서\n\n| 통화 | 환율 |\n| --- | --- |\n| 달러 | 1,338.80 |\n\n출처: 한국은행 · 기록 재생' };

test('only successful saved report originals are exported, with duplicates removed', () => {
  const saved = { ok: true, tool_name: 'save_morning_report', result: JSON.stringify(report) };
  assert.deepEqual(savedReports([saved, { ...saved, tool_name: 'get_morning_report' },
    { ...saved, ok: false }, { ...saved, tool_name: 'market_snapshot' },
    { ...saved, result: 'invalid JSON' }, { ...saved, result: '{}' }]), [report]);
  assert.deepEqual(savedReports(null), []);
});

test('print document renders Korean headings and GFM tables and retains sources', () => {
  const html = reportDocument(report);
  assert.match(html, /<html lang="ko">/);
  assert.match(html, /<h1>환율 보고서<\/h1>/);
  assert.match(html, /<table>/);
  assert.match(html, /1,338\.80/);
  assert.match(html, /한국은행 · 기록 재생/);
  assert.match(html, /size: A4/);
});

test('document text and titles cannot inject HTML, scripts or remote images', () => {
  const html = reportDocument({ ...report, template: { name: '</title><script>alert(1)</script>' },
    markdown: '<script>alert(2)</script>\n\n![기밀](https://example.com/track.png)\n\n[클릭](javascript:alert)' });
  assert.doesNotMatch(html, /<script|<img|href="javascript:/);
  assert.match(html, /&lt;\/title&gt;/);
  assert.match(html, /이미지: 기밀/);
});

const structured = { schema_version: 1, title: '환율 보고서', base_date: report.base_date, report_id: report.report_id,
  template: { id: 'fx-brief', name: '환율 보고서', version: 2 }, blocks: [
    { type: 'heading', level: 1, text: '환율 보고서' },
    { type: 'field', label: '기준일', value: report.base_date },
    { type: 'paragraph', text: '요약 본문 <script>alert(1)</script> **별표도 원문 그대로**' },
    { type: 'table', columns: ['통화', '환율'], rows: [['USD', '1,338.80']] },
    { type: 'list', items: ['출처: 한국은행', '기록 데이터 재생'] } ] };

test('JSON-only reports render directly without a Markdown intermediate', () => {
  const jsonReport = { ...report, markdown: undefined, document: structured };
  assert.equal(savedReports([{ ok: true, tool_name: 'save_morning_report', result: JSON.stringify(jsonReport) }]).length, 1);
  const html = reportDocument({ ...jsonReport, markdown: '사용하면 안 되는 이전 표시 내용' });
  assert.match(html, /<h1>환율 보고서<\/h1>/);
  assert.match(html, /<td>1,338.80<\/td>/);
  assert.match(html, /기록 데이터 재생/);
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /\*\*별표도 원문 그대로\*\*/);
  assert.doesNotMatch(html, /사용하면 안 되는|<script>/);
});

test('invalid structured tables and unknown schema versions are rejected', () => {
  assert.equal(isReportDocument({ ...structured, schema_version: 2 }), false);
  assert.equal(isReportDocument({ ...structured, blocks: [{ type: 'table', columns: ['a', 'b'], rows: [['one']] }] }), false);
  assert.equal(isReportDocument({ ...structured, blocks: [{ type: 'html', text: '<script>' }] }), false);
});
