# services/web_search.py
import os
import logging

import httpx

logger = logging.getLogger(__name__)

TAVILY_ENDPOINT = "https://api.tavily.com/search"


def _get_api_key() -> str:
    """Read the key at call time, not import time.

    This module is imported before load_dotenv() runs, so caching the key at
    import would capture it as empty. Reading it lazily lets .env win.
    """
    return os.environ.get("TAVILY_API_KEY", "")


def is_configured() -> bool:
    """True when a Tavily API key is available, so callers can offer search."""
    return bool(_get_api_key())


async def search(query: str, max_results: int = 5) -> dict:
    """Run a live web search via Tavily and return a compact result payload.

    Returns a dict with an optional 'answer' (Tavily's synthesized summary) and
    a list of 'results' ({title, url, content}). On any failure returns
    {'error': <message>} so the caller can surface a clean message instead of
    raising into the streaming loop.
    """
    api_key = _get_api_key()
    if not api_key:
        return {"error": "Web search is not configured (missing TAVILY_API_KEY)."}

    body = {
        "api_key": api_key,
        "query": query,
        "search_depth": "basic",
        "include_answer": True,
        "max_results": max_results,
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(TAVILY_ENDPOINT, json=body)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        logger.error(f"Tavily HTTP error: {e.response.status_code}", exc_info=True)
        return {"error": f"Search service returned status {e.response.status_code}."}
    except httpx.RequestError as e:
        logger.error(f"Tavily request error: {e}", exc_info=True)
        return {"error": "Search service is unreachable."}
    except Exception as e:
        logger.error(f"Tavily unexpected error: {e}", exc_info=True)
        return {"error": "Search failed unexpectedly."}

    results = [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "content": r.get("content", ""),
        }
        for r in data.get("results", [])
    ]
    return {"answer": data.get("answer", ""), "results": results}


def format_for_model(payload: dict) -> str:
    """Flatten a search payload into plain text for feeding back to the model."""
    if "error" in payload:
        return f"SEARCH ERROR: {payload['error']}"

    lines = []
    if payload.get("answer"):
        lines.append(f"Summary: {payload['answer']}")
        lines.append("")
    for i, r in enumerate(payload.get("results", []), 1):
        lines.append(f"[{i}] {r['title']}")
        lines.append(f"    URL: {r['url']}")
        if r["content"]:
            lines.append(f"    {r['content']}")
    return "\n".join(lines) or "No results found."
