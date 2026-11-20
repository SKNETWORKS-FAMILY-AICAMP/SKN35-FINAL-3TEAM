# -*- coding: utf-8 -*-
"""KURE-v3 임베딩 (sentence-transformers 없이 같은 계산).

계산: 프롬프트 붙이기("passage: " / "query: ") → 마지막 은닉층 → 평균 풀링(프롬프트 포함) → 앞쪽 dim차원만 → L2 정규화.
근거: 모델 폴더의 config_sentence_transformers.json(프롬프트), 1_Pooling/config.json(mean, include_prompt).

- 차원: KURE-v3는 2,048차원(Matryoshka로 2,048·1,024·512·256차원에 학습돼 앞쪽만 잘라 쓸 수도 있음).
  기본 2048 = 팀 DB chunk.embedding halfvec(2048) (마이그레이션 0004 초안). 0004 전 DB(vector(1024))면 dim=1024.
  (2차 청킹 실험, 질문 108개: 2048 Recall@1 92.6%·MRR 0.957, 1024 Recall@1 90.7%·MRR 0.947, Recall@5는 둘 다 100%.)
- 장치: 배포·운영은 CPU 기준이라 기본 CPU. GPU는 device="cuda"로 직접 줄 때만.
- 폐쇄망: 로컬 모델 폴더만 읽는다(인터넷에서 받지 않음). 경로는 인자 또는 환경변수 EMBEDDING_MODEL_PATH.
  (실험에서 쓴 모델: nlpai-lab/KURE-v3 판 5b0494e, model.safetensors sha256 53b26a87…dc9)
"""
from __future__ import annotations

import os
from pathlib import Path

MODEL_ENV = "EMBEDDING_MODEL_PATH"
DEFAULT_DIM = 2048
PROMPT = {"query": "query: ", "document": "passage: "}


class KureEmbedder:
    def __init__(self, model_path: str | Path | None = None, *, dim: int = DEFAULT_DIM, device: str = "cpu",
                 max_length: int = 1024, batch_size: int = 8):
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        import torch
        from transformers import AutoModel, AutoTokenizer

        p = model_path or os.environ.get(MODEL_ENV)
        if not p:
            raise RuntimeError(f"임베딩 모델 경로가 없음: model_path 인자나 환경변수 {MODEL_ENV}로 KURE-v3 폴더를 주세요")
        if dim not in (256, 512, 1024, 2048):
            raise ValueError("KURE-v3는 2048·1024·512·256차원만 학습됨")
        self._torch = torch
        self.dim, self.device, self.max_length, self.batch_size = dim, device, max_length, batch_size
        self.tokenizer = AutoTokenizer.from_pretrained(str(p), local_files_only=True)
        self.model = AutoModel.from_pretrained(str(p), local_files_only=True, dtype=torch.float32).to(device)
        self.model.eval()

    def _encode(self, texts: list[str], kind: str) -> list[list[float]]:
        torch = self._torch
        out: list[list[float]] = []
        with torch.no_grad():
            for k in range(0, len(texts), self.batch_size):
                batch = [PROMPT[kind] + t for t in texts[k:k + self.batch_size]]
                enc = self.tokenizer(batch, padding=True, truncation=True, max_length=self.max_length,
                                     return_tensors="pt").to(self.device)
                h = self.model(**enc).last_hidden_state
                m = enc["attention_mask"].unsqueeze(-1).to(h.dtype)
                v = (h * m).sum(1) / m.sum(1).clamp(min=1)
                v = torch.nn.functional.normalize(v[:, :self.dim], dim=-1)
                out.extend(v.float().cpu().tolist())
        return out

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """청크의 embed_text 목록 → 벡터 목록 (각 dim차원, 길이 1)."""
        return self._encode(texts, "document")

    def embed_query(self, text: str) -> list[float]:
        """검색 질문 → 벡터."""
        return self._encode([text], "query")[0]
