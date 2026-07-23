"""Bundled skill: one-time reminders.

Writes into core's reminders file; the pulse thread (in core) fires
them. Uses core's json helpers and log so persistence and logging match
the rest of the platform.
"""
from datetime import datetime

import barrel_v1 as core


def remind(arg: str, chat_id: int) -> str:
    if "|" not in arg:
        return ("(bad format — use: "
                "<remind>YYYY-MM-DD HH:MM | message</remind>)")
    when_s, message = (part.strip() for part in arg.split("|", 1))
    try:
        due = datetime.strptime(when_s, "%Y-%m-%d %H:%M")
    except ValueError:
        return f"(could not parse '{when_s}' — use YYYY-MM-DD HH:MM)"
    if due <= datetime.now():
        return (f"(that time is in the past — it is now "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M')})")
    if not message:
        return "(reminder needs a message after the |)"
    reminders = core.load_json(core.REMINDERS_FILE, [])
    reminders.append({"due": due.isoformat(timespec="minutes"),
                      "message": message, "chat_id": chat_id})
    core.save_json(core.REMINDERS_FILE, reminders)
    core.log_event("reminder_set", due=reminders[-1]["due"], message=message)
    return f"(reminder saved for {when_s}: {message})"


SKILL = {
    "name": "remind",
    "desc": "Set a one-time reminder. Convert the user's request to an "
            "absolute time using the current date/time you were given. "
            "Emit <remind>YYYY-MM-DD HH:MM | message</remind>",
    "handler": remind,
}
