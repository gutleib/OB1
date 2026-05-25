"""
OB1 Russian Fork — Self-Hosted MCP Server

Drop-in replacement for the Supabase Edge Function MCP server from upstream OB1.
- PostgreSQL + pgvector (direct connection, no Supabase)
- Infinity for embeddings (deepvk/USER-bge-m3, 1024-dim)
- OpenAI-compatible LLM for metadata extraction
- MCP Streamable HTTP transport

Tools (mirrors original OB1 server/index.ts):
  search, fetch                  — read-only, ChatGPT-compatible
  search_thoughts                — semantic search
  list_thoughts                  — list recent with filters
  thought_stats                  — aggregate statistics
  capture_thought                — save + embed + extract metadata
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from dotenv import load_dotenv
from fastmcp import FastMCP

import db
from embeddings import get_embedding, EMBEDDING_DIM
from metadata import extract_metadata

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger("ob1-server")

DATABASE_URL = os.environ["DATABASE_URL"]
MCP_ACCESS_KEY = os.environ.get("MCP_ACCESS_KEY", "")
CITATION_BASE_URL = os.environ.get("OPEN_BRAIN_CITATION_BASE_URL", "https://openbrain.local/thoughts")

mcp = FastMCP(
    "ob1-ru",
    version="1.0.0",
    description="OB1 Russian Fork — self-hosted knowledge memory for AI agents",
)


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------

@mcp.lifespan
async def lifespan():
    """Initialize DB pool on startup, close on shutdown."""
    await db.init_pool(DATABASE_URL)
    await db.init_schema()
    logger.info("OB1-RU MCP server ready")
    yield
    await db.close_pool()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _thought_title(content: str, created_at: str | None = None) -> str:
    first_line = re.sub(r"\s+", " ", content).strip()[:80]
    date_prefix = created_at[:10] if created_at else "Open Brain"
    return f"{date_prefix} - {first_line}" if first_line else f"{date_prefix} thought"


def _thought_url(thought_id: str) -> str:
    return f"{CITATION_BASE_URL.rstrip('/')}/{thought_id}"


# ---------------------------------------------------------------------------
# Tool: search (ChatGPT-compatible, read-only)
# ---------------------------------------------------------------------------

@mcp.tool(
    name="search",
    annotations={"readOnlyHint": True},
)
async def tool_search(query: str) -> dict[str, Any]:
    """Search Open Brain memories by meaning. Read-only compatibility tool for ChatGPT."""
    try:
        q_emb = await get_embedding(query)
        results = await db.search_thoughts(q_emb, match_threshold=0.5, match_count=10)
        items = [
            {"id": t["id"], "title": _thought_title(t["content"], str(t["created_at"])),
             "url": _thought_url(t["id"])}
            for t in results
        ]
        return {"results": items}
    except Exception as e:
        logger.exception("search failed")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool: fetch (ChatGPT-compatible, read-only)
# ---------------------------------------------------------------------------

@mcp.tool(
    name="fetch",
    annotations={"readOnlyHint": True},
)
async def tool_fetch(thought_id: str) -> dict[str, Any]:
    """Fetch one Open Brain thought by ID after using search. Read-only."""
    try:
        thought = await db.fetch_thought(thought_id)
        if not thought:
            return {"error": "Thought not found"}
        return {
            "id": thought["id"],
            "content": thought["content"],
            "metadata": thought["metadata"],
            "created_at": str(thought["created_at"]),
            "url": _thought_url(thought["id"]),
        }
    except Exception as e:
        logger.exception("fetch failed")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool: search_thoughts (full semantic search)
# ---------------------------------------------------------------------------

@mcp.tool(
    name="search_thoughts",
    annotations={"readOnlyHint": True},
)
async def tool_search_thoughts(
    query: str,
    match_threshold: float = 0.5,
    match_count: int = 10,
) -> dict[str, Any]:
    """Search captured thoughts by meaning. Use when asking about a topic, person, or idea."""
    try:
        q_emb = await get_embedding(query)
        results = await db.search_thoughts(q_emb, match_threshold, match_count)
        items = [
            {
                "id": t["id"],
                "content": t["content"],
                "metadata": t["metadata"],
                "similarity": round(t["similarity"], 4),
                "created_at": str(t["created_at"]),
                "url": _thought_url(t["id"]),
            }
            for t in results
        ]
        return {"results": items, "count": len(items)}
    except Exception as e:
        logger.exception("search_thoughts failed")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool: list_thoughts
# ---------------------------------------------------------------------------

@mcp.tool(
    name="list_thoughts",
    annotations={"readOnlyHint": True},
)
async def tool_list_thoughts(
    type: str | None = None,
    topic: str | None = None,
    person: str | None = None,
    since: str | None = None,
    before: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """List recently captured thoughts with optional filters by type, topic, person, or time range."""
    try:
        results = await db.list_thoughts(
            thought_type=type, topic=topic, person=person,
            since=since, before=before, limit=limit,
        )
        items = [
            {
                "id": t["id"],
                "content": t["content"],
                "metadata": t["metadata"],
                "created_at": str(t["created_at"]),
            }
            for t in results
        ]
        return {"results": items, "count": len(items)}
    except Exception as e:
        logger.exception("list_thoughts failed")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool: thought_stats
# ---------------------------------------------------------------------------

@mcp.tool(
    name="thought_stats",
    annotations={"readOnlyHint": True},
)
async def tool_thought_stats() -> dict[str, Any]:
    """Get a summary of all captured thoughts: totals, types, top topics, and people."""
    try:
        stats = await db.thought_stats()
        return stats
    except Exception as e:
        logger.exception("thought_stats failed")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool: capture_thought
# ---------------------------------------------------------------------------

@mcp.tool(
    name="capture_thought",
    annotations={
        "readOnlyHint": False,
        "openWorldHint": False,
        "destructiveHint": False,
    },
)
async def tool_capture_thought(content: str) -> dict[str, Any]:
    """Save a new thought. Generates embedding and extracts metadata automatically."""
    try:
        embedding = await get_embedding(content)
        meta = await extract_metadata(content)
        result = await db.insert_thought(content, embedding, meta)
        return {
            "id": result["id"],
            "fingerprint": result.get("fingerprint", ""),
            "metadata": meta,
            "url": _thought_url(result["id"]),
        }
    except Exception as e:
        logger.exception("capture_thought failed")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Auth middleware + HTTP app
# ---------------------------------------------------------------------------

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


async def auth_middleware(request: Request, call_next):
    """Validate MCP_ACCESS_KEY via header or query param."""
    if not MCP_ACCESS_KEY:
        return await call_next(request)

    provided = request.headers.get("x-brain-key") or request.query_params.get("key")
    if not provided or provided != MCP_ACCESS_KEY:
        # JSON-RPC error envelope (HTTP 200) for MCP compat
        body = await request.body()
        body_text = body.decode() if body else "{}"
        req_id = None
        try:
            parsed = json.loads(body_text)
            if isinstance(parsed, dict):
                req_id = parsed.get("id")
        except (json.JSONDecodeError, TypeError):
            pass
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "error": {"code": -32001, "message": "Unauthorized: missing or invalid authentication."},
                "id": req_id,
            },
            status_code=200,
        )
    return await call_next(request)


# Build Starlette app with CORS and auth, mount MCP
app = Starlette(
    middleware=[
        Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST", "OPTIONS", "DELETE"],
                   allow_headers=["*"]),
        Middleware(BaseHTTPMiddleware, dispatch=auth_middleware),
    ]
)

# Mount MCP streamable HTTP at /mcp
app.mount("/mcp", mcp.streamable_http_app())

# Health check
@app.route("/health")
async def health(request: Request) -> Response:
    return JSONResponse({"ok": True, "version": "1.0.0"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=7981, log_level="info")
