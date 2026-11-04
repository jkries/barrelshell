"""Document fetch/read/search API, independent of Ollama and Telegram."""
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from pathlib import Path

from .cache import Cache
from .extract import extract
from .transport import FetchError, get, validate_url


@dataclass
class Settings:
    max_chars: int = 4000
    max_bytes: int = 20_000_000
    timeout: int = 30
    browser_enabled: bool = False
    browser_timeout: int = 25
    cache_ttl: int = 86400
    cache_max_documents: int = 64

    @classmethod
    def from_config(cls, cfg):
        def integer(key, default, low, high):
            value = cfg.get(key, default)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise FetchError(f"config {key} must be an integer between {low} and {high}")
            return value
        enabled = cfg.get("fetch_browser_enabled", False)
        if not isinstance(enabled, bool):
            raise FetchError("config fetch_browser_enabled must be true or false")
        return cls(
            max_chars=integer("fetch_max_chars", 4000, 1000, 32000),
            max_bytes=integer("download_max_bytes", 20_000_000, 1024, 50_000_000),
            timeout=integer("fetch_timeout_seconds", 30, 1, 120),
            browser_enabled=enabled,
            browser_timeout=integer("fetch_browser_timeout_seconds", 25, 5, 90),
            cache_ttl=integer("fetch_cache_ttl_seconds", 86400, 60, 604800),
            cache_max_documents=integer("fetch_cache_max_documents", 64, 1, 256))


class Documents:
    def __init__(self, settings=None, cache_root=".barrel_cache"):
        self.settings = settings or Settings()
        self.cache = Cache(cache_root, self.settings.cache_ttl,
                           self.settings.cache_max_documents)

    def fetch(self, url, chat_id, *, force_render=False, refresh=False):
        s = self.settings
        url, _, _ = validate_url(url)
        mode = "browser" if force_render else ("auto" if s.browser_enabled else "static")
        if force_render and not s.browser_enabled:
            raise FetchError("browser rendering is disabled; set fetch_browser_enabled to true and install Chromium")
        if not refresh:
            cached = self.cache.find(chat_id, url, mode)
            if cached:
                return self.format(cached, range(len(cached["chunks"])), cached=True)
        response = get(url, max_bytes=s.max_bytes, timeout=s.timeout)
        ctype = response.headers.get("content-type", "")
        doc = extract(response.body, ctype, response.url)
        final_url = response.url
        html = ("html" in ctype.lower() or response.body[:1024].lstrip().lower().startswith((b"<!doctype html", b"<html")))
        sparse = sum(len(p) for p in doc["pages"]) < 250
        if html and s.browser_enabled and (force_render or sparse):
            from .browser import render
            try:
                body, rendered_url, warnings = render(response.url, timeout=s.browser_timeout,
                                                  max_bytes=s.max_bytes)
                rendered = extract(body, "text/html; charset=utf-8", rendered_url)
                rendered["warnings"].extend(warnings)
                rendered["extractor"] += "+chromium"
                if force_render and not any(rendered["pages"]):
                    raise FetchError("browser-rendered page had no extractable text")
                if any(rendered["pages"]) and (force_render or sum(len(p) for p in rendered["pages"]) >= sum(len(p) for p in doc["pages"])):
                    doc = rendered
                    final_url = rendered_url
                else:
                    doc["warnings"].append("Rendered extraction was shorter; retained static content.")
            except FetchError as exc:
                if force_render or not any(doc["pages"]):
                    raise
                doc["warnings"].append(f"Browser fallback failed: {exc}")
        if not any(doc["pages"]):
            hint = " Try URL | render if browser support is enabled." if not s.browser_enabled else " Browser rendering also found no text."
            raise FetchError("page had no extractable text." + hint)
        if html and sparse and not s.browser_enabled:
            doc["warnings"].append("Very little static text; this may need JavaScript. Browser rendering is disabled.")
        doc.update(source=url, final_url=final_url, mode=mode,
                   content_type=ctype, source_bytes=len(response.body))
        doc = self.cache.save(chat_id, doc)
        return self.format(doc, range(len(doc["chunks"])))

    def local(self, path, name, chat_id):
        s = self.settings
        with Path(path).open("rb") as f:
            body = f.read(s.max_bytes + 1)
        if len(body) > s.max_bytes:
            raise FetchError("file exceeds extraction byte limit")
        doc = extract(body, "application/pdf", name)
        doc.update(source=f"workspace:{name}", final_url=f"workspace:{name}",
                   mode="local", content_type="application/pdf", source_bytes=len(body))
        doc = self.cache.save(chat_id, doc)
        return self.format(doc, range(len(doc["chunks"])))

    def format(self, doc, indexes, *, cached=False):
        total = len(doc["chunks"])
        if not total:
            raise FetchError("document has no text chunks")
        selected = []
        parts = []
        budget = self.settings.max_chars
        for index in indexes:
            chunk = doc["chunks"][index]
            label = f"[Chunk {index + 1}/{total}" + (f", PDF page {chunk['page']}" if doc["page_count"] else "") + "]\n"
            content = chunk["text"]
            # Never silently clip a chunk: every numbered chunk remains fully
            # readable, even at the smallest supported response budget.
            if budget < len(label) + len(content):
                break
            parts.append(label + content)
            selected.append(index + 1)
            budget -= len(label) + len(content) + 2
        title = re.sub(r"\s+", " ", doc["title"]).strip() or "(untitled)"
        header = [f"Document ID: {doc['id']}", f"Title: {title}",
                  f"Source: {doc['source']}", f"Final URL: {doc['final_url']}",
                  f"Extractor: {doc['extractor']} | {'cached' if cached else 'stored'}",
                  f"Fetched at: {datetime.fromtimestamp(doc['fetched_at'], timezone.utc).isoformat(timespec='seconds')}",
                  f"Downloaded bytes: {doc['source_bytes']}",
                  f"Chunks returned: {', '.join(map(str, selected))} of {total}"]
        if doc["page_count"]:
            header.append(f"PDF pages: {doc['page_count']}")
        if len(selected) < total:
            header.append("PARTIAL READ: more content exists. Do not claim to have read the whole document.")
        header += [f"Warning: {w}" for w in doc["warnings"]]
        header.append(f"Continue: read {doc['id']} | <chunk-number/range>; search {doc['id']} | <query>")
        return "\n".join(header) + "\n\n" + "\n\n".join(parts)

    def command(self, arg, chat_id):
        verb, _, rest = arg.strip().partition(" ")
        if verb in {"read", "search", "pages"}:
            doc_id, sep, value = rest.partition("|")
            doc = self.cache.read(chat_id, doc_id.strip())
            value = value.strip()
            if not sep or not value:
                raise FetchError(f"use {verb} DOCUMENT_ID | " + ("query" if verb == "search" else "1-3"))
            if verb == "search":
                if len(value) > 500:
                    raise FetchError("search query is too long")
                terms = set(re.findall(r"\w+", value.lower()))
                ranked = sorted(((sum(c["text"].lower().count(t) for t in terms), i)
                                 for i, c in enumerate(doc["chunks"])), reverse=True)
                indexes = [i for score, i in ranked if score > 0][:3]
                if not indexes:
                    return f"No matching chunks in document {doc['id']}. Try other words."
            else:
                match = re.fullmatch(r"(\d{1,6})(?:\s*-\s*(\d{1,6}))?", value)
                if not match:
                    raise FetchError("expected a positive number or range, e.g. 2-4")
                start, end = int(match[1]), int(match[2] or match[1])
                limit = doc["page_count"] if verb == "pages" else len(doc["chunks"])
                if not limit or not 1 <= start <= end <= limit:
                    raise FetchError(f"range is outside this document (maximum {limit or 0})")
                indexes = ([i for i, c in enumerate(doc["chunks"]) if start <= c["page"] <= end]
                           if verb == "pages" else list(range(start - 1, end)))
                if not indexes:
                    raise FetchError("requested pages have no extracted text or exceed extraction limits")
            return self.format(doc, indexes, cached=True)
        pieces = [p.strip() for p in arg.split("|")]
        if not pieces[0] or any(p not in {"render", "refresh"} for p in pieces[1:]):
            raise FetchError("use URL [| render] [| refresh], read ID | 2-4, pages ID | 5-7, or search ID | query")
        return self.fetch(pieces[0], chat_id, force_render="render" in pieces[1:], refresh="refresh" in pieces[1:])
