"""Bundled skill: web search via DuckDuckGo (no API key).

Pulls its result cap from core config, so it honors config.json —
the lazy `import barrel_v1 as core` inside the handler is the pattern
for reaching platform settings without a load-time dependency.
"""
from ddgs import DDGS

import barrel_v1 as core


def search(query: str, chat_id: int) -> str:
    try:
        results = DDGS().text(query, max_results=core.SEARCH_RESULTS)
    except Exception as e:
        return f"(search failed: {e})"
    if not results:
        return "(no results)"
    return "\n".join(f"- {r.get('title','')}\n  {r.get('href','')}\n"
                     f"  {r.get('body','')}" for r in results)


SKILL = {
    "name": "search",
    "desc": "Search the web for current events or anything that may have "
            "changed since your training. Emit <search>concise query</search>",
    "handler": search,
}
