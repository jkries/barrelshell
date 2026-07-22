#!/usr/bin/env python3
"""
BarrelShell — a self-hosted AI agent platform.  (barrelshell.com)

The Shell is the platform: a local model via Ollama, this Python
core, and the settings and skills around it. A running instance of
the bot is a Barrel — yours is defined entirely by editable files
living next to this script.

Architecture in one breath: identity.md (persona) + history.md
(durable facts) + a generated tool protocol are assembled into the
system prompt every turn; the agent loop lets the model call tools
via tags; pulse.md schedules proactive behavior; skills/*.py add
drop-in tools; config.json overrides tunables; a localhost dashboard
(dashboard.html) shows live state and chats without Telegram.

Trust tiers: read-only tools fire freely; contained tools (workspace/
file access, sending to the requesting chat only) fire with logging;
self-modifying capability (pulse tasks) requires a human /approve —
a command the model cannot invoke.

This file is platform release 1 (barrel_v1).

Requires: pip install ollama ddgs requests croniter tzdata
Env:      TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_IDS, [PULSE_CHAT_ID]
"""

import ast
import base64
import ipaddress
import json
import math
import mimetypes
import os
import re
import socket
import threading
import time
import random
from datetime import datetime, date, timedelta
from html.parser import HTMLParser
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlparse

import ollama
import requests
from croniter import croniter
from ddgs import DDGS

# ---------------------------------------------------------------- config

# Structural files — fixed names, not configurable.
IDENTITY_FILE = "identity.md"
HISTORY_FILE = "history.md"
PULSE_FILE = "pulse.md"
PULSE_STATE_FILE = "pulse_state.json"
REMINDERS_FILE = "reminders.json"
LOG_FILE = "agent_log.jsonl"
TOKEN_USAGE_FILE = "token_usage.json"
CONFIG_FILE = "config.json"
SKILLS_DIR = "skills"

PENDING_PULSE_FILE = "pulse_pending.json"
DASHBOARD_HTML = "dashboard.html"
WEB_CHAT_ID = 0   # web-chat conversation id (Telegram ids are positive)

# Tunables — defaults here, user overrides in config.json.
DEFAULTS = {
    "model": "qwen3:8b",
    "num_ctx": 16384,
    "max_tool_rounds": 3,
    "max_turns": 40,
    "search_results": 5,
    "fetch_max_chars": 4000,
    "download_max_bytes": 20_000_000,
    "pulse_check_seconds": 30,
    "dashboard_host": "127.0.0.1",
    "dashboard_port": 8787,
    "workspace_dir": "workspace",
    "vision_model": "",
}


def _load_config() -> dict:
    """config.json merged over DEFAULTS. Unknown keys are ignored with
    a warning; a missing or broken file just means defaults — so a
    core update that adds a setting never breaks an existing config."""
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_FILE) as f:
            user = json.load(f)
    except FileNotFoundError:
        return cfg
    except (json.JSONDecodeError, OSError) as e:
        print(f"config: {CONFIG_FILE} unreadable ({e}) — using defaults")
        return cfg
    unknown = set(user) - set(DEFAULTS) - {"_readme"}
    if unknown:
        print(f"config: ignoring unknown keys: {', '.join(sorted(unknown))}")
    cfg.update({k: user[k] for k in user if k in DEFAULTS})
    print(f"config: loaded {CONFIG_FILE}")
    return cfg


_cfg = _load_config()
MODEL = str(_cfg["model"])
NUM_CTX = int(_cfg["num_ctx"])
MAX_TOOL_ROUNDS = int(_cfg["max_tool_rounds"])
MAX_TURNS = int(_cfg["max_turns"])
SEARCH_RESULTS = int(_cfg["search_results"])
FETCH_MAX_CHARS = int(_cfg["fetch_max_chars"])
DOWNLOAD_MAX_BYTES = int(_cfg["download_max_bytes"])
PULSE_CHECK_SECONDS = int(_cfg["pulse_check_seconds"])
DASHBOARD_HOST = str(_cfg["dashboard_host"])
DASHBOARD_PORT = int(_cfg["dashboard_port"])
WORKSPACE_DIR = str(_cfg["workspace_dir"])
VISION_MODEL = str(_cfg["vision_model"]).strip()

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_IDS = {int(x) for x in
               os.environ.get("TELEGRAM_ALLOWED_IDS", "").split(",") if x}
PULSE_CHAT_ID = int(os.environ.get("PULSE_CHAT_ID",
                                   next(iter(ALLOWED_IDS), 0)))
API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Dashboard binds to localhost ONLY (config: dashboard_host) — it
# displays private conversation content. To view from another device,
# SSH-tunnel the port; don't bind wider.
OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
START_TIME = datetime.now()

# ------------------------------------------------------------- turn log

def log_event(event: str, **data) -> None:
    """One JSON line per event. This is your black box recorder —
    when the bot misbehaves, the answer is in here."""
    try:
        entry = {"ts": datetime.now().isoformat(timespec="seconds"),
                 "event": event, **data}
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"log write failed: {e}")

# ---------------------------------------------------------- basic files

def read_file(path: str, default: str = "") -> str:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    return default


def load_json(path: str, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: str, data) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ------------------------------------------------------- token tracking

token_usage: dict = {}  # loaded in main(); persisted per model call


def _count(resp, field: str) -> int:
    """Read a numeric field off an Ollama response, tolerating both
    dict and pydantic-model response types across ollama lib versions."""
    try:
        return int(resp[field] or 0)
    except (KeyError, TypeError, IndexError):
        return int(getattr(resp, field, 0) or 0)


def track_tokens(t_in: int, t_out: int) -> None:
    """Session counters + lifetime file in one place — every model
    call (chat, pulse, vision) accounts identically."""
    stats["session_tokens_in"] += t_in
    stats["session_tokens_out"] += t_out
    record_tokens(t_in, t_out)


def record_tokens(t_in: int, t_out: int) -> None:
    """Accumulate lifetime + per-day token usage in token_usage.json.
    One JSON entry per day keeps the file small forever."""
    life = token_usage.setdefault("lifetime",
                                  {"in": 0, "out": 0, "calls": 0})
    life["in"] += t_in
    life["out"] += t_out
    life["calls"] += 1
    day = token_usage.setdefault("daily", {}).setdefault(
        date.today().isoformat(), {"in": 0, "out": 0})
    day["in"] += t_in
    day["out"] += t_out
    save_json(TOKEN_USAGE_FILE, token_usage)

# ---------------------------------------------------------- tool: search

def run_search(query: str, chat_id: int) -> str:
    try:
        results = DDGS().text(query, max_results=SEARCH_RESULTS)
    except Exception as e:
        return f"(search failed: {e})"
    if not results:
        return "(no results)"
    return "\n".join(f"- {r.get('title','')}\n  {r.get('href','')}\n"
                     f"  {r.get('body','')}" for r in results)

# ----------------------------------------------------------- tool: fetch

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


def _is_private_host(hostname: str) -> bool:
    """SSRF guard: refuse URLs that resolve to loopback/private/link-local
    addresses. Without this, a prompt-injected fetch could probe your LAN
    (router admin pages, internal services) through the bot."""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return True  # unresolvable — refuse
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved):
            return True
    return False


def run_fetch(url: str, chat_id: int) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "(fetch refused: only http/https URLs)"
    if not parsed.hostname or _is_private_host(parsed.hostname):
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
    if len(text) > FETCH_MAX_CHARS:
        text = text[:FETCH_MAX_CHARS] + " …(truncated)"
    return text or "(page had no extractable text)"

# --------------------------------------------------------- tool: weather

_WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
        45: "fog", 48: "rime fog", 51: "light drizzle", 53: "drizzle",
        55: "heavy drizzle", 61: "light rain", 63: "rain",
        65: "heavy rain", 66: "freezing rain", 71: "light snow",
        73: "snow", 75: "heavy snow", 77: "snow grains",
        80: "rain showers", 81: "rain showers", 82: "violent showers",
        85: "snow showers", 86: "snow showers", 95: "thunderstorm",
        96: "thunderstorm w/ hail", 99: "thunderstorm w/ hail"}


def run_weather(place: str, chat_id: int) -> str:
    place = place.strip()
    zip_m = re.fullmatch(r"(\d{5})(?:-\d{4})?", place)
    try:
        if zip_m:  # US ZIP — Open-Meteo's geocoder mishandles these
            z = requests.get(
                f"https://api.zippopotam.us/us/{zip_m.group(1)}",
                timeout=15).json()
            p = z["places"][0]
            loc = {"latitude": float(p["latitude"]),
                   "longitude": float(p["longitude"]),
                   "name": p["place name"],
                   "admin1": p["state abbreviation"],
                   "country_code": "US"}
        else:
            geo = requests.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": place, "count": 1}, timeout=15).json()
            if not geo.get("results"):
                return f"(no location found for '{place}')"
            loc = geo["results"][0]
        wx = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": loc["latitude"], "longitude": loc["longitude"],
                "current": "temperature_2m,apparent_temperature,"
                           "weather_code,wind_speed_10m,precipitation",
                "daily": "temperature_2m_max,temperature_2m_min,"
                         "precipitation_probability_max,weather_code",
                "temperature_unit": "fahrenheit",
                "wind_speed_unit": "mph",
                "timezone": "auto", "forecast_days": 3},
            timeout=15).json()
    except (requests.RequestException, KeyError, IndexError, ValueError) as e:
        return f"(weather lookup failed: {e})"

    cur = wx["current"]
    lines = [f"Weather for {loc['name']}, "
             f"{loc.get('admin1', '')} {loc.get('country_code', '')}:",
             f"Now: {cur['temperature_2m']}°F "
             f"(feels {cur['apparent_temperature']}°F), "
             f"{_WMO.get(cur['weather_code'], 'unknown')}, "
             f"wind {cur['wind_speed_10m']} mph"]
    d = wx["daily"]
    for i, day in enumerate(d["time"]):
        lines.append(f"{day}: {d['temperature_2m_min'][i]:.0f}–"
                     f"{d['temperature_2m_max'][i]:.0f}°F, "
                     f"{_WMO.get(d['weather_code'][i], '?')}, "
                     f"precip {d['precipitation_probability_max'][i]}%")
    return "\n".join(lines)

# -------------------------------------------------------- tool: reminders

def run_remind(arg: str, chat_id: int) -> str:
    if "|" not in arg:
        return ("(bad format — use: "
                "<remind>YYYY-MM-DD HH:MM | message</remind>)")
    when_s, message = (part.strip() for part in arg.split("|", 1))
    try:
        due = datetime.strptime(when_s, "%Y-%m-%d %H:%M")
    except ValueError:
        return f"(could not parse '{when_s}' — use YYYY-MM-DD HH:MM)"
    if due <= datetime.now():
        return f"(that time is in the past — it is now "\
               f"{datetime.now().strftime('%Y-%m-%d %H:%M')})"
    if not message:
        return "(reminder needs a message after the |)"
    reminders = load_json(REMINDERS_FILE, [])
    reminders.append({"due": due.isoformat(timespec="minutes"),
                      "message": message, "chat_id": chat_id})
    save_json(REMINDERS_FILE, reminders)
    log_event("reminder_set", due=reminders[-1]["due"], message=message)
    return f"(reminder saved for {when_s}: {message})"

# ----------------------------------------------------- tool: forget

def run_forget(arg: str, chat_id: int) -> str:
    """Remove exactly one history line. Unique-match-or-refuse: the
    destructive op only fires when the target is unambiguous."""
    needle = arg.strip().lower()
    if len(needle) < 3:
        return "(give at least 3 characters of the fact to forget)"
    lines = read_file(HISTORY_FILE).splitlines()
    hits = [i for i, l in enumerate(lines)
            if l.lstrip().startswith("-") and needle in l.lower()]
    if not hits:
        return f"(no history entry contains '{arg.strip()}')"
    if len(hits) > 1:
        listing = "\n".join(lines[i] for i in hits[:10])
        return (f"({len(hits)} entries match — give a more specific "
                f"phrase:\n{listing})")
    removed = lines.pop(hits[0])
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).strip() + "\n")
    log_event("history_forgot", line=removed)
    return f"(removed from history: {removed.lstrip('- ').strip()})"


# ------------------------------------------------------- tool: calc

_CALC_FUNCS = {"sqrt": math.sqrt, "abs": abs, "round": round,
               "floor": math.floor, "ceil": math.ceil,
               "log": math.log, "log10": math.log10,
               "log2": math.log2, "sin": math.sin, "cos": math.cos,
               "tan": math.tan, "min": min, "max": max}
_CALC_NAMES = {"pi": math.pi, "e": math.e}
_CALC_OPS = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b,
             ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b,
             ast.FloorDiv: lambda a, b: a // b,
             ast.Mod: lambda a, b: a % b, ast.Pow: lambda a, b: a ** b}


def _calc_eval(node):
    """Whitelist-based AST walk — the safe alternative to eval().
    Anything not explicitly allowed raises, including names,
    attributes, subscripts, and comprehensions."""
    if isinstance(node, ast.Expression):
        return _calc_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value,
                                                    (int, float)):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(
            node.op, (ast.USub, ast.UAdd)):
        v = _calc_eval(node.operand)
        return -v if isinstance(node.op, ast.USub) else v
    if isinstance(node, ast.BinOp) and type(node.op) in _CALC_OPS:
        a, b = _calc_eval(node.left), _calc_eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(b) > 256:
            raise ValueError("exponent too large")
        return _CALC_OPS[type(node.op)](a, b)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in _CALC_FUNCS and not node.keywords):
        return _CALC_FUNCS[node.func.id](
            *[_calc_eval(a) for a in node.args])
    if isinstance(node, ast.Name) and node.id in _CALC_NAMES:
        return _CALC_NAMES[node.id]
    raise ValueError(f"unsupported: {type(node).__name__}")


def run_calc(arg: str, chat_id: int) -> str:
    expr = arg.strip().replace("^", "**")
    try:
        val = _calc_eval(ast.parse(expr, mode="eval"))
    except (ValueError, TypeError, SyntaxError, ZeroDivisionError,
            OverflowError) as e:
        return f"(couldn't evaluate: {e})"
    if isinstance(val, float):
        val = int(val) if (val == int(val) and abs(val) < 1e15) \
            else round(val, 10)
    if isinstance(val, int) and len(str(val)) > 18:
        return f"{arg.strip()} ≈ {float(val):.6e}"
    return f"{arg.strip()} = {val}"


# ------------------------------------------------------ tool: clock

_TZ_ALIASES = {
    "utc": "UTC", "gmt": "UTC",
    "eastern": "America/New_York", "newyork": "America/New_York",
    "nyc": "America/New_York", "central": "America/Chicago",
    "chicago": "America/Chicago", "mountain": "America/Denver",
    "denver": "America/Denver", "pacific": "America/Los_Angeles",
    "losangeles": "America/Los_Angeles", "la": "America/Los_Angeles",
    "anchorage": "America/Anchorage", "honolulu": "Pacific/Honolulu",
    "toronto": "America/Toronto", "mexicocity": "America/Mexico_City",
    "saopaulo": "America/Sao_Paulo", "london": "Europe/London",
    "paris": "Europe/Paris", "berlin": "Europe/Berlin",
    "madrid": "Europe/Madrid", "rome": "Europe/Rome",
    "amsterdam": "Europe/Amsterdam", "athens": "Europe/Athens",
    "istanbul": "Europe/Istanbul", "moscow": "Europe/Moscow",
    "cairo": "Africa/Cairo", "johannesburg": "Africa/Johannesburg",
    "dubai": "Asia/Dubai", "delhi": "Asia/Kolkata",
    "mumbai": "Asia/Kolkata", "kolkata": "Asia/Kolkata",
    "singapore": "Asia/Singapore", "hongkong": "Asia/Hong_Kong",
    "shanghai": "Asia/Shanghai", "beijing": "Asia/Shanghai",
    "seoul": "Asia/Seoul", "tokyo": "Asia/Tokyo",
    "perth": "Australia/Perth", "sydney": "Australia/Sydney",
    "auckland": "Pacific/Auckland",
}


def _resolve_tz(name: str):
    raw = name.strip()
    for candidate in (raw, _TZ_ALIASES.get(
            re.sub(r"[\s_.-]", "", raw.lower()), "")):
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except Exception:
            continue
    return None


CONVERT_RE = re.compile(
    r"convert\s+(\d{1,2}):(\d{2})\s+(.+?)\s+to\s+(.+)", re.I)


def run_clock(arg: str, chat_id: int) -> str:
    arg = arg.strip()
    m = CONVERT_RE.fullmatch(arg)
    if m:
        hh, mm, src_name, dst_name = (int(m.group(1)), int(m.group(2)),
                                      m.group(3), m.group(4))
        src_tz, dst_tz = _resolve_tz(src_name), _resolve_tz(dst_name)
        if not src_tz or not dst_tz:
            bad = src_name if not src_tz else dst_name
            return (f"(unknown timezone '{bad.strip()}' — use IANA "
                    f"names like Asia/Tokyo; on Windows, tzdata must "
                    f"be installed)")
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return "(invalid time — use 24h HH:MM)"
        src_dt = datetime.now(src_tz).replace(hour=hh, minute=mm,
                                              second=0, microsecond=0)
        dst_dt = src_dt.astimezone(dst_tz)
        return (f"{hh:02d}:{mm:02d} in {src_tz.key} = "
                f"{dst_dt.strftime('%H:%M (%A)')} in {dst_tz.key}")
    names = [n for n in re.split(r"[,;]+", arg) if n.strip()] or ["UTC"]
    lines = [f"Local time here: "
             f"{datetime.now().strftime('%H:%M (%A %Y-%m-%d)')}"]
    for name in names[:8]:
        tz = _resolve_tz(name)
        if not tz:
            lines.append(f"{name.strip()}: (unknown timezone — use "
                         f"IANA names like Asia/Tokyo)")
            continue
        lines.append(f"{tz.key}: "
                     f"{datetime.now(tz).strftime('%H:%M (%A %Y-%m-%d)')}")
    return "\n".join(lines)


# ------------------------------------------------------ tool: files

def _workspace_path(name: str):
    """Resolve a filename inside the workspace; refuse escapes.
    realpath + commonpath defeats '../', absolute paths, and symlink
    tricks in one check. Keeps identity.md / pulse.md / history.md
    and everything else outside workspace/ unreachable."""
    base = os.path.realpath(WORKSPACE_DIR)
    target = os.path.realpath(os.path.join(base, name))
    if os.path.commonpath([base, target]) != base:
        return None
    return target


def _sniff_ext(path: str) -> str:
    """Best-effort file type from magic bytes, for files that arrived
    without a usable extension. Models routinely name downloads
    'fox_picture' with no suffix, which leaves the file unopenable
    and unroutable when sent to chat."""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
    except OSError:
        return ""
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head.startswith(b"GIF8"):
        return ".gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return ".wav"
    if head.startswith(b"ID3") or head[:2] == b"\xff\xfb":
        return ".mp3"
    if head.startswith(b"OggS"):
        return ".ogg"
    if head.startswith(b"fLaC"):
        return ".flac"
    if head[4:8] == b"ftyp" and head[8:11] == b"M4A":
        return ".m4a"
    if head.startswith(b"%PDF"):
        return ".pdf"
    return ""


def run_file(arg: str, chat_id: int) -> str:
    os.makedirs(WORKSPACE_DIR, exist_ok=True)
    verb, _, rest = arg.strip().partition(" ")
    verb, rest = verb.lower(), rest.strip()

    if verb == "list":
        entries = sorted(os.listdir(WORKSPACE_DIR))
        if not entries:
            return "(workspace is empty)"
        return "\n".join(
            f"- {n} ({os.path.getsize(os.path.join(WORKSPACE_DIR, n))}"
            f" bytes)" for n in entries)

    if verb == "read":
        path = _workspace_path(rest)
        if not path or not os.path.isfile(path):
            return f"(no such file in workspace: {rest})"
        try:
            with open(path, "r", encoding="utf-8",
                      errors="replace") as f:
                text = f.read(FETCH_MAX_CHARS + 1)
        except OSError as e:
            return f"(read failed: {e})"
        if len(text) > FETCH_MAX_CHARS:
            text = text[:FETCH_MAX_CHARS] + " …(truncated)"
        return text or "(file is empty)"

    if verb == "write":
        name, _, content = rest.partition("|")
        name, content = name.strip(), content.strip()
        path = _workspace_path(name)
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
        path = _workspace_path(name)
        if not path or not os.path.isfile(path):
            return f"(no such file in workspace: {name})"
        size = os.path.getsize(path)
        if size > 50_000_000:
            return "(file exceeds Telegram's 50 MB bot limit)"
        if chat_id == WEB_CHAT_ID:
            return (f"(files can't be pushed into the web chat — give "
                    f"the user this link to open it: "
                    f"http://{DASHBOARD_HOST}:{DASHBOARD_PORT}"
                    f"/workspace/{quote(name)} )")
        ext = os.path.splitext(name)[1].lower()
        if not ext:
            # Repair a missing extension from the file's own bytes, so
            # it routes correctly AND arrives openable.
            sniffed = _sniff_ext(path)
            if sniffed:
                fixed = _workspace_path(name + sniffed)
                try:
                    if fixed and not os.path.exists(fixed):
                        os.rename(path, fixed)
                        path, name, ext = fixed, name + sniffed, sniffed
                except OSError:
                    ext = sniffed   # send it anyway, just don't rename
        photo_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
        audio_exts = {".mp3", ".m4a", ".ogg", ".oga", ".wav", ".flac"}
        try:
            if ext in photo_exts and size <= 10_000_000:
                try:
                    tg_upload("sendPhoto", chat_id, "photo", path,
                              caption)
                except requests.RequestException:
                    # Telegram rejects some photos (odd dimensions or
                    # formats) — retry uncompressed as a document.
                    tg_upload("sendDocument", chat_id, "document",
                              path, caption)
            elif ext in audio_exts:
                tg_upload("sendAudio", chat_id, "audio", path, caption)
            else:
                tg_upload("sendDocument", chat_id, "document", path,
                          caption)
        except requests.RequestException as e:
            return f"(send failed: {e.__class__.__name__})"
        log_event("file_sent", name=name, bytes=size, chat_id=chat_id)
        return f"(sent {name} to the chat — {size} bytes)"

    if verb == "download":
        url, _, name = rest.partition("|")
        url, name = url.strip(), name.strip()
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return "(download refused: only http/https URLs)"
        if not parsed.hostname or _is_private_host(parsed.hostname):
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
            # No extension given — take it from the URL, else from the
            # server's content-type. A file named "fox_picture" is
            # useless to every program that opens it later.
            ext = os.path.splitext(unquote(parsed.path))[1]
            if not ext:
                ctype = r.headers.get("content-type", "").split(";")[0]
                ext = mimetypes.guess_extension(ctype.strip()) or ""
                ext = {".jpe": ".jpg", ".jpeg": ".jpg"}.get(ext, ext)
            name += ext or ".bin"
        path = _workspace_path(name)
        if not path:
            return "(refused: filename escapes the workspace)"
        try:
            size = 0
            with open(path, "wb") as f:
                for chunk in r.iter_content(65536):
                    size += len(chunk)
                    if size > DOWNLOAD_MAX_BYTES:
                        f.close()
                        os.remove(path)
                        return "(download refused: exceeds size cap)"
                    f.write(chunk)
        except requests.RequestException as e:
            return f"(download failed: {e})"
        return f"(downloaded {name}, {size} bytes)"

    return ("(unknown file command — use: list | read <name> | "
            "write <name> | <content> | download <url> | <name>)")


# ----------------------------------------- tool: pulse (propose only)

PULSE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")


def run_pulse(arg: str, chat_id: int) -> str:
    """Trust-tier three: the model may PROPOSE a scheduled task but can
    never activate one. Proposals land in pulse_pending.json and the
    user is notified; only the deterministic /approve command — which
    runs in code the model cannot invoke or fake — writes pulse.md.
    This keeps prompt injection from persisting behavior."""
    verb, _, rest = arg.strip().partition(" ")
    verb, rest = verb.lower(), rest.strip()

    if verb == "list":
        active = [f"- {t['name']} ({t['cron']}) [active]"
                  for t in parse_pulse()]
        pending = [f"- {n} ({p['cron']}) [PENDING /approve {n}]"
                   for n, p in load_json(PENDING_PULSE_FILE, {}).items()]
        return "\n".join(active + pending) or "(no pulse tasks)"

    if verb == "add":
        parts = [p.strip() for p in rest.split("|", 2)]
        if len(parts) != 3 or not all(parts):
            return ("(bad format — use: add task-name | cron | "
                    "what to do)")
        name, cron, prompt = parts
        name = name.lower()
        if not PULSE_NAME_RE.match(name):
            return ("(bad task name — lowercase letters, digits, "
                    "hyphens, 2-41 chars)")
        if cron != "@reboot" and not croniter.is_valid(cron):
            return f"(invalid cron expression: {cron})"
        if any(t["name"] == name for t in parse_pulse()):
            return f"(a task named '{name}' already exists in pulse.md)"
        pending = load_json(PENDING_PULSE_FILE, {})
        if name in pending:
            return (f"('{name}' is already proposed and awaiting "
                    f"/approve {name})")
        pending[name] = {"cron": cron, "prompt": prompt,
                         "proposed": datetime.now().isoformat(
                             timespec="seconds")}
        save_json(PENDING_PULSE_FILE, pending)
        log_event("pulse_proposed", name=name, cron=cron,
                  prompt=prompt[:200])
        try:
            send_message(PULSE_CHAT_ID,
                         f"\U0001f552 Proposed pulse task '{name}' "
                         f"({cron}):\n{prompt}\n\nSend /approve "
                         f"{name} to enable or /reject {name} to "
                         f"discard.")
        except Exception as e:
            print(f"pulse proposal notify failed: {e!r}")
        return (f"(proposed '{name}' — it is NOT active yet. The user "
                f"must send /approve {name} to enable it; tell them "
                f"so.)")

    return "(unknown pulse command — use: add name | cron | prompt, "\
           "or list)"


# ---------------------------------------------------- tool: self-status

def run_status(arg: str, chat_id: int) -> str:
    """Grounded self-knowledge: report live runtime facts so the model
    never has to guess about itself. gather_status() lives in the
    dashboard section — resolved at call time, so the forward
    reference is fine."""
    s = gather_status()
    st = s["stats"]
    tk = s["tokens"]
    loaded = "; ".join(
        f"{m['name']} using {m['vram_gb']} of {m['total_gb']} GB in VRAM"
        for m in s["ollama_loaded"]) or "nothing currently loaded"
    lines = [
        "Agent: a Barrel (BarrelShell platform)",
        f"Status: {s['status']}, uptime {s['uptime']} "
        f"(started {s['started']})",
        f"Configured model: {s['model']}, context window "
        f"{s['num_ctx']} tokens",
        f"Vision model: {s['vision_model']}",
        f"Ollama: {s['ollama']}; loaded: {loaded}",
        f"Skills: {', '.join(TOOLS)}",
        f"Turns this session: {st['turns']}; facts saved: "
        f"{st['facts_saved']}; errors: {st['errors']}",
        f"Tokens this session: {st['session_tokens_in']} in / "
        f"{st['session_tokens_out']} out",
    ]
    if st["last_prompt_tokens"]:
        lines.append(f"Context used last call: "
                     f"~{st['last_prompt_tokens']} of "
                     f"{s['num_ctx']} tokens")
    if st["last_tps"]:
        lines.append(f"Generation speed (last): "
                     f"{st['last_tps']} tokens/sec")
    if tk["lifetime"]:
        lines.append(f"Lifetime tokens: {tk['lifetime']['in']} in / "
                     f"{tk['lifetime']['out']} out over "
                     f"{tk['lifetime']['calls']} model calls")
    if tk["today"]:
        lines.append(f"Today: {tk['today']['in']} in / "
                     f"{tk['today']['out']} out")
    lines.append(f"Pulse tasks: {len(s['pulse_tasks'])}; pending "
                 f"reminders: {len(s['pending_reminders'])}")
    return "\n".join(lines)

# --------------------------------------------------------- tool registry

TOOLS = {
    "search": {
        "handler": run_search,
        "desc": "Search the web for current events or anything that may "
                "have changed since your training. Emit "
                "<search>concise query</search>",
    },
    "fetch": {
        "handler": run_fetch,
        "desc": "Read the full text of one web page — use when search "
                "snippets aren't enough, on a URL from results or from "
                "the user. Emit <fetch>https://full.url/here</fetch>",
    },
    "weather": {
        "handler": run_weather,
        "desc": "Get current conditions and a 3-day forecast for a "
                "city name or US ZIP code. Emit "
                "<weather>city or place name</weather>",
    },
    "forget": {
        "handler": run_forget,
        "desc": "Remove ONE outdated or wrong fact from your saved "
                "history — use when the user corrects or retracts "
                "something you have saved. Give a distinctive phrase "
                "from that entry; if several match you'll get a list "
                "to narrow. Emit <forget>distinctive phrase</forget>",
    },
    "calc": {
        "handler": run_calc,
        "desc": "Do precise arithmetic — never compute multi-step "
                "math in your head. Supports + - * / ** % (), and "
                "sqrt, log, sin, cos, round, floor, ceil, min, max, "
                "abs, pi, e. Emit <calc>(17.5*12)/0.85</calc>",
    },
    "clock": {
        "handler": run_clock,
        "desc": "Current time in other timezones, or convert a time "
                "between zones. Emit <clock>tokyo</clock>, "
                "<clock>Asia/Tokyo, Europe/London</clock>, or "
                "<clock>convert 09:00 America/New_York to "
                "Asia/Tokyo</clock>",
    },
    "file": {
        "handler": run_file,
        "desc": "Work with files in your workspace folder (the ONLY "
                "folder you can access). Grammar: <file>list</file>, "
                "<file>read notes.txt</file>, "
                "<file>write notes.txt | the content</file>, "
                "<file>download https://url | saved-name.pdf</file> "
                "(always give the saved name a matching file "
                "extension), "
                "<file>send name.jpg | optional caption</file> to "
                "deliver a workspace file (image/audio/any) to the "
                "user in Telegram",
    },
    "pulse": {
        "handler": run_pulse,
        "desc": "Propose a recurring/scheduled task. You can only "
                "PROPOSE — the user must approve with /approve before "
                "it activates, so always tell them that. Grammar: "
                "<pulse>add task-name | cron like '0 15 * * *' or "
                "@reboot | what to do</pulse>, or <pulse>list</pulse>. "
                "Use when asked for something daily, weekly, or "
                "recurring.",
    },
    "status": {
        "handler": run_status,
        "desc": "Report your own live runtime status: uptime, model, "
                "token usage, context fill, and current skill list. "
                "Use when asked about yourself, your model, your "
                "capabilities, or your usage. Emit "
                "<status>report</status>",
    },
    "remind": {
        "handler": run_remind,
        "desc": "Set a one-time reminder. Convert the user's request to "
                "an absolute time using the current date/time you were "
                "given. Emit <remind>YYYY-MM-DD HH:MM | message</remind>",
    },
}

def load_skills() -> None:
    """Merge drop-in skills from skills/*.py into the registry.
    Contract per file:  SKILL = {"name", "desc", "handler"}  where
    handler(arg, chat_id) -> str and name is the (lowercase) tag.
    A user skill may override a built-in — loudly. SKILLS ARE CODE:
    they execute with the bot's full permissions at load time; read
    anything you didn't write before dropping it in this folder."""
    if not os.path.isdir(SKILLS_DIR):
        return
    import importlib.util
    for fname in sorted(os.listdir(SKILLS_DIR)):
        if not fname.endswith(".py") or fname.startswith("_"):
            continue
        path = os.path.join(SKILLS_DIR, fname)
        try:
            spec = importlib.util.spec_from_file_location(
                f"barrel_skill_{fname[:-3]}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception as e:
            print(f"skills: FAILED to load {fname}: {e!r}")
            continue
        skill = getattr(mod, "SKILL", None)
        name = str((skill or {}).get("name", ""))
        if not (isinstance(skill, dict) and skill.get("desc")
                and callable(skill.get("handler"))
                and re.fullmatch(r"[a-z0-9_]+", name)):
            print(f"skills: {fname} skipped — needs SKILL dict with "
                  f"lowercase name, desc, handler(arg, chat_id)")
            continue
        if name in TOOLS:
            print(f"skills: '{name}' from {fname} OVERRIDES a built-in")
        TOOLS[name] = {"handler": skill["handler"],
                       "desc": str(skill["desc"])}
        print(f"skills: loaded '{name}' from {fname}")


load_skills()
# Compiled AFTER skills load, so drop-in tags are recognized.
TOOL_RE = re.compile(
    rf"<({'|'.join(map(re.escape, TOOLS))})>(.*?)</\1>", re.DOTALL)
REMEMBER_RE = re.compile(r"<remember>(.*?)</remember>", re.DOTALL)


def build_protocol() -> str:
    tool_lines = "\n".join(f"- {name}: {t['desc']}"
                           for name, t in TOOLS.items())
    vision_note = (
        "You cannot see images directly. When the user sends a "
        "photo, a separate vision model's description is injected "
        "as an [IMAGE] block — answer from that description and be "
        "honest that you are working from a description."
        if VISION_MODEL else
        "You cannot see or receive images in this configuration.")
    return f"""\
## History protocol
You keep a persistent history of durable facts, shown under "Your history". To save a
durable fact, emit: <remember>one-line fact</remember>
Only durable facts, phrased to make sense out of context. Always
include a normal reply to the user in the same message — never send
the tag alone. Confirm naturally that you'll remember it, without
mentioning the tag mechanism.

## Tools
{tool_lines}

Tool rules: emit ONE tool tag, then STOP — write nothing after it.
The result will be provided; then answer the user (or use another
tool if genuinely needed). Never mention the tag mechanism.

## Self-knowledge
You are a Barrel — a running instance of the BarrelShell platform, a
self-hosted agent running the local model {MODEL} via Ollama with a
{NUM_CTX}-token context window. Do not guess at other
technical details about yourself — for live runtime facts (uptime,
token usage, context fill, loaded model), use the status tool.
{vision_note}

## Pulse protocol
Some turns are scheduled tasks, marked [PULSE]. These are your own
routines firing, not the user. Do the task, then send what's worth
sending. If nothing is worth sending, reply exactly PULSE_OK.

## Style
You are chatting over Telegram on a phone. Keep replies short.
Plain text only — no markdown headers or tables.
"""


def build_system_prompt() -> str:
    identity = read_file(IDENTITY_FILE, "You are a helpful assistant.")
    hist = read_file(HISTORY_FILE, "(no history yet)")
    now = datetime.now()
    return (f"{identity}\n\n{build_protocol()}\n"
            f"## Your history\n{hist}\n\n"
            f"Current date/time: {now.strftime('%A %Y-%m-%d %H:%M')}")

# ============================================================ agent core

def save_history(reply: str) -> tuple[str, int]:
    """Persist <remember> facts; return (cleaned reply, facts saved)."""
    facts = [m.strip() for m in REMEMBER_RE.findall(reply) if m.strip()]
    if facts:
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            for fact in facts:
                f.write(f"- [{date.today().isoformat()}] {fact}\n")
        log_event("history_saved", facts=facts)
    return REMEMBER_RE.sub("", reply).strip(), len(facts)


conversations: dict[int, list[dict]] = {}
agent_lock = threading.Lock()

# Lightweight runtime counters for the dashboard.
stats: dict = {"turns": 0, "facts_saved": 0, "errors": 0, "tools": {},
               "last_user": None, "last_user_ts": None,
               "last_kind": None, "last_reply": None,
               "session_tokens_in": 0, "session_tokens_out": 0,
               "last_turn_tokens": None, "last_prompt_tokens": None,
               "last_tps": None}

EMPTY_FALLBACKS = [
    "I blanked on that one — mind rephrasing?",
    "My reply came back empty. Ask me that again?",
    "Well, that's embarrassing — I generated nothing. Try me again.",
    "The model returned silence. Rephrase and I'll take another run.",
]

def handle_turn(chat_id: int, user_text: str, kind: str = "chat",
                on_status=lambda s: None) -> str:
    with agent_lock:
        log_event("user_msg", chat_id=chat_id, kind=kind, text=user_text)
        stats["turns"] += 1
        stats["last_user"] = user_text[:300]
        stats["last_user_ts"] = datetime.now().isoformat(timespec="seconds")
        stats["last_kind"] = kind
        convo = conversations.setdefault(chat_id, [])
        convo.append({"role": "user", "content": user_text})

        seen_calls: set[tuple] = set()
        total_saved = 0
        turn_in = turn_out = 0
        retried_empty = False
        final_reply = "(no reply)"
        for _round in range(MAX_TOOL_ROUNDS + 2):
            messages = ([{"role": "system",
                          "content": build_system_prompt()}]
                        + convo[-MAX_TURNS:])
            response = ollama.chat(model=MODEL, messages=messages,
                                   options={"num_ctx": NUM_CTX})
            raw = response["message"]["content"]
            t_in = _count(response, "prompt_eval_count")
            t_out = _count(response, "eval_count")
            turn_in += t_in
            turn_out += t_out
            track_tokens(t_in, t_out)
            stats["last_prompt_tokens"] = t_in
            eval_ns = _count(response, "eval_duration")
            if eval_ns:
                stats["last_tps"] = round(t_out / (eval_ns / 1e9), 1)
            log_event("model_reply", chat_id=chat_id, round=_round,
                      raw=raw, tokens_in=t_in, tokens_out=t_out)
            reply, n_saved = save_history(raw)
            total_saved += n_saved
            stats["facts_saved"] += n_saved

            match = TOOL_RE.search(reply)
            if match is None or _round >= MAX_TOOL_ROUNDS:
                stripped = TOOL_RE.sub("", reply).strip()
                if not stripped and total_saved:
                    # Model sent only the <remember> tag — confirm
                    # the save deterministically instead of going
                    # silent or leaking a placeholder.
                    stripped = ("Got it — I've saved that and will "
                                "remember it in future conversations.")
                if not stripped and not retried_empty:
                    # One silent retry: nudge the model instead of
                    # shrugging at the user.
                    retried_empty = True
                    log_event("empty_reply_retry", chat_id=chat_id,
                              round=_round)
                    convo.append({"role": "user", "content":
                                  "(Your previous reply was empty. "
                                  "Reply to my last message in plain "
                                  "text now, without any tags.)"})
                    continue
                final_reply = stripped or random.choice(EMPTY_FALLBACKS)
                stats["last_reply"] = final_reply[:300]
                stats["last_turn_tokens"] = {"in": turn_in,
                                             "out": turn_out}
                convo.append({"role": "assistant",
                                "content": final_reply})
                log_event("final_reply", chat_id=chat_id,
                          text=final_reply)
                break

            tool, arg = match.group(1), match.group(2).strip()
            convo.append({"role": "assistant", "content": reply})
            call_key = (tool, arg.lower())
            if call_key in seen_calls:
                convo.append({"role": "user", "content":
                                "(You already did that. Answer with "
                                "what you have.)"})
                continue
            seen_calls.add(call_key)

            stats["tools"][tool] = stats["tools"].get(tool, 0) + 1
            on_status(f"{tool}: {arg}")
            result = TOOLS[tool]["handler"](arg, chat_id)
            log_event("tool_call", tool=tool, arg=arg,
                      result_chars=len(result))
            convo.append({"role": "user", "content":
                            f"[{tool.upper()} RESULT for \"{arg}\"]\n"
                            f"{result}\n[END RESULT] Use this to answer "
                            f"my previous message. Use another tool only "
                            f"if genuinely needed."})
        return final_reply

# =============================================================== pulse

PULSE_TASK_RE = re.compile(
    r"^#{2,}[ \t]*(?P<name>\S[^\n]*?)[ \t]*\n"      # ## task name
    r"\s*schedule:[ \t]*(?P<cron>[^\n]+?)[ \t]*(?:\n|\Z)"
    r"(?P<prompt>.*?)(?=^#{2,}[ \t]*\S|\Z)",
    re.MULTILINE | re.DOTALL | re.IGNORECASE)

# Any heading at all — used to catch entries that LOOK like tasks but
# don't parse, so a typo never fails silently again.
PULSE_HEADING_RE = re.compile(r"^#{2,}[ \t]*(\S[^\n]*)$", re.MULTILINE)

pulse_problems: list[str] = []   # surfaced in /pulse and the dashboard
_pulse_warned: set = set()


def _pulse_warn(msg: str) -> None:
    """Print once per distinct problem — the pulse loop re-parses every
    30s and would otherwise flood the log."""
    if msg not in _pulse_warned:
        _pulse_warned.add(msg)
        print(f"pulse: {msg}")
        log_event("pulse_problem", detail=msg)


def parse_pulse() -> list[dict]:
    text = read_file(PULSE_FILE)
    tasks, seen = [], set()
    for m in PULSE_TASK_RE.finditer(text):
        name = m.group("name").strip()
        cron = m.group("cron").strip()
        seen.add(name)
        if cron != "@reboot" and not croniter.is_valid(cron):
            _pulse_warn(f"task '{name}': invalid schedule '{cron}'")
            continue
        tasks.append({"name": name, "cron": cron,
                      "prompt": m.group("prompt").strip()})
    # Loud about headings that never became tasks — a missing or
    # misspelled 'schedule:' line used to vanish without a trace.
    for heading in PULSE_HEADING_RE.findall(text):
        if heading.strip() not in seen:
            _pulse_warn(f"'{heading.strip()}' has no valid 'schedule:' "
                        f"line on the following line — ignored")
    live = {t["name"] for t in tasks}
    pulse_problems[:] = [p for p in sorted(_pulse_warned)
                         if not any(f"'{n}'" in p and "invalid" not in p
                                    for n in live)]
    return tasks


def fire_due_reminders() -> None:
    reminders = load_json(REMINDERS_FILE, [])
    now = datetime.now()
    keep = []
    for r in reminders:
        if datetime.fromisoformat(r["due"]) <= now:
            send_message(r["chat_id"], f"⏰ Reminder: {r['message']}")
            log_event("reminder_fired", **r)
        else:
            keep.append(r)
    if len(keep) != len(reminders):
        save_json(REMINDERS_FILE, keep)


def fire_task(task: dict, state: dict, now: datetime) -> None:
    log_event("pulse_fired", task=task["name"])
    prompt = (f"[PULSE: {task['name']}] It is "
              f"{now.strftime('%A %Y-%m-%d %H:%M')}. "
              f"Task: {task['prompt']}")
    try:
        reply = handle_turn(PULSE_CHAT_ID, prompt, kind="pulse")
        if reply.strip() != "PULSE_OK":
            send_message(PULSE_CHAT_ID, reply)
    except Exception as e:
        print(f"pulse: '{task['name']}' failed: {e}")
    state[task["name"]] = now.isoformat()
    save_json(PULSE_STATE_FILE, state)


def pulse_loop() -> None:
    if not PULSE_CHAT_ID:
        print("pulse: no PULSE_CHAT_ID; pulse disabled")
        return
    print("pulse: scheduler started")
    state = load_json(PULSE_STATE_FILE, {})

    # @reboot tasks: fire once, now. The 10-minute guard matters —
    # without it, a crash-looping bot would announce "I'm back!"
    # every restart cycle, forever.
    startup = datetime.now()
    for task in parse_pulse():
        if task["cron"] != "@reboot":
            continue
        last_iso = state.get(task["name"])
        if last_iso and (startup - datetime.fromisoformat(last_iso)
                         < timedelta(minutes=10)):
            print(f"pulse: skipping @reboot '{task['name']}' "
                  f"(fired recently — crash-loop guard)")
            continue
        fire_task(task, state, startup)

    while True:
        now = datetime.now()
        try:
            fire_due_reminders()
        except Exception as e:
            print(f"pulse: reminder check failed: {e}")

        for task in parse_pulse():
            if task["cron"] == "@reboot":
                continue  # handled at startup, not on a schedule
            last_iso = state.get(task["name"])
            last = datetime.fromisoformat(last_iso) if last_iso else now
            due_at = croniter(task["cron"], last).get_next(datetime)
            if now < due_at:
                if last_iso is None:
                    state[task["name"]] = now.isoformat()
                    save_json(PULSE_STATE_FILE, state)
                continue
            fire_task(task, state, now)
        time.sleep(PULSE_CHECK_SECONDS)

# ============================================================ dashboard

def gather_status() -> dict:
    now = datetime.now()
    ps_state = load_json(PULSE_STATE_FILE, {})
    status = {
        "status": "online",
        "started": START_TIME.isoformat(timespec="seconds"),
        "uptime": str(now - START_TIME).split(".")[0],
        "model": MODEL,
        "vision_model": VISION_MODEL or "disabled",
        "num_ctx": NUM_CTX,
        "skills": sorted(TOOLS),
        "stats": {**stats, "tools": dict(stats["tools"])},
        "conversations_in_ram": {str(cid): len(c)
                                 for cid, c in conversations.items()},
        "pulse_tasks": [{"name": t["name"], "schedule": t["cron"],
                         "last_run": ps_state.get(t["name"])}
                        for t in parse_pulse()],
        "pending_reminders": load_json(REMINDERS_FILE, []),
        "pulse_problems": list(pulse_problems),
        "tokens": {
            "session_in": stats["session_tokens_in"],
            "session_out": stats["session_tokens_out"],
            "last_turn": stats["last_turn_tokens"],
            "last_speed_tps": stats["last_tps"],
            "last_prompt_tokens": stats["last_prompt_tokens"],
            "lifetime": token_usage.get("lifetime"),
            "today": token_usage.get("daily", {}).get(
                date.today().isoformat()),
        },
    }
    try:
        ps = requests.get(f"{OLLAMA_URL}/api/ps", timeout=5).json()
        status["ollama"] = "reachable"
        status["ollama_loaded"] = [
            {"name": m.get("name"),
             "vram_gb": round(m.get("size_vram", 0) / 1e9, 1),
             "total_gb": round(m.get("size", 0) / 1e9, 1),
             "keeps_until": str(m.get("expires_at", ""))[:19]}
            for m in ps.get("models", [])]
    except (requests.RequestException, ValueError) as e:
        status["ollama"] = f"unreachable ({e.__class__.__name__})"
        status["ollama_loaded"] = []
    return status

class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep service.log quiet
        pass

    def _send(self, body: bytes, ctype: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/status.json":
            self._send(json.dumps(gather_status(), indent=2).encode(),
                       "application/json")
        elif self.path == "/":
            # Served from disk every request — edit dashboard.html and
            # refresh the browser; no bot restart needed.
            page = read_file(DASHBOARD_HTML,
                             "<h1>dashboard.html not found next to "
                             "the script</h1>")
            self._send(page.encode(), "text/html; charset=utf-8")
        elif self.path.startswith("/workspace/"):
            # Same containment as the file skill: _workspace_path
            # refuses anything that escapes workspace/.
            name = unquote(self.path[len("/workspace/"):])
            wpath = _workspace_path(name)
            if not wpath or not os.path.isfile(wpath):
                self._send(b"not found", "text/plain", 404)
                return
            ctype = (mimetypes.guess_type(wpath)[0]
                     or "application/octet-stream")
            with open(wpath, "rb") as f:
                self._send(f.read(), ctype)
        else:
            self._send(b"not found", "text/plain", 404)

    def _chat_image(self):
        """Web-chat image upload: base64 JSON in, described by the
        vision model, injected as an [IMAGE] turn — the exact same
        path Telegram photos take."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > 14_000_000:   # ~10 MB binary after base64
                self._send(b'{"error": "image too large (10 MB max)"}',
                           "application/json", 400)
                return
            data = json.loads(self.rfile.read(length) or b"{}")
            img = base64.b64decode(str(data.get("image_b64", "")),
                                   validate=True)
            caption = str(data.get("caption", "")).strip()[:1000]
        except (ValueError, TypeError):
            self._send(b'{"error": "bad request"}',
                       "application/json", 400)
            return
        if not img:
            self._send(b'{"error": "no image data"}',
                       "application/json", 400)
            return
        if not VISION_MODEL:
            self._send(b'{"error": "no vision_model configured - '
                       b'set it in config.json"}',
                       "application/json", 400)
            return
        log_event("web_photo", size=len(img), caption=caption[:200])
        try:
            reply = image_turn(img, caption, WEB_CHAT_ID, "web")
        except Exception as e:
            log_event("web_photo_error", error=repr(e))
            self._send(json.dumps({"error": repr(e)}).encode(),
                       "application/json", 500)
            return
        self._send(json.dumps({"reply": reply}).encode(),
                   "application/json")

    def do_POST(self):
        if self.path == "/chat-image":
            self._chat_image()
            return
        if self.path != "/chat":
            self._send(b"not found", "text/plain", 404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
            message = str(data.get("message", "")).strip()
        except (ValueError, TypeError):
            self._send(b'{"error": "bad request"}',
                       "application/json", 400)
            return
        if not message or len(message) > 4000:
            self._send(b'{"error": "empty or too long"}',
                       "application/json", 400)
            return
        # Trust model: the server binds to localhost, so anyone who
        # can reach this endpoint already has your machine. No auth.
        log_event("web_msg", text=message[:300])
        try:
            cmd_reply = handle_command(message)
            reply = (cmd_reply if cmd_reply is not None
                     else handle_turn(WEB_CHAT_ID, message, kind="web"))
        except Exception as e:
            log_event("web_chat_error", error=repr(e))
            self._send(json.dumps({"error": repr(e)}).encode(),
                       "application/json", 500)
            return
        self._send(json.dumps({"reply": reply}).encode(),
                   "application/json")


def dashboard_loop() -> None:
    if not DASHBOARD_PORT:
        print("dashboard: disabled (dashboard_port is 0)")
        return
    try:
        server = ThreadingHTTPServer((DASHBOARD_HOST, DASHBOARD_PORT),
                                     DashboardHandler)
    except OSError as e:
        print(f"dashboard: disabled ({e})")
        return
    print(f"dashboard: http://{DASHBOARD_HOST}:{DASHBOARD_PORT}")
    server.serve_forever()

# ======================================================= telegram frontend

def tg(method: str, **params):
    r = requests.post(f"{API}/{method}", json=params, timeout=90)
    r.raise_for_status()
    return r.json()


def tg_upload(method: str, chat_id: int, field: str, path: str,
              caption: str = "") -> None:
    """Multipart upload for sendPhoto/sendAudio/sendDocument."""
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption[:1000]
    with open(path, "rb") as f:
        r = requests.post(f"{API}/{method}", data=data,
                          files={field: (os.path.basename(path), f)},
                          timeout=120)
    r.raise_for_status()


def typing(chat_id: int):
    """Returns an on_status callback that pokes Telegram's typing
    indicator; also callable directly with any string."""
    return lambda s: tg("sendChatAction", chat_id=chat_id,
                        action="typing")


def send_message(chat_id: int, text: str) -> None:
    for i in range(0, len(text), 4000):
        tg("sendMessage", chat_id=chat_id, text=text[i:i + 4000])


def describe_image(image_bytes: bytes) -> str:
    """One call to the vision model; its description — not the image —
    enters the conversation. Token usage is tracked like any call."""
    response = ollama.chat(model=VISION_MODEL, messages=[{
        "role": "user",
        "content": ("Describe this image in detail for someone who "
                    "cannot see it. Transcribe any visible text "
                    "exactly. Note objects, people, layout, and "
                    "anything unusual."),
        "images": [image_bytes]}])
    t_in = _count(response, "prompt_eval_count")
    t_out = _count(response, "eval_count")
    track_tokens(t_in, t_out)
    desc = response["message"]["content"].strip()
    log_event("vision_reply", model=VISION_MODEL, tokens_in=t_in,
              tokens_out=t_out, description=desc[:500])
    return desc


def image_turn(image_bytes: bytes, caption: str, chat_id: int,
               kind: str, on_status=lambda s: None) -> str:
    """The one image pipeline: describe, inject as an [IMAGE] block,
    run a normal turn. Telegram photos and web uploads both land
    here, so their behavior can never drift apart."""
    description = describe_image(image_bytes)
    user_text = ("[IMAGE] I sent you a photo"
                 + (f' with the caption: "{caption}"' if caption else "")
                 + f". A vision model described it as:\n{description}\n"
                 + "Respond to me based on this description"
                 + (" and my caption." if caption else "."))
    return handle_turn(chat_id, user_text, kind=kind,
                       on_status=on_status)


def handle_photo(msg: dict, chat_id: int) -> None:
    typing(chat_id)("")
    file_id = msg["photo"][-1]["file_id"]   # sizes ascend; last = largest
    info = tg("getFile", file_id=file_id)
    r = requests.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/"
                     f"{info['result']['file_path']}", timeout=60)
    r.raise_for_status()
    if len(r.content) > DOWNLOAD_MAX_BYTES:
        send_message(chat_id, "That image is too large for me.")
        return
    caption = (msg.get("caption") or "").strip()
    reply = image_turn(r.content, caption, chat_id, "chat",
                       on_status=typing(chat_id))
    send_message(chat_id, reply)


UNSUPPORTED = {
    "photo": "photos", "voice": "voice messages", "video": "videos",
    "video_note": "video notes", "sticker": "stickers",
    "document": "files", "audio": "audio", "location": "locations",
    "contact": "contacts",
}

COMMANDS = {
    "/history": lambda: read_file(HISTORY_FILE, "(no history yet)"),
    "/pulse": lambda: "\n".join(
        [f"{t['name']}: {t['cron']}" for t in parse_pulse()]
        + [f"\u26a0 {p}" for p in pulse_problems]) or "(no pulse tasks)",
    "/reminders": lambda: "\n".join(
        f"{r['due']}: {r['message']}"
        for r in load_json(REMINDERS_FILE, []))
        or "(no pending reminders)",
}


def approve_pending(name: str) -> str:
    """HUMAN-GATED WRITE: this is the only code path that adds to
    pulse.md, and it only runs from a user-typed /approve command."""
    name = name.strip().lower()
    pending = load_json(PENDING_PULSE_FILE, {})
    if name not in pending:
        return (f"No pending task named '{name}'. Pending: "
                f"{', '.join(pending) or 'none'}")
    task = pending.pop(name)
    with open(PULSE_FILE, "a", encoding="utf-8") as f:
        f.write(f"\n## {name}\nschedule: {task['cron']}\n\n"
                f"{task['prompt']}\n")
    save_json(PENDING_PULSE_FILE, pending)
    log_event("pulse_approved", name=name, cron=task["cron"])
    note = (" (@reboot tasks first fire at the next restart)"
            if task["cron"] == "@reboot" else "")
    return f"Approved — '{name}' ({task['cron']}) is now active{note}."


def reject_pending(name: str) -> str:
    name = name.strip().lower()
    pending = load_json(PENDING_PULSE_FILE, {})
    if name not in pending:
        return f"No pending task named '{name}'."
    pending.pop(name)
    save_json(PENDING_PULSE_FILE, pending)
    log_event("pulse_rejected", name=name)
    return f"Rejected and discarded '{name}'."


def remove_pulse_task(name: str) -> str:
    """Human-only removal of an active task, by name."""
    name = name.strip().lower()
    content = read_file(PULSE_FILE)
    pattern = re.compile(rf"^#{{2,}}[ \t]*{re.escape(name)}[ \t]*\n"
                         rf"\s*schedule:[^\n]*(?:\n|\Z)"
                         rf".*?(?=^#{{2,}}[ \t]*\S|\Z)",
                         re.MULTILINE | re.DOTALL | re.IGNORECASE)
    new_content, n = pattern.subn("", content)
    if not n:
        names = ", ".join(t["name"] for t in parse_pulse()) or "none"
        return f"No active task named '{name}'. Active: {names}"
    with open(PULSE_FILE, "w", encoding="utf-8") as f:
        f.write(new_content.strip() + "\n")
    state = load_json(PULSE_STATE_FILE, {})
    if state.pop(name, None) is not None:
        save_json(PULSE_STATE_FILE, state)
    log_event("pulse_removed", name=name)
    return f"Removed pulse task '{name}'."


PREFIX_COMMANDS = {
    "/approve": approve_pending,
    "/reject": reject_pending,
    "/pulse_remove": remove_pulse_task,
}


def handle_command(text: str):
    """Deterministic user commands, shared by Telegram and web chat.
    These run in CODE — the model can neither invoke nor fake them,
    which is exactly what makes /approve a human-in-the-loop gate.
    Returns reply text, or None if the message is not a command."""
    text = text.strip()
    if text in COMMANDS:
        return COMMANDS[text]()
    for prefix, fn in PREFIX_COMMANDS.items():
        if text == prefix:
            return fn("")
        if text.startswith(prefix + " "):
            return fn(text[len(prefix) + 1:])
    return None


def main() -> None:
    token_usage.update(load_json(TOKEN_USAGE_FILE, {}))
    print("BarrelShell — Barrel started, Telegram polling")
    threading.Thread(target=pulse_loop, daemon=True).start()
    threading.Thread(target=dashboard_loop, daemon=True).start()

    offset = 0
    while True:
        try:
            updates = tg("getUpdates", offset=offset, timeout=60)
        except requests.RequestException as e:
            print(f"poll error, retrying: {e}")
            time.sleep(5)
            continue

        for update in updates.get("result", []):
            offset = update["update_id"] + 1
            msg = update.get("message")
            if not msg:
                continue
            chat_id = msg["chat"]["id"]
            user_id = msg["from"]["id"]

            # Whitelist first — strangers get the rejection, not the
            # courtesy reply.
            if user_id not in ALLOWED_IDS:
                send_message(chat_id, "Sorry, this is a private bot.")
                log_event("rejected_user", user_id=user_id)
                continue

            if "text" not in msg:
                if "photo" in msg and VISION_MODEL:
                    try:
                        handle_photo(msg, chat_id)
                    except Exception as e:
                        stats["errors"] += 1
                        log_event("photo_error", chat_id=chat_id,
                                  error=repr(e))
                        send_message(chat_id, "⚠️ Couldn't process "
                                     "that image — details are in "
                                     "the log.")
                    continue
                label = next((lbl for key, lbl in UNSUPPORTED.items()
                              if key in msg), "that kind of message")
                send_message(chat_id, f"Text only for now — I can't "
                             f"handle {label} yet.")
                log_event("unsupported_message", chat_id=chat_id,
                          fields=[k for k in msg if k not in
                                  ("message_id", "from", "chat", "date")])
                continue

            # One bad message must not kill the poller: a crash here
            # happens before the offset is confirmed to Telegram, so
            # the same message gets redelivered on restart — an
            # infinite crash loop. Contain it, log it, move on.
            try:
                cmd_reply = handle_command(msg["text"])
                if cmd_reply is not None:
                    send_message(chat_id, cmd_reply)
                    continue

                typing(chat_id)("")
                reply = handle_turn(chat_id, msg["text"],
                                    on_status=typing(chat_id))
                send_message(chat_id, reply)
            except Exception as e:
                stats["errors"] += 1
                log_event("turn_error", chat_id=chat_id, error=repr(e))
                print(f"turn error: {e!r}")
                try:
                    send_message(chat_id, "⚠️ That one broke something "
                                 "on my end — details are in the log.")
                except requests.RequestException:
                    pass


if __name__ == "__main__":
    main()
