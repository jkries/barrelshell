"""Bundled skill: a long-term log of what's been discussed, condensed
from the platform's own turn log rather than kept separately.

Two ideas worth carrying into your own skills:

1. The durable source material already existed. Every user message is
   logged to agent_log.jsonl (core.LOG_FILE) as it happens — that file
   was built for debugging, but it's also a disk-backed, restart-proof
   transcript. This skill doesn't add new capture; it reads what's
   already there.

2. gather/save is a stage-then-commit pair, the same shape as pulse's
   propose/approve. gather() marks a window as PENDING but does not
   consider it archived; only a successful save() commits the marker
   forward. If the model gathers and then fails to save (round budget,
   error, whatever), nothing is lost — the next gather starts from the
   same place and simply includes that window again.

This is deliberately NOT the same thing as history.md. History is a
short, curated, always-injected list of facts about the user, checked
for truth on every turn. archives.md is a longer, browsed-on-demand
log of what topics came up over time — nobody re-verifies it, and it
is never stuffed into the system prompt wholesale.
"""
import json
import os

import barrel_v1 as core

ARCHIVE_FILE = "archives.md"
STATE_FILE = "archive_state.json"
MAX_GATHER_CHARS = 6000     # cap on one gather's worth of raw material
MAX_GATHER_ITEMS = 150
MAX_LIST_CHARS = 3000       # cap on recent/search output


def _load_state() -> dict:
    return core.load_json(STATE_FILE, {})


def _save_state(state: dict) -> None:
    core.save_json(STATE_FILE, state)


def _gather(arg: str) -> str:
    state = _load_state()
    since = state.get("last_archived_ts", "")   # "" sorts before everything

    items = []
    try:
        with open(core.LOG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue    # a malformed log line shouldn't stop the scan
                if (entry.get("event") != "user_msg"
                        or entry.get("kind") == "pulse"
                        or entry.get("ts", "") <= since):
                    continue    # kind=="pulse" excludes the archive task's
                                # OWN prompt — without this it would archive
                                # itself, every day, forever
                text = str(entry.get("text", "")).strip()
                if text:
                    items.append((entry["ts"], text))
    except OSError as e:
        return f"(couldn't read the activity log: {e.__class__.__name__})"

    if not items:
        return ("(nothing new since the last archive — don't call "
                "<archive>save</archive>, there is nothing to save. "
                "If this is a check-in, reply PULSE_OK.)")

    items.sort(key=lambda pair: pair[0])
    kept, chars = [], 0
    for ts, text in items[:MAX_GATHER_ITEMS]:
        line = f"[{ts}] {text}"
        if chars + len(line) > MAX_GATHER_CHARS:
            break
        kept.append((ts, line))
        chars += len(line)

    if not kept:   # first item alone exceeds the cap; take it anyway
        ts, text = items[0]
        kept = [(ts, f"[{ts}] {text[:MAX_GATHER_CHARS]}")]

    # PENDING, not committed — save() is what advances this for real.
    state["pending_ts"] = kept[-1][0]
    _save_state(state)

    truncated = len(kept) < len(items)
    body = "\n".join(line for _, line in kept)
    note = ("\n(more remain — they'll be included next time)"
           if truncated else "")
    return (f"{len(kept)} message(s) since the last archive:\n{body}{note}\n"
           f"\nWrite 1-3 short lines naming the TOPICS covered — not a "
           f"transcript, not direct quotes — then commit it with "
           f"<archive>save | your summary</archive>.")


def _save(summary: str) -> str:
    summary = summary.strip()
    if not summary:
        return "(nothing to save — use: save | your topic summary)"
    state = _load_state()
    pending = state.pop("pending_ts", None)
    if pending is None:
        return ("(no pending window to save — call <archive>gather</archive> "
                "first, then save what you found)")
    from datetime import date
    with open(ARCHIVE_FILE, "a", encoding="utf-8") as f:
        f.write(f"- [{date.today().isoformat()}] {summary}\n")
    state["last_archived_ts"] = pending
    _save_state(state)
    core.log_event("archive_saved", summary=summary[:300])
    n = sum(1 for ln in core.read_file(ARCHIVE_FILE, "").splitlines()
           if ln.startswith("- ["))
    return f"(saved — {n} entries now in {ARCHIVE_FILE})"


def _recent(n_str: str) -> str:
    try:
        n = max(1, min(int(n_str), 50)) if n_str.strip() else 10
    except ValueError:
        n = 10
    lines = [ln for ln in core.read_file(ARCHIVE_FILE, "").splitlines()
             if ln.startswith("- [")]
    if not lines:
        return "(the archive is empty so far)"
    shown = lines[-n:]
    out = "\n".join(shown)
    return out[-MAX_LIST_CHARS:]


def _search(query: str) -> str:
    query = query.strip().lower()
    if not query:
        return "(use: search | words to look for)"
    lines = [ln for ln in core.read_file(ARCHIVE_FILE, "").splitlines()
             if ln.startswith("- [") and query in ln.lower()]
    if not lines:
        return f"(no archive entries mention '{query}')"
    out = "\n".join(lines)
    return out[-MAX_LIST_CHARS:]


def archive(arg: str, chat_id: int) -> str:
    arg = arg.strip()
    if not arg:
        # No verb at all is genuinely ambiguous — gather? recent? —
        # so say so rather than silently picking one. A malformed or
        # argument-less tool call should land here as a visible,
        # actionable error, not quietly succeed at the wrong thing.
        return ("(archive needs a command — use: gather | save | topic "
               "summary | recent [N] | search | words)")
    verb, _, rest = arg.partition(" ")
    verb, rest = verb.lower(), rest.strip()

    if verb == "gather":
        return _gather(rest)
    if verb == "save":
        # Grammar is "save | summary" for visual consistency with the
        # file skill's two-part verbs, but save only takes one value —
        # strip the separator itself rather than treating it as content.
        return _save(rest.lstrip("|").strip())
    if verb == "recent":
        return _recent(rest)
    if verb == "search":
        return _search(rest.lstrip("|").strip())
    return ("(unknown archive command — use: gather | save | topic "
           "summary | recent [N] | search | words)")


SKILL = {
    "name": "archive",
    "desc": "A long-term log of TOPICS discussed over time (distinct "
            "from your short-term memory of individual facts, which "
            "uses <remember> instead). To add to it: "
            "<archive>gather</archive> returns recent conversation "
            "since the last archive entry, then write 1-3 lines "
            "naming the topics and commit with <archive>save | your "
            "summary</archive>. To look something up: "
            "<archive>recent</archive> or <archive>recent 20</archive> "
            "for the last N entries, or <archive>search | word</archive>.",
    "handler": archive,
}
