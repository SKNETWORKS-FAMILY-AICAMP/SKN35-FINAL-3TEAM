import React from 'react';

// Agent Argus 로고 — 콘셉트 A "열쇠구멍 눈" (2_내가_만든것/디자인/Agent_Argus_로고_SVG/keyhole*.svg)
// 감시(눈)와 권한(열쇠구멍). 어두운 바탕(onDark)에서는 눈 테두리를 밝게, 열쇠구멍을 남색으로.
export default function ArgusLogo({ size = 34, onDark = false, title = 'Agent Argus' }) {
  const line = onDark ? '#e8eefb' : '#0b1730';
  const hole = onDark ? '#0b1730' : '#ffffff';
  return <svg width={size} height={size} viewBox="0 0 120 120" role="img" aria-label={title} className="argus-logo">
    <path d="M8 60 Q60 12 112 60 Q60 108 8 60 Z" fill="none" stroke={line} strokeWidth="7" strokeLinejoin="round" />
    <circle cx="60" cy="60" r="24" fill="#2f6bc4" />
    <circle cx="60" cy="54" r="7.5" fill={hole} />
    <path d="M55.5 57 L64.5 57 L68 76 L52 76 Z" fill={hole} />
  </svg>;
}
