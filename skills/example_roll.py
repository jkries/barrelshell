"""Example drop-in skill: a dice roller. Safe to delete.

The contract every skill file must meet:

    SKILL = {
        "name":    the tag, lowercase letters/digits/underscore only
                   -> the model will emit <roll>...</roll>
        "desc":    tells the model WHEN to use it + the exact grammar
        "handler": function(arg: str, chat_id: int) -> str
    }

Return errors as instructive "(...)" text so the model can correct
itself. Cap any output length. See barrelshell-skill-guide.md for design
and security guidance — and remember skills run with the bot's full
permissions, so read any skill you didn't write before installing.
"""
import random
import re


def roll(arg: str, chat_id: int) -> str:
    m = re.fullmatch(r"\s*(\d{1,2})d(\d{1,4})\s*", arg.lower())
    if not m:
        return "(bad format — use NdS, e.g. <roll>2d6</roll>)"
    n, sides = int(m.group(1)), int(m.group(2))
    if not (1 <= n <= 20 and 2 <= sides <= 1000):
        return "(supported: 1-20 dice, 2-1000 sides)"
    rolls = [random.randint(1, sides) for _ in range(n)]
    return f"rolled {n}d{sides}: {rolls} (total {sum(rolls)})"


SKILL = {
    "name": "roll",
    "desc": "Roll dice for games, random picks, or chance-based "
            "decisions. Emit <roll>2d6</roll> (NdS format).",
    "handler": roll,
}
