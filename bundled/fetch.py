"""Bundled skill: read the full text of one web page.

Uses core._is_private_host (the shared SSRF guard) so a prompt-injected
URL can't probe the local network. That guard lives in core because the
file-download skill needs it too.
"""
import re
from html.parser import HTMLParser

import requests

import barrel_v1 as core


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "template"}

    def __init__(self):
        super().__init__()
        self._skip_depth = 0
        self.chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth and data.strip():
            self.chunks.append(data.strip())


def fetch(url: str, chat_id: int) -> str:
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "(fetch refused: only http/https URLs)"
    if not parsed.hostname or core._is_private_host(parsed.hostname):
        return "(fetch refused: host is private/unresolvable)"
    try:
        r = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (BarrelShell agent)"})
        r.raise_for_status()
    except requests.RequestException as e:
        return f"(fetch failed: {e})"
    ctype = r.headers.get("content-type", "")
    if "html" not in ctype and "text" not in ctype:
        return f"(fetch refused: content-type {ctype})"
    if "html" in ctype:
        p = _TextExtractor()
        p.feed(r.text[:400_000])
        text = " ".join(p.chunks)
    else:
        text = r.text
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > core.FETCH_MAX_CHARS:
        text = text[:core.FETCH_MAX_CHARS] + " …(truncated)"
    return text or "(page had no extractable text)"


SKILL = {
    "name": "fetch",
    "desc": "Read the full text of one web page — use when search "
            "snippets aren't enough, on a URL from results or from the "
            "user. Emit <fetch>https://full.url/here</fetch>",
    "handler": fetch,
}
