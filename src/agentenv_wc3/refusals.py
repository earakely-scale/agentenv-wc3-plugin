"""Why an order the game took without a word never started. wc3env refuses only malformed orders; one the player
can't pay for or hasn't the buildings for, the game drops with the "Not enough gold." a player sees on screen and an
agent otherwise doesn't."""

from __future__ import annotations

from .frames import HALLS
from .prompts import SUPPLY
from .render import Reference

FOOD_LIMIT = 100
ORDINALS = ("first", "second", "third")


def unstarted(before: dict, after: dict, batch: list[dict], refused: set[int], ref: Reference,
              faction: str) -> dict[int, str]:
    """The batch's train, research and build orders that never started, by index, with what each lacked when it
    was sent: gold, lumber and food are spent down the batch in order. One is named only when it both lacked
    something and left no trace after the step (in its structure's queue, an upgrade under way, a new unit, a
    builder on its way), so an order the game took is never reported."""
    player = before.get("player") or {}
    gold, lumber, cap = player.get("gold") or 0, player.get("lumber") or 0, player.get("food_cap") or 0
    free = cap - (player.get("food_used") or 0)
    mine = {u["unit_id"]: u for u in before.get("units") or ()}
    have = {u["type_id"] for u in mine.values() if u.get("structure") and u.get("state") != "constructing"}
    heroes = sum(bool(u.get("hero")) for u in mine.values()) + sum(
        bool((ref.units.get(q) or {}).get("hero")) for u in mine.values() for q in u.get("queue") or ())
    out = {}
    for i, action in enumerate(batch):
        made = (action.get("arguments") or {}).get("type_id")
        if i in refused or action["command"] not in ("train", "research", "build") or not isinstance(made, str):
            continue
        if action["command"] == "research":
            level = ((ref.upgrades.get(made) or {}).get("levels") or [None])[0]
            if level is None:
                continue
            g, w, f, needs = level.get("gold") or 0, level.get("lumber") or 0, 0, level.get("requires") or []
        else:
            facts = ref.units.get(made)
            if facts is None:
                continue
            g, w, f, needs = facts.get("gold") or 0, facts.get("lumber") or 0, facts.get("food") or 0, list(
                facts.get("requires") or ())
            by_count = facts.get("requires_by_count") or []
            if facts.get("hero") and heroes < len(by_count):
                needs += by_count[heroes]
        lacks = []
        if g > gold:
            lacks.append(f"not enough gold (needs {g}, you had {gold})")
        if w > lumber:
            lacks.append(f"not enough lumber (needs {w}, you had {lumber})")
        if f > free:
            lacks.append(f"not enough food (needs {f}, {max(free, 0)} of {cap} free"
                         + (" at the 100 food limit)" if cap >= FOOD_LIMIT else
                            f": build another {SUPPLY.get(faction, 'supply structure')})"))
        if missing := [r for r in needs if not _built(r, have)]:
            nth = f"a {ORDINALS[heroes]} hero " if (ref.units.get(made) or {}).get("hero") and heroes < 3 else ""
            lacks.append(f"{nth}requires {', '.join(ref.name(r) for r in missing)}")
        if not lacks:
            gold, lumber, free = gold - g, lumber - w, free - f
            if (ref.units.get(made) or {}).get("hero"):
                heroes += 1
        elif not _started(action, made, mine, after):
            out[i] = "didn't start: " + "; ".join(lacks)
    return out


def _built(required: str, have: set[str]) -> bool:
    """A requirement a finished structure meets: that type, or for a town hall, any hall of its tier or above."""
    tier = next((i for i, halls in enumerate(HALLS) if required in halls), None)
    return required in have or tier is not None and any(have & halls for halls in HALLS[tier:])


def _started(action: dict, made: str, mine: dict[int, dict], after: dict) -> bool:
    now = {u["unit_id"]: u for u in after.get("units") or ()}
    if any(u["type_id"] == made for uid, u in now.items() if uid not in mine):
        return True
    unit = now.get(action["unit_id"])
    if unit is None:
        return False
    if action["command"] == "build":
        return (unit.get("order") or {}).get("name") == made
    return made in (unit.get("queue") or ()) or unit["type_id"] == made or unit.get("state") == "upgrading"
