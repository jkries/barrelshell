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


def _rel(name: str) -> str:
    """Normalise a user/model-supplied name to a workspace-relative
    one. Models often write a leading slash; treat that as the
    workspace root rather than the filesystem root, consistently
    across every verb. '..' is NOT handled here — core._workspace_path
    still refuses it, which is what keeps the sandbox closed."""
    return name.strip().lstrip("/\\").strip()


_HTML_EXTS = (".html", ".htm")


def _completeness_hint(path: str, name: str) -> str:
    """A cheap, generic sanity check — NOT verification (nothing here
    can run the page to confirm it actually works). Checks the two
    cheapest, clearest signs a generation got cut off: an outermost
    tag that opened but never closed, OR content so short it can't
    plausibly BE a real page — a generation cut off early enough
    never even reaches "<html" in the first place, which the
    unclosed-tag check alone can't see. Returns a short warning
    suffix, or "" if the check doesn't apply or nothing looks wrong."""
    if not name.lower().endswith(_HTML_EXTS):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            body = f.read()
    except OSError:
        return ""
    lower = body.lower()
    if "<html" in lower and "</html>" not in lower:
        return ("\n\u26a0 This file has an opening <html> tag but no "
                "closing </html> — it looks like it may have been cut "
                "off mid-generation. Read it back before assuming "
                "it's complete or telling the user it's ready.")
    if len(body.strip()) < 5 or (
            "<html" not in lower and "<!doctype" not in lower):
        return (f"\n\u26a0 This file is only {len(body.strip())} "
                f"character(s) and doesn't contain <html or "
                f"<!DOCTYPE anywhere — a generation cut off very "
                f"early wouldn't even reach those, so the "
                f"unclosed-tag check alone can't catch this. Read it "
                f"back before assuming it's complete or telling the "
                f"user it's ready. If this keeps happening, the "
                f"individual piece you're generating is still too "
                f"large — make it smaller still, don't switch to "
                f"writing the whole page in one shot, which is the "
                f"ORIGINAL problem this guidance exists to avoid.")
    return ""


def _ensure_parent(path: str) -> bool:
    """Create the folder a file is about to live in. Safe because the
    path came from core._workspace_path, which already refused
    anything outside the workspace."""
    parent = os.path.dirname(path)
    if not parent:
        return True
    try:
        os.makedirs(parent, exist_ok=True)
        return True
    except OSError:
        return False


def _walk_workspace(root: str, rel: str = "") -> list:
    """Depth-first listing, folders marked with a trailing slash."""
    out = []
    try:
        entries = sorted(os.scandir(root), key=lambda e: (not e.is_dir(),
                                                          e.name.lower()))
    except OSError:
        return out
    for e in entries:
        shown = f"{rel}{e.name}"
        if e.is_dir():
            out.append(f"- {shown}/")
            out.extend(_walk_workspace(e.path, shown + "/"))
        else:
            try:
                size = e.stat().st_size
            except OSError:
                size = 0
            out.append(f"- {shown} ({size} bytes)")
        if len(out) >= 200:
            out.append("- …(more entries not shown)")
            break
    return out


def file(arg: str, chat_id: int) -> str:
    os.makedirs(core.WORKSPACE_DIR, exist_ok=True)
    verb, _, rest = arg.strip().partition(" ")
    verb, rest = verb.lower(), rest.strip()

    if verb == "list":
        rest = _rel(rest)
        root = core._workspace_path(rest) if rest else \
            os.path.realpath(core.WORKSPACE_DIR)
        if not root or not os.path.isdir(root):
            return f"(no such folder in workspace: {rest})"
        lines = _walk_workspace(root)
        where = f"{rest.strip('/')}/" if rest else "workspace"
        if not lines:
            return f"({where} is empty)"
        return f"{where}:\n" + "\n".join(lines)

    if verb == "read":
        rest = _rel(rest)
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
        name, content = _rel(name), content.strip()
        path = core._workspace_path(name)
        if not path:
            return "(refused: path escapes the workspace)"
        if not content:
            return "(nothing to write — use: write name.txt | content)"
        if os.path.isdir(path):
            return f"({name} is a folder, not a file)"
        if not _ensure_parent(path):
            return f"(couldn't create the folder for {name})"
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content + "\n")
        except OSError as e:
            return f"(write failed: {e})"
        return (f"(wrote {name}, {len(content)} chars — this REPLACED "
                f"any previous contents)" + _completeness_hint(path, name))

    if verb == "mkdir":
        name = _rel(rest).rstrip("/")
        path = core._workspace_path(name) if name else None
        if not path:
            return "(refused: path escapes the workspace)"
        if os.path.isfile(path):
            return f"({name} already exists as a file)"
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as e:
            return f"(couldn't create folder: {e})"
        return (f"(folder {name}/ ready — put files in it by using the "
                f"full path, e.g. write {name}/notes.txt | ...)")

    if verb == "append":
        name, _, content = rest.partition("|")
        name, content = _rel(name), content.strip()
        path = core._workspace_path(name)
        if not path:
            return "(refused: path escapes the workspace)"
        if not content:
            return "(nothing to append — use: append name.txt | text)"
        if os.path.isdir(path):
            return f"({name} is a folder, not a file)"
        if not _ensure_parent(path):
            return f"(couldn't create the folder for {name})"
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(content + "\n")
        except OSError as e:
            return f"(append failed: {e})"
        return (f"(appended {len(content)} chars to {name})"
               + _completeness_hint(path, name))

    if verb == "edit":
        name, _, rest2 = rest.partition("|")
        old_text, sep, new_text = rest2.partition("|")
        name, old_text = _rel(name), old_text.strip()
        new_text = new_text.strip()
        if not sep or not name or not old_text:
            return ("(bad format — use: edit name.txt | text to find | "
                    "text to replace it with. To empty a file's line, "
                    "leave the replacement blank.)")
        path = core._workspace_path(name)
        if not path or not os.path.isfile(path):
            return f"(no such file in workspace: {name})"
        try:
            with open(path, "r", encoding="utf-8") as f:
                body = f.read()
        except (OSError, UnicodeDecodeError) as e:
            return f"(edit failed — can't read {name}: {e})"
        count = body.count(old_text)
        if not count:
            return (f"(no match in {name} for: {old_text[:120]} — read "
                    f"the file first and copy the exact text you want "
                    f"to change, including spacing)")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(body.replace(old_text, new_text))
        except OSError as e:
            return f"(edit failed: {e})"
        return (f"(edited {name} — replaced {count} occurrence"
                f"{'' if count == 1 else 's'})" + _completeness_hint(path, name))

    if verb == "send":
        name, _, caption = rest.partition("|")
        name, caption = _rel(name), caption.strip()
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
        return (f"(sent {name} — the user has now actually received "
                f"this file in the chat, {size} bytes)")

    if verb == "download":
        url, _, name = rest.partition("|")
        url, name = url.strip(), _rel(name)
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
        if not _ensure_parent(path):
            return f"(couldn't create the folder for {name})"
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
        return (f"(downloaded {name} into the workspace, {size} bytes. "
                f"The user has NOT received this file — it is only on "
                f"disk. If they asked you for it, your next action must "
                f"be <file>send {name}</file>.)")

    return ("(unknown file command — use: list [folder] | "
            "read <name> | write <name> | <content> | "
            "append <name> | <text> | edit <name> | <find> | <replace> "
            "| mkdir <folder> | download <url> | <name> | "
            "send <name>)")


SKILL = {
    "name": "file",
    "desc": "Work with files in your workspace folder (the ONLY folder "
            "you can access). You may use subfolders anywhere a name "
            "is expected, e.g. notes/2026/july.txt. Grammar: "
            "<file>list</file> or <file>list notes/2026</file>, "
            "<file>read notes.txt</file>, "
            "<file>write notes.txt | the content</file> (this REPLACES "
            "the whole file — to add to one, use append; to change "
            "part of one, use edit). For anything longer than a "
            "paragraph or two — a whole HTML page, several files, a "
            "multi-section document — build it in SEVERAL smaller "
            "steps, not one long piece of text: a single very long "
            "generation can get cut off partway through with no "
            "error, leaving broken content saved to disk. For an "
            "HTML page specifically: write a COMPLETE, PROPERLY "
            "CLOSED skeleton first, with placeholder empty tags — "
            "<style></style>, <script></script> — then use EDIT to "
            "replace those exact empty tags with the real content, "
            "one piece at a time. Do NOT use append to add CSS or "
            "JS after writing a closed skeleton — append only adds "
            "to the very end of the FILE, after </html>, not inside "
            "any tag, so it will not land where you meant it to. "
            "You cannot verify that HTML/JS/CSS you write actually "
            "runs correctly — there is no way to execute it here. "
            "Never tell the user something 'works' or is 'ready to "
            "play'; say you've saved a draft and ask them to open it "
            "and report back what happens. "
            "<file>append notes.txt | a new line</file>, "
            "<file>edit notes.txt | old text | new text</file> (read "
            "the file first and copy the exact text to change), "
            "<file>mkdir notes/2026</file> (folders are also created "
            "automatically when you write into them), "
            "<file>download https://url | saved-name.pdf</file> (always "
            "give the saved name a matching file extension; downloading "
            "only puts a file on disk — it does NOT give it to the "
            "user), "
            "<file>send name.jpg | optional caption</file> to deliver a "
            "workspace file (image/audio/any) to the user in Telegram",
    "handler": file,
}
