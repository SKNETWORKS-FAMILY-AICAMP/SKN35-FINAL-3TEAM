# -*- coding: utf-8 -*-
"""문서 파서: 파일 하나 → ParseResult (RAG 수집·프롬프트용 문서 공용). 사용법은 pipeline.py 맨 위.

    from agent_platform.knowledge.ingestion.parsers import DocumentParser
    r = DocumentParser(models_dir="...").parse("원본.pdf")

  pipeline.py  진입점 (형식 판별 → 형식별 경로 → 결과)
  hangul/      HWPX(우리 추출기)·HWP(syhwp) 원본 읽기
  pdf/         PDF·이미지: Docling 결과 뒤에 붙는 후처리 (선 격자·규칙선·칸 클립 표, 쪽 넘김 표 합치기, 글자 충실도 등)
제3자 코드 고지: THIRD_PARTY_NOTICES.md
"""
from agent_platform.knowledge.ingestion.parsers.pipeline import DocumentParser, ParseResult  # noqa: F401
