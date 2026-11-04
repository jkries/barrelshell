"""Pulse task format, lifecycle metadata and atomic persistence helpers.

No model calls or agent imports. The scheduler and human commands share LOCK;
never hold it while waiting for the agent/model lock or running a task.
"""
from collections import Counter
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading

from croniter import croniter

LOCK = threading.RLock()
LAST_CONTACT_KEY = "_last_user_contact"
COMPLETED_KEY = "_completed_once"
HEADING_RE = re.compile(r"^#{2,}[ \t]*(\S[^\n]*?)[ \t]*$", re.MULTILINE)
DURATION_RE = re.compile(r"(\d+)\s*([mhd])", re.IGNORECASE)
TIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?)?", re.I)


def parse_time(value: str, *, end_of_date=False, require_time=False) -> datetime:
    """Naive timestamps use host local time; offsets are converted to it.

    A date-only expiry includes that whole day (expires at next midnight).
    @once deliberately requires a time rather than guessing one.
    """
    value = value.strip()
    if not TIME_RE.fullmatch(value) or (require_time and len(value) == 10):
        raise ValueError("use YYYY-MM-DD HH:MM or an ISO timestamp (optional timezone offset)")
    result = datetime.fromisoformat(value.upper().replace("Z", "+00:00"))
    if result.tzinfo is not None:
        result = result.astimezone().replace(tzinfo=None)
    if end_of_date and len(value) == 10:
        result += timedelta(days=1)
    return result


def parse_duration(text: str):
    match = DURATION_RE.fullmatch(text.strip())
    if not match:
        return None
    if len(match[1]) > 9:
        return None
    number, unit = int(match[1]), match[2].lower()
    if number <= 0:
        return None
    try:
        return timedelta(**{{"m": "minutes", "h": "hours", "d": "days"}[unit]: number})
    except OverflowError:
        return None


def schedule_kind(schedule: str):
    """Return (kind, timedelta/datetime/None); invalid schedules return None."""
    if not isinstance(schedule, str) or "\n" in schedule or "\r" in schedule:
        return None, None
    schedule = schedule.strip()
    if schedule.lower() == "@reboot":
        return "reboot", None
    for prefix, kind in (("@every", "every"), ("@idle", "idle"), ("@once", "once")):
        prefix_matches = (schedule.lower().startswith(prefix) if kind != 'once' else
                          bool(re.match(r'@once\s+', schedule, re.I)))
        if prefix_matches:
            value = schedule[len(prefix):].strip()
            if kind == "once":
                try:
                    return kind, parse_time(value, require_time=True)
                except (ValueError, OverflowError):
                    return None, None
            duration = parse_duration(value)
            return (kind, duration) if duration else (None, None)
    # Preserve croniter aliases and extended cron syntax accepted by earlier
    # releases; the documented/default grammar remains five-field cron.
    return ("cron", None) if croniter.is_valid(schedule) else (None, None)


def sections(text: str):
    headings = list(HEADING_RE.finditer(text))
    return [(m[1].strip(), m.start(), headings[i + 1].start() if i + 1 < len(headings) else len(text))
            for i, m in enumerate(headings)]


def parse(text: str):
    tasks, problems = [], []
    blocks = sections(text)
    counts = Counter(name.casefold() for name, _, _ in blocks)
    for name, start, end in blocks:
        if counts[name.casefold()] > 1:
            problems.append(f"task '{name}': duplicate name (case-insensitive) — ignored; resolve duplicates manually")
            continue
        if name.startswith("_"):
            problems.append(f"task '{name}': names starting with '_' are reserved — ignored")
            continue
        lines = text[start:end].splitlines()[1:]
        if not lines or not re.match(r"^schedule:", lines[0].strip(), re.I):
            problems.append(f"'{name}' has no 'schedule:' line directly after its heading — ignored")
            continue
        schedule = lines.pop(0).strip().split(":", 1)[1].strip()
        kind, when = schedule_kind(schedule)
        if kind is None:
            problems.append(f"task '{name}': invalid schedule '{schedule}' — use cron, @reboot, @every 30m, @idle 4h or @once YYYY-MM-DD HH:MM")
            continue
        expiry = None
        invalid = False
        # Metadata must be adjacent to schedule. After a blank line it is
        # prompt text, so a model-written prompt cannot smuggle lifecycle data.
        while lines and re.match(r"^expires:", lines[0].strip(), re.I):
            value = lines.pop(0).strip().split(":", 1)[1].strip()
            try:
                if expiry is not None:
                    raise ValueError("duplicate expires field")
                expiry = parse_time(value, end_of_date=True).isoformat(timespec="seconds")
            except (ValueError, OverflowError) as exc:
                problems.append(f"task '{name}': invalid expires field ({exc}) — ignored")
                invalid = True
                break
        if invalid:
            continue
        prompt = "\n".join(lines).strip()
        if not prompt:
            problems.append(f"task '{name}': empty prompt — ignored")
            continue
        if kind == "once" and expiry and parse_time(expiry) <= when:
            problems.append(f"task '{name}': expires must be after the @once time — ignored")
            continue
        tasks.append({"name": name, "cron": schedule, "prompt": prompt, "expires": expiry})
    return tasks, sorted(set(problems))


def expired(task, now):
    return bool(task.get("expires") and now >= parse_time(task["expires"]))


def fingerprint(task):
    content = [task["name"], task["cron"], task["prompt"], task.get("expires")]
    return hashlib.sha256(json.dumps(content, ensure_ascii=False).encode()).hexdigest()


def completed(task, state):
    if schedule_kind(task['cron'])[0] != 'once':
        return False
    markers = state.get(COMPLETED_KEY, {})
    return (isinstance(markers, dict)
            and markers.get(task["name"]) == fingerprint(task))


def remove_sections(text, names):
    """Exact, case-insensitive names; refuse ambiguity rather than delete many."""
    wanted = {name.casefold() for name in names}
    matches = [(name, start, end) for name, start, end in sections(text) if name.casefold() in wanted]
    counts = Counter(name.casefold() for name, _, _ in matches)
    if any(count > 1 for count in counts.values()):
        raise ValueError("duplicate task name; resolve duplicate headings manually before removal")
    for _, start, end in reversed(matches):
        text = text[:start] + text[end:]
    return text, [name for name, _, _ in matches]


def read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return ""


def atomic_write(path, text):
    path = Path(path).absolute()
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load_json(path, default):
    try:
        return json.loads(read_text(path))
    except (ValueError, UnicodeError):
        return default


def save_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def forget_state(state, names):
    names = {name.casefold() for name in names}
    for key in list(state):
        if not key.startswith("_") and key.casefold() in names:
            del state[key]
    markers = state.get(COMPLETED_KEY)
    if isinstance(markers, dict):
        for key in list(markers):
            if key.casefold() in names:
                del markers[key]
        if not markers:
            state.pop(COMPLETED_KEY, None)
