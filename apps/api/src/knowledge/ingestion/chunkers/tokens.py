# -*- coding: utf-8 -*-
"""청크 길이 세기 = 임베딩 모델(KURE-v3)의 토크나이저 기준.

폐쇄망 전제: 인터넷에서 받지 않는다. tokenizer.json 경로를 인자나 환경변수 CHUNKER_TOKENIZER_PATH로 준다.
(실험에서 쓴 파일: nlpai-lab/KURE-v3 판 5b0494e, tokenizer.json 17,077,777바이트,
 sha256 bed0c218bf2a85500014ecadb69d0f83c94b30526235f4bf2cac6d5c40bb5ced)
"""
import os
from functools import lru_cache
from pathlib import Path

TOKENIZER_ENV = "CHUNKER_TOKENIZER_PATH"


@lru_cache(maxsize=4)
def _load(path: str):
    from transformers import PreTrainedTokenizerFast        # docling 설치에 함께 들어온다

    return PreTrainedTokenizerFast(tokenizer_file=path)


def token_counter(tokenizer_path: str | Path | None = None):
    """토큰 수를 세는 함수를 돌려준다. 실험(2차 청킹)과 같은 방식: len(tokenizer.tokenize(text))."""
    p = tokenizer_path or os.environ.get(TOKENIZER_ENV)
    if not p:
        raise RuntimeError(f"토크나이저 경로가 없음: tokenizer_path 인자나 환경변수 {TOKENIZER_ENV}로 tokenizer.json 위치를 주세요")
    p = Path(p)
    if p.is_dir():
        p = p / "tokenizer.json"
    if not p.exists():
        raise FileNotFoundError(f"tokenizer.json이 없음: {p}")
    tok = _load(str(p))

    def count(text: str) -> int:
        return len(tok.tokenize(text)) if text else 0

    return count
