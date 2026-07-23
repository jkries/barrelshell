"""Bundled skill: sandboxed workspace file access.

The deepest bundled skill, and the clearest example of using the core
API: workspace containment (core._workspace_path), the SSRF guard
(core._is_private_host), extension repair (core._sniff_ext), and
Telegram upload (core.tg_upload) all live in core and are called via
`core.`. The skill owns the verbs; core owns the shared, security-
sensitive primitives.
"""
import mimetypes
import os
from urllib.parse import quote, unquote, urlparse

import requests

import barrel_v1 as core


def file(arg: str, chat_id: int) -> str:
    os.makedirs(core.WORKSPACE_DIR, exist_ok=True)
    verb, _, rest = arg.strip().partition(" ")
    verb, rest = verb.lower(), rest.strip()

    if verb == "list":
        entries = sorted(os.listdir(core.WORKSPACE_DIR))
        if not entries:
            return "(workspace is empty)"
        return "\n".join(
            f"- {n} ({os.path.getsize(os.path.join(core.WORKSPACE_DIR, n))}"
            f" bytes)" for n in entries)

    if verb == "read":
        path = core._workspace_path(rest)
        if not path or not os.path.isfile(path):
            return f"(no such file in workspace: {rest})"
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read(core.FETCH_MAX_CHARS + 1)
        except OSError as e:
            return f"(read failed: {e})"
        if len(text) > core.FETCH_MAX_CHARS:
            text = text[:core.FETCH_MAX_CHARS] + " …(truncated)"
        return text or "(file is empty)"

    if verb == "write":
        name, _, content = rest.partition("|")
        name, content = name.strip(), content.strip()
        path = core._workspace_path(name)
        if not path:
            return "(refused: path escapes the workspace)"
        if not content:
            return "(nothing to write — use: write name.txt | content)"
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content + "\n")
        except OSError as e:
            return f"(write failed: {e})"
        return f"(wrote {name}, {len(content)} chars)"

    if verb == "send":
        name, _, caption = rest.partition("|")
        name, caption = name.strip(), caption.strip()
        path = core._workspace_path(name)
        if not path or not os.path.isfile(path):
            return f"(no such file in workspace: {name})"
        size = os.path.getsize(path)
        if size > 50_000_000:
            return "(file exceeds Telegram's 50 MB bot limit)"
        if chat_id == core.WEB_CHAT_ID:
            return (f"(files can't be pushed into the web chat — give the "
                    f"user this link to open it: "
                    f"http://{core.DASHBOARD_HOST}:{core.DASHBOARD_PORT}"
                    f"/workspace/{quote(name)} )")
        ext = os.path.splitext(name)[1].lower()
        if not ext:
            sniffed = core._sniff_ext(path)
            if sniffed:
                fixed = core._workspace_path(name + sniffed)
                try:
                    if fixed and not os.path.exists(fixed):
                        os.rename(path, fixed)
                        path, name, ext = fixed, name + sniffed, sniffed
                except OSError:
                    ext = sniffed
        photo_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
        audio_exts = {".mp3", ".m4a", ".ogg", ".oga", ".wav", ".flac"}
        try:
            if ext in photo_exts and size <= 10_000_000:
                try:
                    core.tg_upload("sendPhoto", chat_id, "photo", path, caption)
                except requests.RequestException:
                    core.tg_upload("sendDocument", chat_id, "document",
                                   path, caption)
            elif ext in audio_exts:
                core.tg_upload("sendAudio", chat_id, "audio", path, caption)
            else:
                core.tg_upload("sendDocument", chat_id, "document", path,
                               caption)
        except requests.RequestException as e:
            return f"(send failed: {e.__class__.__name__})"
        core.log_event("file_sent", name=name, bytes=size, chat_id=chat_id)
        return f"(sent {name} to the chat — {size} bytes)"

    if verb == "download":
        url, _, name = rest.partition("|")
        url, name = url.strip(), name.strip()
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return "(download refused: only http/https URLs)"
        if not parsed.hostname or core._is_private_host(parsed.hostname):
            return "(download refused: host is private/unresolvable)"
        try:
            r = requests.get(url, timeout=30, stream=True, headers={
                "User-Agent": "Mozilla/5.0 (BarrelShell agent)"})
            r.raise_for_status()
        except requests.RequestException as e:
            return f"(download failed: {e})"
        if not name:
            name = os.path.basename(unquote(parsed.path)) or "download"
        if not os.path.splitext(name)[1]:
            ext = os.path.splitext(unquote(parsed.path))[1]
            if not ext:
                ctype = r.headers.get("content-type", "").split(";")[0]
                ext = mimetypes.guess_extension(ctype.strip()) or ""
                ext = {".jpe": ".jpg", ".jpeg": ".jpg"}.get(ext, ext)
            name += ext or ".bin"
        path = core._workspace_path(name)
        if not path:
            return "(refused: filename escapes the workspace)"
        try:
            size = 0
            with open(path, "wb") as f:
                for chunk in r.iter_content(65536):
                    size += len(chunk)
                    if size > core.DOWNLOAD_MAX_BYTES:
                        f.close()
                        os.remove(path)
                        return "(download refused: exceeds size cap)"
                    f.write(chunk)
        except requests.RequestException as e:
            return f"(download failed: {e})"
        return f"(downloaded {name}, {size} bytes)"

    return ("(unknown file command — use: list | read <name> | "
            "write <name> | <content> | download <url> | <name>)")


SKILL = {
    "name": "file",
    "desc": "Work with files in your workspace folder (the ONLY folder "
            "you can access). Grammar: <file>list</file>, "
            "<file>read notes.txt</file>, "
            "<file>write notes.txt | the content</file>, "
            "<file>download https://url | saved-name.pdf</file> (always "
            "give the saved name a matching file extension), "
            "<file>send name.jpg | optional caption</file> to deliver a "
            "workspace file (image/audio/any) to the user in Telegram",
    "handler": file,
}
