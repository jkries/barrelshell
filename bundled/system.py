"""Bundled skill: diagnose the installation environment.

Answers "is my setup actually working?" rather than "what is the
Barrel doing?" — that second question belongs to the core `status`
tool. Keep the split clean: status = runtime (model, uptime, tokens,
skills); system = the machine and installation underneath it.

DESIGN RULE, and the reason this skill is safe: it reports a curated
set of facts. It never runs arbitrary commands. There is no
subprocess import here and there should never be one — a skill that
can shell out dissolves every containment guarantee the platform
makes. Read-only introspection is trust tier one; shell access is
not a tier at all.
"""
import importlib.util
import os
import platform
import shutil
import sys

import barrel_v1 as core

# Packages the platform needs, with what breaks when each is missing.
REQUIRED = [
    ("ollama", "talking to the model at all"),
    ("requests", "web search, fetch, weather, downloads"),
    ("ddgs", "the search and images skills"),
    ("croniter", "scheduled pulse tasks"),
]


def _tick(ok: bool) -> str:
    return "OK  " if ok else "FAIL"


def _check_packages() -> list:
    lines = []
    for name, why in REQUIRED:
        found = importlib.util.find_spec(name) is not None
        lines.append(f"  [{_tick(found)}] {name}"
                     + ("" if found else f"  <- MISSING; needed for "
                                         f"{why}. Fix: pip install {name}"))
    # tzdata isn't imported directly — zoneinfo needs it on Windows,
    # so test the capability rather than the package.
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("Asia/Tokyo")
        lines.append("  [OK  ] timezone data")
    except Exception:
        lines.append("  [FAIL] timezone data  <- the clock skill can't "
                     "convert zones. Fix: pip install tzdata")
    return lines


def _check_ollama() -> list:
    """Is the model server up, and are the configured models pulled?"""
    try:
        import requests
    except ImportError:
        return ["  [FAIL] can't check — requests is not installed"]
    url = core.OLLAMA_URL.rstrip("/")
    try:
        r = requests.get(f"{url}/api/tags", timeout=10)
        r.raise_for_status()
        installed = [m.get("name", "") for m in r.json().get("models", [])]
    except Exception as e:
        return [f"  [FAIL] no answer from Ollama at {url} "
                f"({e.__class__.__name__})",
                "         Fix: start Ollama (`ollama serve`), or set "
                "OLLAMA_HOST if it runs on another machine."]

    lines = [f"  [OK  ] Ollama responding at {url}",
             f"         {len(installed)} model(s) installed"]
    for label, want in (("model", core.MODEL),
                        ("vision_model", core.VISION_MODEL)):
        if not want:
            continue
        # Ollama reports "name:tag". Match the exact tag first, then
        # fall back to the family so a wrong-tag case is reported as a
        # warning rather than a bare "not installed".
        exact = want in installed
        family = any(m.split(":")[0] == want.split(":")[0]
                     for m in installed)
        if exact:
            lines.append(f"  [OK  ] {label} '{want}' is pulled")
        elif family:
            lines.append(f"  [WARN] {label} '{want}' not found by that "
                         f"exact tag; a similar one is installed. "
                         f"Pin exact tags — '{want}' may resolve "
                         f"differently than you expect.")
        else:
            lines.append(f"  [FAIL] {label} '{want}' is NOT pulled. "
                         f"Fix: ollama pull {want}")
    return lines


def _check_files() -> list:
    lines = []
    here = os.path.dirname(os.path.abspath(core.__file__))
    for label, path, needed in (
            ("identity.md", "identity.md", True),
            ("pulse.md", core.PULSE_FILE, False),
            ("history.md", core.HISTORY_FILE, False),
            ("config.json", "config.json", False)):
        full = os.path.join(here, path)
        exists = os.path.isfile(full)
        if exists:
            lines.append(f"  [OK  ] {label}")
        elif needed:
            lines.append(f"  [WARN] {label} missing — running with a "
                         f"default persona. Fix: copy "
                         f"identity.example.md to identity.md")
        else:
            lines.append(f"  [ -- ] {label} not present (optional)")

    ws = os.path.join(here, core.WORKSPACE_DIR)
    try:
        os.makedirs(ws, exist_ok=True)
        probe = os.path.join(ws, ".write_probe")
        with open(probe, "w") as f:
            f.write("x")
        os.remove(probe)
        lines.append("  [OK  ] workspace is writable")
    except OSError as e:
        lines.append(f"  [FAIL] workspace not writable ({e.__class__.__name__})"
                     f" — the file skill can't save anything")

    for label, folder in (("bundled", core.BUNDLED_DIR),
                          ("skills", core.SKILLS_DIR)):
        full = os.path.join(here, folder)
        n = len([f for f in os.listdir(full)
                 if f.endswith(".py") and not f.startswith("_")]) \
            if os.path.isdir(full) else 0
        lines.append(f"  [{'OK  ' if os.path.isdir(full) else ' -- '}] "
                     f"{folder}/ — {n} skill file(s)")
    try:
        free = shutil.disk_usage(here).free / 1e9
        flag = "OK  " if free > 5 else "WARN"
        lines.append(f"  [{flag}] {free:.1f} GB free on this drive"
                     + ("" if free > 5 else " — models are large; this "
                                            "is tight"))
    except OSError:
        pass
    return lines


def system(arg: str, chat_id: int) -> str:
    bits = platform.machine()
    lines = ["Installation check", "",
             "Environment:",
             f"  Python {sys.version.split()[0]} on "
             f"{platform.system()} {platform.release()} ({bits})",
             f"  Running from {os.path.dirname(os.path.abspath(core.__file__))}",
             f"  Interfaces: "
             f"{'Telegram + dashboard' if core.TELEGRAM_ENABLED else 'dashboard only (local-only mode)'}",
             "", "Python packages:"]
    lines += _check_packages()
    lines += ["", "Model server:"]
    lines += _check_ollama()
    lines += ["", "Files and folders:"]
    lines += _check_files()

    failures = sum(1 for ln in lines if "[FAIL]" in ln)
    warnings = sum(1 for ln in lines if "[WARN]" in ln)
    lines += ["", f"Summary: {failures} problem(s), {warnings} warning(s)."]
    if failures:
        lines.append("Report the FAIL lines to the user with the "
                     "suggested fix — those are why something isn't "
                     "working.")
    else:
        lines.append("Nothing is broken; if something still isn't "
                     "working, it isn't the installation.")
    return "\n".join(lines)


SKILL = {
    "name": "system",
    "desc": "Check the INSTALLATION and machine you run on: Python "
            "version, operating system, whether required packages are "
            "installed, whether the Ollama server is reachable and the "
            "configured models are pulled, and whether your files and "
            "workspace are in order. Use this when something seems "
            "broken, when a skill reports it can't work, or when asked "
            "about the computer or setup. Do NOT use it for your own "
            "runtime state — uptime, token usage and your skill list "
            "come from the status tool instead. "
            "Emit <system>check</system>",
    "handler": system,
}
