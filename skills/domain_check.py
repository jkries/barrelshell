"""Drop-in skill: domain availability checking.

Kept out of core on purpose: it depends on one personal server
(donowbot.com's rdap.php wrapper). If that endpoint moves or goes
away, edit CHECK_URL below or delete this file — the core bot
doesn't know this skill exists.

Endpoint contract (rdap.php):
    taken:     {"available": false, "domain": "...", "status": [...]}
    available: {"available": true,  "domain": "..."}
Raw-RDAP responses are also handled as a fallback.
"""
import json
import re
import time
from urllib.parse import quote

import requests

CHECK_URL = "https://www.donowbot.com/rdap.php?domain={domain}"
MAX_PER_CALL = 8
DOMAIN_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")


def _check(domain: str) -> str:
    url = CHECK_URL.format(domain=quote(domain))
    try:
        r = requests.get(url, timeout=15)
    except requests.RequestException as e:
        return f"{domain}: (check failed: {e.__class__.__name__})"
    body = r.text.strip()
    try:
        data = json.loads(body)
    except ValueError:
        if r.status_code == 404 or "not found" in body.lower()[:200]:
            return f"{domain}: AVAILABLE (no registration found)"
        return f"{domain}: {body[:200]}"
    # donowbot rdap.php format
    if "available" in data:
        if data["available"]:
            return f"{domain}: AVAILABLE"
        dropping = [s for s in data.get("status", [])
                    if "pending delete" in s or "redemption" in s]
        if dropping:
            return (f"{domain}: TAKEN (but status is "
                    f"{'; '.join(dropping)} — may be dropping soon)")
        return f"{domain}: TAKEN"
    # Fallback: standard RDAP shape
    if data.get("errorCode") == 404 or r.status_code == 404:
        return f"{domain}: AVAILABLE (no registration found)"
    events = {e.get("eventAction"): str(e.get("eventDate", ""))[:10]
              for e in data.get("events", [])}
    bits = [f"{domain}: TAKEN"]
    if events.get("registration"):
        bits.append(f"registered {events['registration']}")
    if events.get("expiration"):
        bits.append(f"expires {events['expiration']}")
    return ", ".join(bits)


def check_domains(arg: str, chat_id: int) -> str:
    names = [n.strip().lower().rstrip(".")
             for n in re.split(r"[,\s]+", arg) if n.strip()]
    if not names:
        return ("(no domains given — use: "
                "<domain>name.com, other.net</domain>)")
    results = []
    for name in names[:MAX_PER_CALL]:
        if not DOMAIN_RE.match(name):
            results.append(f"{name}: (not a valid domain name)")
            continue
        results.append(_check(name))
        time.sleep(0.3)   # be polite to the endpoint
    if len(names) > MAX_PER_CALL:
        results.append(f"(capped at {MAX_PER_CALL} — "
                       f"{len(names) - MAX_PER_CALL} not checked)")
    return "\n".join(results)


SKILL = {
    "name": "domain",
    "desc": "Check whether internet domain names are available to "
            "register — up to 8 per call, comma-separated. Use when "
            "brainstorming domains or asked about availability; "
            "never guess availability from memory. "
            "Emit <domain>name1.com, name2.net</domain>",
    "handler": check_domains,
}
