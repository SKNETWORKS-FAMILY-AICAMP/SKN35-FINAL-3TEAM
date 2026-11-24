import React from 'react';
import './login-flow.css';

// 로그인 화면 왼쪽: 요청 하나가 플랫폼 안에서 지나가는 길을 노드 그림으로 보여 준다.
// - 불 켜진 단계가 왼쪽→오른쪽으로 차례로 옮겨 가고, 선 위로 빛 알갱이가 흐른다
// - 가운데 허브(Agent Argus)는 보라 그라데이션·궤도로 따로 꾸민다 (늘 켜져 있음). 로고는 미정이라 아직 안 넣음
// - 모든 단계가 아래 "감사 로그" 줄로 기록된다
// CSS·SVG 애니메이션만 쓴다. '움직임 줄이기' 설정이면 멈추고 그림만 보인다.
// 그림이라 화면 읽기 프로그램에는 aria-label 한 줄로 순서를 알려 준다.

const W = 134;          // 노드 폭
const H = 38;           // 노드 높이
const HUB_W = 188;      // 허브 폭
const HUB_H = 58;       // 허브 높이
const AUDIT_Y = 382;    // 감사 로그 줄 높이
const VIEW_W = 852;
const CYCLE = 8;        // 한 바퀴(초)

const NODES = [
  // id, x(중심), y(중심), 이름, 아래 설명, 불 켜지는 때(초). 특정 업종이 아니라 어느 회사에나 맞는 말로
  { id: 'login', x: 67, y: 180, label: '로그인', sub: '사내 계정 · 세션', at: 0 },
  { id: 'abac', x: 210, y: 180, label: '권한 엔진', sub: '부서 · 역할 · 보안등급', at: 0.9 },
  { id: 'hub', x: 404, y: 180, label: 'Agent Argus', sub: 'AI 오케스트레이터', at: 1.8, hub: true },
  { id: 'rag', x: 614, y: 40, label: '사내 문서 검색', sub: '규정 · 매뉴얼 RAG', at: 2.8 },
  { id: 'core', x: 614, y: 110, label: '업무 시스템 조회', sub: 'ERP · CRM 연동', at: 3.1 },
  { id: 'market', x: 614, y: 180, label: '외부 데이터', sub: '공공 API · 지표', at: 3.4 },
  { id: 'doc', x: 614, y: 250, label: '문서 생성', sub: '보고서 · PDF · DOCX', at: 3.7 },
  { id: 'mail', x: 614, y: 320, label: '메일 · 알림', sub: '승인 후 발송', at: 4.0 },
  { id: 'verify', x: 782, y: 180, label: '답변 검증', sub: '근거 · 숫자 대조', at: 5.0 },
];
const BRANCHES = ['rag', 'core', 'market', 'doc', 'mail'];
const DROP_SKIP = ['rag', 'core', 'market', 'doc'];   // 아래에 다른 노드가 있는 가지는 기록 선을 생략 (가운데를 뚫지 않게)
const EDGES = [
  ['login', 'abac'], ['abac', 'hub'],
  ...BRANCHES.map(b => ['hub', b]),
  ...BRANCHES.map(b => [b, 'verify']),
];

const half = n => (n.hub ? HUB_W : W) / 2;

function edgePath(a, b) {
  const x1 = a.x + half(a), x2 = b.x - half(b), mid = (x1 + x2) / 2;
  return `M${x1},${a.y} C${mid},${a.y} ${mid},${b.y} ${x2},${b.y}`;
}

// 허브 둘레 궤도 (타원). 빛 알갱이 두 개가 이 길을 따라 돈다
const ORBIT_RX = HUB_W / 2 + 22, ORBIT_RY = HUB_H / 2 + 14;
function orbitPath(cx, cy) {
  return `M${cx - ORBIT_RX},${cy} a${ORBIT_RX},${ORBIT_RY} 0 1,0 ${ORBIT_RX * 2},0 a${ORBIT_RX},${ORBIT_RY} 0 1,0 ${-ORBIT_RX * 2},0`;
}

function Hub({ n }) {
  const left = n.x - HUB_W / 2, top = n.y - HUB_H / 2, orbit = orbitPath(n.x, n.y);
  return <g className="lf-hub">
    <circle className="lf-hub-glow" cx={n.x} cy={n.y} r="104" fill="url(#lf-hub-glow)" />
    <path className="lf-orbit" d={orbit} />
    {[0, 3].map(delay => <circle key={delay} className="lf-orbit-dot" r="3.4">
      <animateMotion dur="6s" begin={`-${delay}s`} repeatCount="indefinite" path={orbit} />
    </circle>)}
    <rect className="lf-hub-ring" x={left - 7} y={top - 7} width={HUB_W + 14} height={HUB_H + 14} rx={(HUB_H + 14) / 2} />
    <rect className="lf-hub-body" x={left} y={top} width={HUB_W} height={HUB_H} rx={HUB_H / 2} />
    <rect className="lf-hub-shine" x={left + 10} y={top + 4} width={HUB_W - 20} height={HUB_H / 2 - 6} rx={(HUB_H / 2 - 6) / 2} />
    <text x={n.x} y={n.y - 1} textAnchor="middle" className="lf-hub-label">{n.label}</text>
    <text x={n.x} y={n.y + 15} textAnchor="middle" className="lf-hub-sub">{n.sub}</text>
  </g>;
}

export default function LoginFlow() {
  const byId = Object.fromEntries(NODES.map(n => [n.id, n]));
  return <figure className="login-flow">
    <svg viewBox={`0 0 ${VIEW_W} 412`} role="img" style={{ '--cycle': `${CYCLE}s` }}
      aria-label="처리 순서: 로그인, 권한 엔진, Agent Argus, 사내 문서 검색·업무 시스템 조회·외부 데이터·문서 생성·메일 알림, 답변 검증, 모든 단계 감사 로그 기록">
      <defs>
        <radialGradient id="lf-hub-glow">
          <stop offset="0%" stopColor="#a970ff" stopOpacity=".7" />
          <stop offset="55%" stopColor="#5e56f0" stopOpacity=".25" />
          <stop offset="100%" stopColor="#5e56f0" stopOpacity="0" />
        </radialGradient>
        <linearGradient id="lf-hub-fill" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#6d63ff" />
          <stop offset="55%" stopColor="#8b5cf6" />
          <stop offset="100%" stopColor="#b57bff" />
        </linearGradient>
        <linearGradient id="lf-hub-stroke" x1="0" x2="1">
          <stop offset="0%" stopColor="#5ee0b5" />
          <stop offset="100%" stopColor="#c4b5fd" />
        </linearGradient>
        <linearGradient id="lf-audit-sheen" x1="0" x2="1">
          <stop offset="0%" stopColor="#5ee0b5" stopOpacity="0" />
          <stop offset="50%" stopColor="#5ee0b5" stopOpacity=".9" />
          <stop offset="100%" stopColor="#5ee0b5" stopOpacity="0" />
        </linearGradient>
        <clipPath id="lf-audit-clip"><rect x="4" y={AUDIT_Y - 10} width={VIEW_W - 8} height="24" rx="12" /></clipPath>
      </defs>

      {/* 감사 로그 줄: 모든 노드에서 아래로 기록이 떨어진다 */}
      <g className="lf-audit">
        {NODES.filter(n => !DROP_SKIP.includes(n.id)).map(n => <line key={n.id} className="lf-drop" x1={n.x} x2={n.x}
          y1={n.y + (n.hub ? HUB_H / 2 + 16 : H / 2 + 20)} y2={AUDIT_Y - 10} style={{ '--at': `${n.at + 0.3}s` }} />)}
        <rect className="lf-audit-bar" x="4" y={AUDIT_Y - 10} width={VIEW_W - 8} height="24" rx="12" />
        <g clipPath="url(#lf-audit-clip)"><rect className="lf-audit-sheen" x="4" y={AUDIT_Y - 10} width="180" height="24" rx="12" fill="url(#lf-audit-sheen)" /></g>
        <text className="lf-audit-text" x={VIEW_W / 2} y={AUDIT_Y + 6} textAnchor="middle">감사 로그 · 누가 · 언제 · 무엇을 · 허용/거부 — 전 구간 기록</text>
      </g>

      {EDGES.map(([from, to]) => {
        const a = byId[from], b = byId[to], d = edgePath(a, b);
        const begin = `${a.at + 0.35}s`;
        return <g key={from + to} className="lf-edge" style={{ '--at': begin }}>
          <path className="lf-line" d={d} />
          <path className="lf-flow" d={d} />
          <circle className="lf-port" cx={a.x + half(a)} cy={a.y} r="2.6" />
          <circle className="lf-port" cx={b.x - half(b)} cy={b.y} r="2.6" />
          {/* 빛 알갱이: 앞 노드가 켜진 직후 선을 따라 한 번 지나간다 */}
          <circle className="lf-particle" r="3.2">
            <animateMotion dur={`${CYCLE}s`} begin={begin} repeatCount="indefinite" path={d}
              keyPoints="0;1;1" keyTimes="0;0.11;1" calcMode="linear" />
            <animate attributeName="opacity" dur={`${CYCLE}s`} begin={begin} repeatCount="indefinite"
              values="0;1;1;0;0" keyTimes="0;0.02;0.09;0.11;1" />
          </circle>
        </g>;
      })}

      {NODES.map(n => n.hub ? <Hub key={n.id} n={n} /> :
        <g key={n.id} className="lf-node" style={{ '--at': `${n.at}s` }}>
          <rect x={n.x - W / 2} y={n.y - H / 2} width={W} height={H} rx={H / 2} />
          <text x={n.x} y={n.y + 5.5} textAnchor="middle" className="lf-label">{n.label}</text>
          <text x={n.x} y={n.y + H / 2 + 16} textAnchor="middle" className="lf-sub">{n.sub}</text>
        </g>)}
    </svg>
    <figcaption>권한은 코드가 막고, 숫자는 툴이 계산하고, 모든 단계는 기록됩니다.</figcaption>
  </figure>;
}
