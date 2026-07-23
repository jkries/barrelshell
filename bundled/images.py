"""Bundled skill: find images on the web and return direct image URLs.

This is the missing first step of a three-part chain. On its own it
only *finds* pictures; sending one to the user means following it with
the file skill:

    <images>rainbow</images>          -> direct image URLs
    <file>download <url> | rainbow.jpg</file>
    <file>send rainbow.jpg</file>

Text search returns pages ABOUT a subject; this returns the pictures
themselves, which is what `file download` needs.

Self-contained — no core imports. Uses the same `ddgs` package the
search skill does, so there is nothing new to install.
"""
from ddgs import DDGS

# Editable settings. Note that bundled/ is replaced by platform
# updates — copy this file to skills/ if you want changes to persist.
MAX_RESULTS = 5
SAFESEARCH = "moderate"     # "on", "moderate", or "off"


def images(query: str, chat_id: int) -> str:
    query = query.strip()
    if not query:
        return "(no search terms — use: <images>what to look for</images>)"
    try:
        results = DDGS().images(query, safesearch=SAFESEARCH,
                                max_results=MAX_RESULTS)
    except Exception as e:
        return f"(image search failed: {e.__class__.__name__})"
    if not results:
        return f"(no images found for '{query}')"

    lines = []
    for r in results:
        url = r.get("image") or r.get("thumbnail") or ""
        if not url:
            continue
        title = (r.get("title") or "untitled").strip()[:80]
        w, h = r.get("width"), r.get("height")
        dims = f" [{w}x{h}]" if w and h else ""
        lines.append(f"- {title}{dims}\n  {url}")
    if not lines:
        return f"(no usable image URLs found for '{query}')"

    lines.append("(These are direct image URLs, not files the user "
                 "can see. Two more steps are required: download one "
                 "into the workspace with a matching extension, THEN "
                 "send it with the file skill.)")
    return "\n".join(lines)


SKILL = {
    "name": "images",
    "desc": "Find pictures on the web and get direct image URLs — use "
            "this, not search, whenever the user wants an image. To "
            "actually give the user a picture, do all three steps: "
            "<images>what to look for</images>, then "
            "<file>download THE_URL | name.jpg</file>, then "
            "<file>send name.jpg</file>.",
    "handler": images,
}
