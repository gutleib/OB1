"""PostgreSQL connection pool and query helpers."""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import asyncpg
from pgvector.asyncpg import register_vector

logger = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None


async def init_pool(dsn: str, *, min_size: int = 2, max_size: int = 10) -> None:
    """Create the global connection pool and register pgvector on every connection."""
    global _pool
    _pool = await asyncpg.create_pool(
        dsn,
        min_size=min_size,
        max_size=max_size,
        init=register_vector,  # runs for every new connection
    )
    logger.info("DB pool ready (min=%d max=%d)", min_size, max_size)


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("DB pool closed")


async def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialized")
    return _pool


async def init_schema(sql_path: str = "schema.sql") -> None:
    """Run schema.sql on startup (idempotent — uses IF NOT EXISTS)."""
    import os
    if not os.path.exists(sql_path):
        logger.warning("Schema file not found: %s", sql_path)
        return
    with open(sql_path) as f:
        sql = f.read()
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(sql)
    logger.info("Schema initialized from %s", sql_path)


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

THOUGHT_FIELDS = "id, content, metadata, created_at, updated_at"


async def search_thoughts(
    query_embedding: list[float],
    match_threshold: float = 0.5,
    match_count: int = 10,
    filter_json: dict | None = None,
) -> list[dict[str, Any]]:
    pool = await get_pool()
    filter_str = json.dumps(filter_json or {})
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, content, metadata, similarity, created_at
            FROM match_thoughts($1::vector, $2, $3, $4::jsonb)
            """,
            query_embedding,  # native list — pgvector codec handles serialization
            match_threshold,
            match_count,
            filter_str,
        )
    return [dict(r) for r in rows]


async def list_thoughts(
    *,
    thought_type: str | None = None,
    topic: str | None = None,
    person: str | None = None,
    since: str | None = None,
    before: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    pool = await get_pool()
    clauses = []
    args: list[Any] = []

    idx = 1
    if thought_type:
        clauses.append(f"metadata->>'type' = ${idx}")
        args.append(thought_type)
        idx += 1
    if topic:
        clauses.append(f"${idx} = ANY (ARRAY(SELECT jsonb_array_elements_text(metadata->'topics')))")
        args.append(topic)
        idx += 1
    if person:
        clauses.append(f"${idx} = ANY (ARRAY(SELECT jsonb_array_elements_text(metadata->'people')))")
        args.append(person)
        idx += 1
    if since:
        clauses.append(f"created_at >= ${idx}::timestamptz")
        args.append(since)
        idx += 1
    if before:
        clauses.append(f"created_at <= ${idx}::timestamptz")
        args.append(before)
        idx += 1

    where = " AND ".join(clauses) if clauses else "TRUE"
    args.append(limit)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {THOUGHT_FIELDS} FROM thoughts WHERE {where} ORDER BY created_at DESC LIMIT ${idx}",
            *args,
        )
    return [dict(r) for r in rows]


async def thought_stats() -> dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT count(*) FROM thoughts")

        type_counts = await conn.fetch(
            "SELECT metadata->>'type' AS type, count(*) AS cnt FROM thoughts GROUP BY 1 ORDER BY 2 DESC"
        )
        types = {r["type"] or "unknown": r["cnt"] for r in type_counts}

        topic_rows = await conn.fetch(
            """
            SELECT topic, count(*) AS cnt
            FROM thoughts, jsonb_array_elements_text(metadata->'topics') AS topic
            GROUP BY topic ORDER BY cnt DESC LIMIT 10
            """
        )
        topics = {r["topic"]: r["cnt"] for r in topic_rows}

        person_rows = await conn.fetch(
            """
            SELECT person, count(*) AS cnt
            FROM thoughts, jsonb_array_elements_text(metadata->'people') AS person
            GROUP BY person ORDER BY cnt DESC LIMIT 10
            """
        )
        people = {r["person"]: r["cnt"] for r in person_rows}

    return {"total": total, "types": types, "top_topics": topics, "top_people": people}


async def fetch_thought(thought_id: str) -> dict[str, Any] | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {THOUGHT_FIELDS} FROM thoughts WHERE id = $1", thought_id
        )
    return dict(row) if row else None


async def insert_thought(
    content: str,
    embedding: list[float],
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Insert a thought atomically — upsert + embedding in one transaction."""
    pool = await get_pool()
    payload = json.dumps({"metadata": metadata or {}})
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Upsert the thought (dedup by content fingerprint)
            row = await conn.fetchrow(
                "SELECT * FROM upsert_thought($1, $2::jsonb)",
                content,
                payload,
            )
            result = dict(row)
            thought_id = result["id"]

            # Store embedding atomically — always update, never leave NULL
            await conn.execute(
                "UPDATE thoughts SET embedding = $1::vector WHERE id = $2 AND embedding IS NULL",
                embedding,  # native list — pgvector codec handles serialization
                thought_id,
            )

    return result
