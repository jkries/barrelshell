"""Local PDF/HTML/text extraction with a killable parser worker."""
from html.parser import HTMLParser
from io import BytesIO
import json
from pathlib import Path
import re
import subprocess
import sys

from .transport import FetchError

MAX_TEXT = 500_000
MAX_PAGES = 300


class ReadableHTML(HTMLParser):
    """Conservative fallback; retain structure while dropping common boilerplate."""
    SKIP = {"script", "style", "noscript", "template", "nav", "footer", "header", "aside", "svg"}
    BLOCK = {"p", "div", "section", "article", "main", "br", "tr", "ul", "ol", "blockquote"}
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.parts = []
        self.title_parts = []
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.in_title = True
        if tag not in self.VOID:
            self.stack.append(tag)
        if any(t in self.SKIP for t in self.stack):
            return
        if tag in self.BLOCK or re.fullmatch(r"h[1-6]", tag):
            self.parts.append("\n\n")
            if tag.startswith("h"):
                self.parts.append("#" * int(tag[1]) + " ")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in {"td", "th"}:
            self.parts.append(" | ")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag in self.stack:
            index = len(self.stack) - 1 - self.stack[::-1].index(tag)
            del self.stack[index:]
        if tag in self.BLOCK or tag == "li" or re.fullmatch(r"h[1-6]", tag):
            self.parts.append("\n")

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data)
        elif not any(t in self.SKIP or t == "head" for t in self.stack):
            self.parts.append(re.sub(r"\s+", " ", data))

    def text(self):
        text = "".join(self.parts)
        text = re.sub(r"[ \t]+\n", "\n", text)
        return re.sub(r"\n{3,}", "\n\n", text).strip()


def decode(body: bytes, content_type: str) -> str:
    match = re.search(r"charset=[\"']?([\w.-]+)", content_type, re.I)
    if match:
        try:
            return body.decode(match[1], errors="replace")
        except LookupError:
            pass
    # HTML parsers handle charset declarations themselves; this path is for
    # plain text and the stdlib fallback. UTF-8 is the conservative default.
    return body.decode("utf-8-sig", errors="replace")


def _extract(body: bytes, content_type: str, url: str) -> dict:
    mime = content_type.split(";", 1)[0].strip().lower()
    warnings = []
    if body.lstrip().startswith(b"%PDF-") or mime == "application/pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise FetchError("PDF support missing; install requirements.txt") from exc
        reader = PdfReader(BytesIO(body))
        if reader.is_encrypted and not reader.decrypt(""):
            raise FetchError("PDF is password-protected; cannot extract it")
        page_count = len(reader.pages)
        pages, total = [], 0
        for page in reader.pages[:MAX_PAGES]:
            text = (page.extract_text() or "").strip()
            if total + len(text) > MAX_TEXT:
                text = text[:MAX_TEXT - total]
            pages.append(text)
            total += len(text)
            if total >= MAX_TEXT:
                break
        if len(pages) < page_count or total >= MAX_TEXT:
            warnings.append("Extraction limit reached; this is NOT the complete PDF.")
        empty = sum(not p.strip() for p in pages)
        if empty:
            warnings.append(f"{empty} extracted page(s) have no text; scanned content needs OCR (not installed).")
        if not any(pages):
            raise FetchError("PDF has no extractable text; it may be scanned/image-only and needs OCR")
        title = str((reader.metadata or {}).get("/Title", ""))[:300]
        return {"title": title, "extractor": "pypdf", "pages": pages,
                "page_count": page_count, "warnings": warnings}
    head = body[:1024].lstrip().lower()
    is_html = ("html" in mime or head.startswith((b"<!doctype html", b"<html")))
    if is_html:
        html = decode(body, content_type)
        fallback = ReadableHTML()
        fallback.feed(html)
        text, extractor = "", "html-fallback"
        try:
            import trafilatura
            text = trafilatura.extract(body, url=url, output_format="markdown",
                                       include_comments=False, include_tables=True,
                                       include_links=True) or ""
            if text:
                extractor = "trafilatura"
        except ImportError:
            warnings.append("Main-content extractor missing; install requirements.txt.")
        except Exception:
            warnings.append("Main-content extraction failed; used structural HTML fallback.")
        text = text or fallback.text()
        title = " ".join(fallback.title_parts).strip()[:300]
    elif mime.startswith("text/") or mime in {"application/json", "application/xml", "application/xhtml+xml"}:
        text, title, extractor = decode(body, content_type), "", "text"
    else:
        raise FetchError(f"unsupported content type: {mime or '(missing)'}")
    if len(text) > MAX_TEXT:
        warnings.append("Extraction character limit reached; source is incomplete.")
    return {"title": title, "extractor": extractor, "pages": [text[:MAX_TEXT].strip()],
            "page_count": None, "warnings": warnings}


def _worker():
    try:
        # A best-effort address-space cap on Unix. Windows still has a hard
        # wall-clock limit but needs OS/container policy for memory isolation.
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024,) * 2)
        except (ImportError, OSError, ValueError):
            pass
        result = (True, _extract(sys.stdin.buffer.read(), sys.argv[2], sys.argv[3]))
    except Exception as exc:
        result = (False, f"{type(exc).__name__}: {exc}")
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))


def extract(body: bytes, content_type: str, url: str = "", timeout: float = 20) -> dict:
    """Parse in a killable interpreter that never re-imports the bot/skills.

    No shell is used. Document bytes travel over stdin, not as Python code.
    Using multiprocessing.spawn here would re-execute the agent's main module
    and arbitrary trusted skill initializers on every Windows extraction.
    """
    process = subprocess.Popen(
        [sys.executable, "-m", "barrel_web.extract", "--worker", content_type, url],
        cwd=Path(__file__).resolve().parent.parent,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        output, _stderr = process.communicate(body, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.communicate()
        raise FetchError("document extraction timed out") from exc
    except BaseException:
        process.kill()
        process.communicate()
        raise
    try:
        ok, result = json.loads(output.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise FetchError("document parser exited unexpectedly") from exc
    if not ok:
        raise FetchError(f"document extraction failed: {result}")
    return result


if __name__ == "__main__" and len(sys.argv) == 4 and sys.argv[1] == "--worker":
    _worker()
