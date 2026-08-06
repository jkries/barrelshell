"""Bundled skill: architect — drafts a NEW skill for a human to
review. It designs; it never installs.

This is a different category from every other bundled skill. Every
handler elsewhere was written by a human and only ever touches data.
A skill drafted here is CODE, and once it's in skills/ it runs with
the Barrel's full permissions, no sandbox — the same warning the
loader itself already carries.

So the design has one hard rule, enforced by absence rather than by a
gate: there is no verb, anywhere in this file, that writes to
SKILLS_DIR or BUNDLED_DIR. Not a gated one, not an approved one — the
capability to install simply does not exist in this program. The only
way a drafted skill goes live is a human moving the file themselves.

Two things this skill does to make that review real instead of
theoretical:
  1. save() prepends an unskippable warning banner to the file itself
     — even a human who doesn't open the .review.md sees it.
  2. save() runs a static scan (AST parsing + substring checks) for
     the clearest red flags — subprocess, eval, exec, os.system,
     ctypes, raw sockets. This is NOT a security review. It is
     mechanical and will miss real problems and occasionally flag
     nothing wrong. It never executes the candidate code to check it
     — that would defeat the entire point.
"""
import ast
import os
import re

import barrel_v1 as core

DRAFTS_DIR = "drafts"   # a subfolder of workspace/, kept separate from
                        # ordinary files the user saves there

_TEMPLATE = '''SKILL FILE TEMPLATE — read this before writing one.

Every skill is one Python file with a SKILL dict:

    SKILL = {
        "name": "lowercase_name",          # a-z, 0-9, _ only
        "desc": "What this does and WHEN to use it, plus the exact "
                "tag grammar the model should emit, e.g. "
                "'Emit <lowercase_name>argument</lowercase_name>'.",
        "handler": my_function,            # handler(arg: str, chat_id: int) -> str
    }

Your handler takes the text between the tags and returns a string —
that string becomes the tool result the model reads next.

TWO SHAPES, pick whichever fits:

1. SELF-CONTAINED (no core needed) — for anything that doesn't touch
   files, the network in a special way, or platform internals:

       def my_skill(arg: str, chat_id: int) -> str:
           return f"you asked for: {arg}"

       SKILL = {"name": "example", "desc": "...", "handler": my_skill}

2. USES THE CORE API — for anything touching the workspace or
   platform state:

       import barrel_v1 as core

       def my_skill(arg: str, chat_id: int) -> str:
           path = core._workspace_path(arg)      # refuses ../, absolute
           if not path:                          # paths, symlink escapes
               return "(refused: path escapes the workspace)"
           ...

       SKILL = {"name": "example", "desc": "...", "handler": my_skill}

NEVER, under any circumstances, in any skill:
  - subprocess, os.system, os.popen — no shelling out, ever
  - eval(...) or exec(...) on any input
  - ctypes — no calling native code directly
  - raw sockets — use requests if the network is needed
  - a file write that does not go through core._workspace_path
  - calling another skill's handler directly, e.g.
    core.TOOLS["images"]["handler"](...). A skill may only do the ONE
    job its own tag names. If a task needs several tools, that's
    several separate tags across ordinary turns — never one handler
    quietly running others. Reaching core._workspace_path,
    core.log_event, core.deliver, or reading core.TOOLS to check
    something (not call it) is fine; those are shared platform
    primitives, not other skills.

If a task genuinely needs one of those, it should not be a skill —
say so instead of writing around the rule.

WHY the last one matters even though it looks harmless: every tool
call is a visible tag someone can see in /trace, and the round limit
only counts steps that go through that same visible path. A skill
that calls other skills internally hides those steps from both —
the round budget stops meaning what it says, and there's no longer
anywhere for a human to step in mid-task. Multi-step work belongs in
the conversation, one tag at a time, not inside one function.

Keep it to ONE file, handle bad input without crashing (return an
error string, never raise), and cap anything you return to a few
thousand characters so one tool result can't blow the context.

Once you write this with <architect>save name.py | the code</architect>,
it lands in the workspace for a HUMAN to read — it is not active, and
there is no way for you to make it active. That is by design.'''

_DANGEROUS = [
    ("subprocess", "imports subprocess — this platform never shells "
                   "out; if a task genuinely needs a system command, "
                   "that's a sign it shouldn't be a skill"),
    ("os.system(", "calls os.system — same concern as subprocess"),
    ("os.popen(", "calls os.popen — same concern as subprocess"),
    ("eval(", "calls eval() — can execute arbitrary code from a string"),
    ("exec(", "calls exec() — can execute arbitrary code from a string"),
    ("__import__(", "imports dynamically — worth checking what and why"),
    ("ctypes", "imports ctypes — can call native code directly, far "
              "outside anything a skill should need"),
    ("socket.", "uses raw sockets — normal skills reach the network "
               "only via requests"),
]


_HANDLER_CALL_RE = re.compile(
    r'TOOLS\s*\[[^\]]+\]\s*\[\s*["\']handler["\']\s*\]\s*\(')


def _scan_for_flags(code: str) -> list:
    """Static text/AST checks ONLY — this never executes the candidate
    code. Advisory, not a gate: it can miss real problems and can flag
    nothing wrong. The human review is the actual check."""
    flags = []
    for needle, why in _DANGEROUS:
        if needle in code:
            flags.append(f"contains '{needle.rstrip('(')}' — {why}")
    if (("\"w\")" in code or "'w')" in code or "\"a\")" in code
         or "'a')" in code) and "barrel_v1" not in code):
        flags.append("appears to write files without importing "
                     "barrel_v1 — verify any file access stays inside "
                     "workspace/ via core._workspace_path")
    if _HANDLER_CALL_RE.search(code):
        flags.append('appears to call TOOLS[...]["handler"](...) — '
                     "calling another skill's handler directly, not "
                     "through its own tag, hides that step from "
                     "/trace and the round limit; each tool call "
                     "should be its own visible tag instead")
    try:
        tree = ast.parse(code)
        has_skill = any(
            isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "SKILL"
                   for t in n.targets)
            for n in ast.walk(tree))
        if not has_skill:
            flags.append("no top-level SKILL = {...} dict found — the "
                         "loader will skip this file entirely as-is")
    except SyntaxError as e:
        flags.append(f"does not parse as valid Python (line "
                     f"{e.lineno}): {e.msg} — this won't load until "
                     f"fixed")
    return flags


_BANNER = '''"""
################################################################
#  DRAFT SKILL — NOT REVIEWED. DO NOT MOVE TO skills/ UNTIL A  #
#  HUMAN HAS READ EVERY LINE BELOW. See the matching           #
#  .review.md file in this same folder.                        #
################################################################
"""

'''


def _review_doc(name: str, flags: list) -> str:
    lines = [f"# Review checklist — {name}", "",
             "This file was DRAFTED by the model, not written or "
             "reviewed by a human. It has no effect and cannot have "
             "any effect until YOU move it into skills/ yourself.",
             "", "## Automated checks"]
    if flags:
        lines += [f"- \u26a0 {f}" for f in flags]
    else:
        lines.append("- No obvious red flags found. This is a quick "
                     "mechanical scan, NOT a security review — read "
                     "the code yourself regardless.")
    lines += ["", "## Before you install this",
             "- [ ] Read every line — does it do only what was asked?",
             "- [ ] Any file access stays inside workspace/ via "
             "core._workspace_path?",
             "- [ ] No network calls you didn't expect?",
             "- [ ] The SKILL name, desc, and tag grammar look right?",
             "- [ ] You'd be fine with this running automatically, "
             "forever, on a schedule?",
             "", "When satisfied, move the .py file into skills/ "
             "(rename it if you like) and send /reload. This "
             ".review.md file is just documentation — it doesn't need "
             "to move with it."]
    return "\n".join(lines)


def _save(arg: str) -> str:
    name, _, code = arg.partition("|")
    name, code = name.strip(), code.strip()
    if not name or not code:
        return "(bad format — use: save name.py | the full code)"
    if not name.endswith(".py"):
        name += ".py"
    if "/" in name or "\\" in name or name.startswith("."):
        return "(refused: use a plain filename, no folders)"

    path = core._workspace_path(f"{DRAFTS_DIR}/{name}")
    if not path:
        return "(refused: that name escapes the workspace)"
    os.makedirs(os.path.dirname(path), exist_ok=True)

    flags = _scan_for_flags(code)
    m = (re.search(r'"name"\s*:\s*"([a-z0-9_]+)"', code)
        or re.search(r"'name'\s*:\s*'([a-z0-9_]+)'", code))
    if m and m.group(1) in core.TOOLS:
        flags.append(f"SKILL name '{m.group(1)}' matches an EXISTING "
                     f"tool — installing this would override it")

    body = code if code.endswith("\n") else code + "\n"
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(_BANNER + body)
        review_path = os.path.splitext(path)[0] + ".review.md"
        with open(review_path, "w", encoding="utf-8") as f:
            f.write(_review_doc(name, flags))
    except OSError as e:
        return f"(couldn't save: {e})"

    core.log_event("skill_drafted", name=name, flags=len(flags))
    tail = (f" {len(flags)} automated flag(s) noted in "
           f"{DRAFTS_DIR}/{os.path.splitext(name)[0]}.review.md."
           if flags else
           " No automated flags — still needs a human read before "
           "install.")
    return (f"(saved {DRAFTS_DIR}/{name} — this is NOT installed and "
           f"NOT active. A human has to read it and move it into "
           f"skills/ themselves; nothing, including this tool, can "
           f"install it automatically.{tail})")


def architect(arg: str, chat_id: int) -> str:
    verb, _, rest = arg.strip().partition(" ")
    verb, rest = verb.lower(), rest.strip()
    if verb == "template":
        return _TEMPLATE
    if verb == "save":
        return _save(rest)
    return ("(unknown architect command — use: template | "
           "save name.py | the full code)")


SKILL = {
    "name": "architect",
    "desc": "Draft a NEW skill file for a human to review — you cannot "
            "install anything yourself, only draft it for later "
            "review. Use <architect>template</architect> first to see "
            "the required format and hard rules (never subprocess, "
            "eval, exec, os.system, ctypes, or raw sockets). Then "
            "<architect>save name.py | the full code</architect> to "
            "write it into the workspace; it is never active and "
            "never will be without the user manually moving it into "
            "skills/ themselves.",
    "handler": architect,
}
