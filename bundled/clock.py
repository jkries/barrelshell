"""Bundled skill: current time in other zones, and time conversion.
Self-contained (stdlib zoneinfo). On Windows, tzdata must be installed.
"""
import re
from datetime import datetime
from zoneinfo import ZoneInfo

_ALIASES = {
    "utc": "UTC", "gmt": "UTC", "eastern": "America/New_York",
    "newyork": "America/New_York", "nyc": "America/New_York",
    "central": "America/Chicago", "chicago": "America/Chicago",
    "mountain": "America/Denver", "denver": "America/Denver",
    "pacific": "America/Los_Angeles", "losangeles": "America/Los_Angeles",
    "la": "America/Los_Angeles", "anchorage": "America/Anchorage",
    "honolulu": "Pacific/Honolulu", "toronto": "America/Toronto",
    "mexicocity": "America/Mexico_City", "saopaulo": "America/Sao_Paulo",
    "london": "Europe/London", "paris": "Europe/Paris",
    "berlin": "Europe/Berlin", "madrid": "Europe/Madrid",
    "rome": "Europe/Rome", "amsterdam": "Europe/Amsterdam",
    "athens": "Europe/Athens", "istanbul": "Europe/Istanbul",
    "moscow": "Europe/Moscow", "cairo": "Africa/Cairo",
    "johannesburg": "Africa/Johannesburg", "dubai": "Asia/Dubai",
    "delhi": "Asia/Kolkata", "mumbai": "Asia/Kolkata",
    "kolkata": "Asia/Kolkata", "singapore": "Asia/Singapore",
    "hongkong": "Asia/Hong_Kong", "shanghai": "Asia/Shanghai",
    "beijing": "Asia/Shanghai", "seoul": "Asia/Seoul",
    "tokyo": "Asia/Tokyo", "perth": "Australia/Perth",
    "sydney": "Australia/Sydney", "auckland": "Pacific/Auckland",
}


def _resolve(name: str):
    raw = name.strip()
    for cand in (raw, _ALIASES.get(re.sub(r"[\s_.-]", "", raw.lower()), "")):
        if not cand:
            continue
        try:
            return ZoneInfo(cand)
        except Exception:
            continue
    return None


_CONVERT = re.compile(r"convert\s+(\d{1,2}):(\d{2})\s+(.+?)\s+to\s+(.+)", re.I)


def clock(arg: str, chat_id: int) -> str:
    arg = arg.strip()
    m = _CONVERT.fullmatch(arg)
    if m:
        hh, mm, src_name, dst_name = (int(m.group(1)), int(m.group(2)),
                                      m.group(3), m.group(4))
        src_tz, dst_tz = _resolve(src_name), _resolve(dst_name)
        if not src_tz or not dst_tz:
            bad = src_name if not src_tz else dst_name
            return (f"(unknown timezone '{bad.strip()}' — use IANA names "
                    f"like Asia/Tokyo; on Windows, tzdata must be installed)")
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
        tz = _resolve(name)
        if not tz:
            lines.append(f"{name.strip()}: (unknown timezone — use IANA "
                         f"names like Asia/Tokyo)")
            continue
        lines.append(f"{tz.key}: "
                     f"{datetime.now(tz).strftime('%H:%M (%A %Y-%m-%d)')}")
    return "\n".join(lines)


SKILL = {
    "name": "clock",
    "desc": "Current time in other timezones, or convert a time between "
            "zones. Emit <clock>tokyo</clock>, "
            "<clock>Asia/Tokyo, Europe/London</clock>, or "
            "<clock>convert 09:00 America/New_York to Asia/Tokyo</clock>",
    "handler": clock,
}
