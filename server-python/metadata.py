"""Metadata extraction using OpenAI-compatible LLM API.

Extracts: people, action_items, dates_mentioned, topics, type.
Mirrors the original OB1 extractMetadata() from server/index.ts.
"""

from __future__ import annotations

import json
import logging
import os

import httpx

logger = logging.getLogger(__name__)

LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-chat")

EXTRACTION_PROMPT = """Extract metadata from the captured thought. Return JSON with:
- "people": array of people mentioned (empty if none)
- "action_items": array of implied to-dos (empty if none)
- "dates_mentioned": array of dates YYYY-MM-DD (empty if none)
- "topics": array of 1-3 short topic tags (always at least one)
- "type": one of "observation", "task", "idea", "reference", "person_note"
Only extract what's explicitly there."""


async def extract_metadata(text: str) -> dict:
    """Extract structured metadata from thought text via LLM."""
    if not LLM_API_KEY:
        logger.warning("LLM_API_KEY not set — skipping metadata extraction")
        return {"topics": ["uncategorized"], "type": "observation"}

    url = f"{LLM_BASE_URL.rstrip('/')}/v1/chat/completions"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {LLM_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": LLM_MODEL,
                "messages": [
                    {"role": "system", "content": EXTRACTION_PROMPT},
                    {"role": "user", "content": text},
                ],
                "temperature": 0.1,
                "max_tokens": 300,
            },
        )
        resp.raise_for_status()
        data = resp.json()

    content_raw = None
    try:
        content_raw = data["choices"][0]["message"]["content"]
        # Strip markdown code fences if present
        if content_raw.startswith("```"):
            content_raw = content_raw.split("\n", 1)[-1].rsplit("\n```", 1)[0]
        return json.loads(content_raw)
    except (json.JSONDecodeError, KeyError, IndexError):
        logger.warning("Failed to parse metadata JSON from: %s", content_raw or "N/A")
        return {"topics": ["uncategorized"], "type": "observation"}
