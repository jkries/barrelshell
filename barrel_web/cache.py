"""Bounded on-disk document cache, partitioned by conversation.

Cached content is never served by the dashboard or exposed as workspace files.
Only extracted text/metadata is stored; raw PDFs, scripts and cookies are not.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import uuid

from .transport import FetchError

_LOCK = threading.RLock()


def chunks(pages: list[str], size: int = 800) -> list[dict]:
    result = []
    for page_no, text in enumerate(pages, 1):
        start = 0
        while start < len(text):
            end = min(len(text), start + size)
            if end < len(text):
                split = text.rfind("\n", start + size // 2, end)
                if split < 0:
                    split = text.rfind(" ", start + size // 2, end)
                if split > start:
                    end = split + 1
            result.append({"page": page_no, "text": text[start:end]})
            start = end
    return result


class Cache:
    def __init__(self, root=".barrel_cache", ttl=86400, max_documents=64):
        self.root = Path(root)
        self.ttl = ttl
        self.max_documents = max_documents

    def _scope(self, chat_id):
        return hashlib.sha256(str(chat_id).encode()).hexdigest()[:24]

    def _purge(self):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        files = sorted(self.root.glob("*/*.json"), key=lambda p: p.stat().st_mtime)
        cutoff = time.time() - self.ttl
        for i, path in enumerate(files):
            if path.stat().st_mtime < cutoff or i < len(files) - self.max_documents:
                path.unlink(missing_ok=True)

    def _load(self, path):
        try:
            if time.time() - path.stat().st_mtime > self.ttl:
                path.unlink(missing_ok=True)
                return None
            with path.open(encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def find(self, chat_id, source, mode):
        with _LOCK:
            self._purge()
            directory = self.root / self._scope(chat_id)
            for path in sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
                doc = self._load(path)
                if doc and doc["source"] == source and doc["mode"] == mode:
                    return doc
        return None

    def read(self, chat_id, doc_id):
        if not re.fullmatch(r"[a-f0-9]{16}", doc_id):
            raise FetchError("invalid document ID")
        with _LOCK:
            doc = self._load(self.root / self._scope(chat_id) / f"{doc_id}.json")
        if not doc:
            raise FetchError("document not found/expired in this chat; fetch the URL again")
        return doc

    def save(self, chat_id, doc):
        doc = {**doc, "id": uuid.uuid4().hex[:16], "fetched_at": time.time()}
        doc["chunks"] = chunks(doc.pop("pages"))
        with _LOCK:
            directory = self.root / self._scope(chat_id)
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(dir=directory, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(doc, f, ensure_ascii=False)
                os.replace(name, directory / f"{doc['id']}.json")
            finally:
                Path(name).unlink(missing_ok=True)
            self._purge()
        return doc
