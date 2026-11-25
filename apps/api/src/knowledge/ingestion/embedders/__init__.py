# -*- coding: utf-8 -*-
"""임베딩: 청크 embed_text·질문 → 벡터. 지금은 KURE-v3 하나."""
from .kure import DEFAULT_DIM, MODEL_ENV, KureEmbedder

__all__ = ["KureEmbedder", "DEFAULT_DIM", "MODEL_ENV"]
