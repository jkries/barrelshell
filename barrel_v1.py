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
import hmac
import ipaddress
import json
import math
import mimetypes
import os
import re
import socket
import sys
import threading
import time
import random
from datetime import datetime, date, timedelta
from html.parser import HTMLParser
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

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
WEB_CHAT_FILE = "web_chat.json"

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
    "dashboard_token": "",
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
DASHBOARD_TOKEN = str(_cfg["dashboard_token"]).strip()

# Telegram is OPTIONAL. With no token or no allowed IDs, the Barrel
# runs local-only: the dashboard becomes the sole interface, and
# scheduled tasks and reminders are delivered to its web chat.
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_IDS = {int(x) for x in
               os.environ.get("TELEGRAM_ALLOWED_IDS", "").split(",")
               if x.strip().lstrip("-").isdigit()}
TELEGRAM_ENABLED = bool(BOT_TOKEN and ALLOWED_IDS)
WEB_CHAT_ID = 0   # web-chat conversation id (Telegram ids are nonzero)
PULSE_CHAT_ID = int(os.environ.get(
    "PULSE_CHAT_ID",
    next(iter(ALLOWED_IDS), WEB_CHAT_ID) if TELEGRAM_ENABLED
    else WEB_CHAT_ID))
API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Dashboard binds to localhost by default (config: dashboard_host) —
# it displays private conversation content and has no login. Binding
# anywhere else REQUIRES config "dashboard_token"; see dashboard_loop.
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

# ------------------------------------------------ shared: SSRF guard

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


# ---------------------------------- shared: workspace + file typing

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
            deliver(PULSE_CHAT_ID,
                    f"\U0001f552 Proposed pulse task '{name}' "
                    f"({cron}):\n{prompt}\n\nSend /approve "
                    f"{name} to enable or /reject {name} to "
                    f"discard.", "pulse")
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

TOOLS: dict = {
    # Core-native tools. Everything else is loaded from bundled/ and
    # skills/ at startup — see load_skills() at the bottom of the file.
    "forget": {
        "handler": run_forget,
        "desc": "Remove ONE outdated or wrong fact from your saved "
                "history — use when the user corrects or retracts "
                "something you have saved. Give a distinctive phrase "
                "from that entry; if several match you'll get a list "
                "to narrow. Emit <forget>distinctive phrase</forget>",
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
}

BUNDLED_DIR = "bundled"


def _load_skill_dir(folder: str) -> None:
    if not os.path.isdir(folder):
        return
    import importlib.util
    for fname in sorted(os.listdir(folder)):
        if not fname.endswith(".py") or fname.startswith("_"):
            continue
        path = os.path.join(folder, fname)
        try:
            spec = importlib.util.spec_from_file_location(
                f"barrel_skill_{folder}_{fname[:-3]}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception as e:
            print(f"skills: FAILED to load {folder}/{fname}: {e!r}")
            continue
        skill = getattr(mod, "SKILL", None)
        name = str((skill or {}).get("name", ""))
        if not (isinstance(skill, dict) and skill.get("desc")
                and callable(skill.get("handler"))
                and re.fullmatch(r"[a-z0-9_]+", name)):
            print(f"skills: {folder}/{fname} skipped — needs SKILL dict "
                  f"with lowercase name, desc, handler(arg, chat_id)")
            continue
        if name in TOOLS:
            print(f"skills: '{name}' from {folder}/{fname} OVERRIDES an "
                  f"existing tool")
        TOOLS[name] = {"handler": skill["handler"],
                       "desc": str(skill["desc"])}
        print(f"skills: loaded '{name}' from {folder}/{fname}")


def load_skills() -> None:
    """Merge drop-in skills into the registry. bundled/ ships with
    BarrelShell and updates freely; skills/ is yours and is never
    touched by updates. skills/ loads last, so a same-named user skill
    OVERRIDES a bundled one — that's how you swap, say, DuckDuckGo
    search for your own. SKILLS ARE CODE: they run with the Barrel's
    full permissions at load time; read anything you didn't write."""
    _load_skill_dir(BUNDLED_DIR)
    _load_skill_dir(SKILLS_DIR)


REMEMBER_RE = re.compile(r"<remember>(.*?)</remember>", re.DOTALL)


def build_protocol() -> str:
    tool_lines = "\n".join(f"- {name}: {t['desc']}"
                           for name, t in TOOLS.items())
    style_note = (
        "You are chatting over Telegram on a phone. Keep replies "
        "short. Plain text only — no markdown headers or tables."
        if TELEGRAM_ENABLED else
        "You are chatting in a local web panel in a desktop browser. "
        "Keep replies reasonably concise. Plain text — no markdown "
        "headers or tables; URLs you write are clickable.")
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
{style_note}
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
            deliver(r["chat_id"], f"⏰ Reminder: {r['message']}",
                    "reminder")
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
            deliver(PULSE_CHAT_ID, reply, "pulse")
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

    def _send(self, body: bytes, ctype: str, code: int = 200,
              extra: dict = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _cookie_token(self) -> str:
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "barrel_token":
                return v
        return ""

    def _authed(self, qs: dict) -> bool:
        """No token configured means loopback-only, which dashboard_loop
        already enforces — nothing to check. With a token, accept it
        from ?token= (then set a cookie) or from that cookie."""
        if not DASHBOARD_TOKEN:
            return True
        supplied = qs.get("token", [""])[0] or self._cookie_token()
        return hmac.compare_digest(supplied, DASHBOARD_TOKEN)

    def do_GET(self):
        parsed = urlparse(self.path)
        path, qs = parsed.path, parse_qs(parsed.query)
        if not self._authed(qs):
            self._send(b"Unauthorized. Open this page with "
                       b"?token=YOUR_TOKEN", "text/plain", 401)
            return
        extra = ({"Set-Cookie": f"barrel_token={DASHBOARD_TOKEN}; "
                                f"Path=/; SameSite=Strict; "
                                f"Max-Age=31536000"}
                 if DASHBOARD_TOKEN and qs.get("token") else None)
        if path == "/messages":
            try:
                since = int(qs.get("since", ["0"])[0])
            except ValueError:
                since = 0
            with _web_lock:
                msgs = [m for m in web_outbox if m["id"] > since]
                last = web_outbox[-1]["id"] if web_outbox else 0
            self._send(json.dumps({"messages": msgs,
                                   "last_id": last}).encode(),
                       "application/json", 200, extra)
            return
        if path == "/status.json":
            self._send(json.dumps(gather_status(), indent=2).encode(),
                       "application/json", 200, extra)
        elif path == "/":
            # Served from disk every request — edit dashboard.html and
            # refresh the browser; no bot restart needed.
            page = read_file(DASHBOARD_HTML,
                             "<h1>dashboard.html not found next to "
                             "the script</h1>")
            self._send(page.encode(), "text/html; charset=utf-8",
                       200, extra)
        elif path.startswith("/workspace/"):
            # Same containment as the file skill: _workspace_path
            # refuses anything that escapes workspace/.
            name = unquote(path[len("/workspace/"):])
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
        label = f"[image] {caption}" if caption else "[image]"
        try:
            reply = image_turn(img, caption, WEB_CHAT_ID, "web")
        except Exception as e:
            log_event("web_photo_error", error=repr(e))
            web_post("me", label)
            self._send(json.dumps({"error": repr(e)}).encode(),
                       "application/json", 500)
            return
        web_post("me", label)
        last_id = web_post("bot", reply)
        self._send(json.dumps({"reply": reply,
                               "last_id": last_id}).encode(),
                   "application/json")

    def do_POST(self):
        if not self._authed(parse_qs(urlparse(self.path).query)):
            self._send(b'{"error": "unauthorized"}',
                       "application/json", 401)
            return
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
            web_post("me", message)
            self._send(json.dumps({"error": repr(e)}).encode(),
                       "application/json", 500)
            return
        # Both halves are recorded only now, after the turn completes.
        # Recording the user's message up front let the dashboard's
        # poller see it mid-generation and draw it a second time.
        web_post("me", message)
        last_id = web_post("bot", reply)
        self._send(json.dumps({"reply": reply,
                               "last_id": last_id}).encode(),
                   "application/json")


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() in ("localhost", "")


def dashboard_available() -> bool:
    """The dashboard refuses to serve beyond loopback without a token:
    it has no login and exposes conversations and file access."""
    if not DASHBOARD_PORT:
        return False
    return _is_loopback(DASHBOARD_HOST) or bool(DASHBOARD_TOKEN)


def dashboard_loop() -> None:
    if not DASHBOARD_PORT:
        print("dashboard: disabled (dashboard_port is 0)")
        return
    if not dashboard_available():
        print(f"dashboard: REFUSING to bind {DASHBOARD_HOST} — the "
              f"dashboard has no login and exposes your conversations "
              f"and workspace. Set \"dashboard_token\" in config.json "
              f"to allow this, or keep dashboard_host at 127.0.0.1 "
              f"and SSH-tunnel instead.")
        return
    try:
        server = ThreadingHTTPServer((DASHBOARD_HOST, DASHBOARD_PORT),
                                     DashboardHandler)
    except OSError as e:
        print(f"dashboard: disabled ({e})")
        return
    if DASHBOARD_TOKEN:
        print(f"dashboard: http://{DASHBOARD_HOST}:{DASHBOARD_PORT}/"
              f"?token={DASHBOARD_TOKEN}  (token required)")
    else:
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


# ------------------------------------------------- web chat delivery

web_outbox: list = []          # [{"id", "ts", "role", "kind", "text"}]
_web_lock = threading.Lock()
WEB_OUTBOX_MAX = 200


def load_web_state() -> None:
    """In local-only mode the dashboard is the ONLY record of a
    conversation, so unlike Telegram chats it is persisted to disk."""
    data = load_json(WEB_CHAT_FILE, {})
    web_outbox[:] = data.get("outbox", [])[-WEB_OUTBOX_MAX:]
    convo = data.get("convo", [])
    if convo:
        conversations[WEB_CHAT_ID] = convo[-MAX_TURNS:]


def save_web_state() -> None:
    try:
        save_json(WEB_CHAT_FILE,
                  {"outbox": web_outbox[-WEB_OUTBOX_MAX:],
                   "convo": conversations.get(WEB_CHAT_ID,
                                              [])[-MAX_TURNS:]})
    except OSError as e:
        print(f"web state save failed: {e}")


def web_post(role: str, text: str, kind: str = "chat") -> int:
    """Append to the web transcript. The dashboard polls /messages,
    which is how proactive pulse and reminder output reaches a
    browser that can't receive a push."""
    with _web_lock:
        msg_id = (web_outbox[-1]["id"] + 1) if web_outbox else 1
        web_outbox.append(
            {"id": msg_id, "role": role, "kind": kind,
             "ts": datetime.now().isoformat(timespec="seconds"),
             "text": text})
        del web_outbox[:-WEB_OUTBOX_MAX]
    save_web_state()
    return msg_id


def deliver(chat_id: int, text: str, kind: str = "chat") -> None:
    """Send to whichever surface this conversation lives on. Pulse
    output, reminders, and proposals all route through here, so they
    behave identically with or without Telegram."""
    if chat_id == WEB_CHAT_ID or not TELEGRAM_ENABLED:
        web_post("bot", text, kind)
    else:
        send_message(chat_id, text)


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
    load_web_state()

    if not TELEGRAM_ENABLED and not dashboard_available():
        print("BarrelShell: no interface available — provide Telegram "
              "credentials (TELEGRAM_BOT_TOKEN + TELEGRAM_ALLOWED_IDS) "
              "or enable the dashboard (dashboard_port, and a "
              "dashboard_token if binding beyond localhost).")
        sys.exit(1)
    if BOT_TOKEN and not ALLOWED_IDS:
        print("BarrelShell: TELEGRAM_BOT_TOKEN set but no valid "
              "TELEGRAM_ALLOWED_IDS — Telegram stays off (an open bot "
              "would let anyone talk to your Barrel).")

    threading.Thread(target=pulse_loop, daemon=True).start()
    threading.Thread(target=dashboard_loop, daemon=True).start()

    if not TELEGRAM_ENABLED:
        print("BarrelShell — local-only mode: no Telegram credentials, "
              "so the dashboard is your Barrel's only interface. "
              "Scheduled tasks and reminders are delivered there.")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return

    print("BarrelShell — Barrel started, Telegram polling")
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


# Skills load LAST, after every core function (deliver, tg_upload,
# _workspace_path, ...) exists — bundled skills import this module, so
# the module must be fully defined first. TOOL_RE is compiled after,
# so freshly loaded tags are recognized.
load_skills()
TOOL_RE = re.compile(
    rf"<({'|'.join(map(re.escape, TOOLS))})>(.*?)</\1>", re.DOTALL)


if __name__ == "__main__":
    main()
