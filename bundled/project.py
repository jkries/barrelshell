"""Bundled skill: state-tracking for work that spans more than one
turn — a project too big for a single conversation to finish, picked
back up a chunk at a time by a pulse task.

This is genuinely general infrastructure, not a creative opinion like
gamedev's genre list — any multi-session build (a website, a longer
game, anything else that legitimately needs several sittings) can use
it, which is why it lives in bundled/ rather than skills/.

The mechanism is a note left for your own next cycle: each time work
happens on a project, the handler that did the work writes down what
it just did and what comes next, and the NEXT pulse cycle reads that
note to know where to resume — the same shape as a sticky note on an
unfinished desk, except the note-taker and the note-reader are the
same model, hours apart, with no memory of the gap between.

This skill does NOT do the building itself — no <file>, no <images>
calls live here. It only tracks state. The actual work happens via
ordinary tags in the SAME turn that calls these verbs, exactly the
rule the gamedev idea led to adding to the architect template.
"""
from datetime import datetime

import barrel_v1 as core

STATE_FILE = "project_state.json"


def _load_state() -> dict:
    return core.load_json(STATE_FILE, {})


def _save_state(state: dict) -> None:
    core.save_json(STATE_FILE, state)


def _start(arg: str) -> str:
    name, _, rest = arg.partition("|")
    name, description = name.strip(), rest.strip()
    if not name or not description:
        return ("(bad format — use: start short-name | what this "
                "project is and what the first step should be)")
    state = _load_state()
    active = state.get("active", {})
    if name in active:
        return (f"('{name}' already exists and is in progress — use "
                f"note to update it, or complete to finish it)")
    active[name] = {
        "started": datetime.now().isoformat(timespec="seconds"),
        "updated": datetime.now().isoformat(timespec="seconds"),
        "next_step": description,
        "cycles": 0,
    }
    state["active"] = active
    _save_state(state)
    core.log_event("project_started", name=name)
    return (f"(started '{name}' — it will be picked up and worked on "
           f"a bit at a time. Use <project>note {name} | ...</project> "
           f"after making progress, or "
           f"<project>complete {name}</project> when it's done.)")


def _note(arg: str) -> str:
    name, _, rest = arg.partition("|")
    name, next_step = name.strip(), rest.strip()
    state = _load_state()
    active = state.get("active", {})
    if name not in active:
        names = ", ".join(active) or "none"
        return f"(no active project called '{name}'. Active: {names})"
    if not next_step:
        return "(bad format — use: note name | what to do next)"
    active[name]["next_step"] = next_step
    active[name]["updated"] = datetime.now().isoformat(timespec="seconds")
    active[name]["cycles"] += 1
    state["active"] = active
    _save_state(state)
    return f"(noted — next cycle on '{name}' will pick up from that)"


def _complete(arg: str) -> str:
    name = arg.strip()
    state = _load_state()
    active = state.get("active", {})
    if name not in active:
        names = ", ".join(active) or "none"
        return f"(no active project called '{name}'. Active: {names})"
    entry = active.pop(name)
    done = state.get("completed", [])
    done.append({"name": name, "started": entry["started"],
                "completed": datetime.now().isoformat(timespec="seconds"),
                "cycles": entry["cycles"]})
    state["active"] = active
    state["completed"] = done[-50:]   # keep the tail, not unbounded
    _save_state(state)
    core.log_event("project_completed", name=name, cycles=entry["cycles"])
    return f"(marked '{name}' complete after {entry['cycles']} cycle(s))"


def _list(arg: str) -> str:
    state = _load_state()
    active = state.get("active", {})
    if not active:
        return "(no projects in progress)"
    lines = []
    for name, e in active.items():
        lines.append(f"- {name}: cycle {e['cycles']} — next: "
                     f"{e['next_step']}")
    return "\n".join(lines)


def project(arg: str, chat_id: int) -> str:
    verb, _, rest = arg.strip().partition(" ")
    verb, rest = verb.lower(), rest.strip()
    if verb == "start":
        return _start(rest)
    if verb == "note":
        return _note(rest)
    if verb == "complete":
        return _complete(rest)
    if verb == "list":
        return _list(rest)
    return ("(unknown project command — use: start short-name | "
           "description | note name | next step | complete name | "
           "list)")


SKILL = {
    "name": "project",
    "desc": "Tracks work that will take more than one sitting — a "
            "website, a longer game, anything too big for one "
            "conversation. This skill only remembers state; it does "
            "NOT build anything itself. <project>start short-name | "
            "description</project> begins one. Each time real "
            "progress happens (via ordinary <file>/<images> tags, "
            "same turn), leave a note for your future self with "
            "<project>note short-name | what to do next</project> — "
            "that's what the next work session reads to know where "
            "to resume. <project>complete short-name</project> when "
            "done. <project>list</project> shows what's active.",
    "handler": project,
}
