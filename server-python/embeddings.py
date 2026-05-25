"""Embedding client for Infinity (OpenAI-compatible API).

Uses deepvk/USER-bge-m3 (1024-dim) by default.
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

INFINITY_URL = os.environ.get("INFINITY_URL", "http://localhost:7997")
INFINITY_MODEL = os.environ.get("INFINITY_MODEL", "deepvk/USER-bge-m3")
EMBEDDING_DIM = 1024


async def get_embedding(text: str) -> list[float]:
    """Get 1024-dim embedding from Infinity."""
    url = f"{INFINITY_URL.rstrip('/')}/embeddings"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            url,
            json={
                "model": INFINITY_MODEL,
                "input": text,
            },
        )
        resp.raise_for_status()
        data = resp.json()
    embedding = data["data"][0]["embedding"]
    if len(embedding) != EMBEDDING_DIM:
        raise ValueError(
            f"Expected {EMBEDDING_DIM}-dim embedding from {INFINITY_MODEL}, "
            f"got {len(embedding)}-dim. Check INFINITY_MODEL."
        )
    return embedding
