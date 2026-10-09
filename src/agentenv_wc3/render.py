"""Everything the agent reads: wc3env observations as compact text, with the game's own names from wc3env's
prepared reference data (wc3env.data.reference.json) next to every type id."""

from __future__ import annotations

import difflib
import json
import math
from collections import Counter
from functools import cache
from pathlib import Path

WORKERS = {"hpea", "opeo", "uaco", "ewsp"}
GOLD_MINE = "ngol"
COMMAND_ORDERS = {"harvest", "repair", "move", "attack", "stop", "smart"}   # orders with a command of their own
RESULTS = {"victory": "VICTORY", "defeat": "DEFEAT", "draw": "DRAW", "time_limit": "TIME LIMIT",
           "finished": "FINISHED (the drill's goal was met)"}
NEUTRAL_HOSTILE = {12}   # the creeps' player


class Reference:
    """Names, costs and tech of every unit, upgrade, item and ability, from wc3env's reference.json."""

    def __init__(self, data: dict):
        self.units = data.get("units", {})
        self.upgrades = data.get("upgrades", {})
        self.items = data.get("items", {})
        self.abilities = data.get("abilities", {})
        self.damage_multipliers = data.get("damage_multipliers", {})   # attack type -> armor class -> factor

    @classmethod
    def load(cls, path: Path | None = None) -> Reference:
        if path is None:
            try:
                from wc3env.data import REFERENCE as path
            except ImportError:
                return cls({})
        try:
            return cls(json.loads(Path(path).read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return cls({})

    def name(self, type_id: str) -> str:
        if not type_id:
            return "?"
        for table in (self.units, self.items):
            if type_id in table:
                return table[type_id].get("name") or type_id
        if type_id in self.upgrades:
            return (self.upgrades[type_id].get("names") or [type_id])[0]
        if type_id in self.abilities:
            levels = self.abilities[type_id].get("levels") or {}
            return next(iter(levels.values()), {}).get("name") or type_id
        return type_id

    def label(self, type_id: str) -> str:
        """`Peasant (hpea)`, or the bare id when it has no name."""
        name = self.name(type_id)
        return f"{name} ({type_id})" if name != type_id else type_id

    def _named(self) -> dict[str, list[tuple[str, str]]]:
        """Lower-case name -> [(table, type id)], for every named entry."""
        out: dict[str, list[tuple[str, str]]] = {}
        for table, entries in (("unit", self.units), ("upgrade", self.upgrades), ("item", self.items),
                               ("ability", self.abilities)):
            for type_id in entries:
                out.setdefault(self.name(type_id).lower(), []).append((table, type_id))
        return out

    def find(self, query: str,
             tables: tuple[str, ...] = ("unit", "upgrade", "item", "ability")) -> list[tuple[str, str]]:
        """Entries whose id or name is `query` (case-insensitive), else the closest names."""
        query = query.strip()
        hits = [(t, query) for t, entries in (("unit", self.units), ("upgrade", self.upgrades),
                                              ("item", self.items), ("ability", self.abilities))
                if t in tables and query in entries]
        named = self._named()
        hits += [h for h in named.get(query.lower(), []) if h[0] in tables and h not in hits]
        if hits:
            return hits
        close = difflib.get_close_matches(query.lower(), list(named), n=5, cutoff=0.75)
        return [h for name in close for h in named[name] if h[0] in tables]

    def named(self, value: str, tables: tuple[str, ...] = ("unit", "upgrade")) -> list[str]:
        """The type ids `value` is: itself if it is one, else every unit or upgrade it names exactly. One name can be
        several: Human and Orc Barracks, a hero and its campaign versions."""
        if any(value in t for t in (self.units, self.upgrades)):
            return [value]
        return list(dict.fromkeys(i for t, i in self._named().get(value.strip().lower(), []) if t in tables))

    def type_id(self, value: str, tables: tuple[str, ...] = ("unit", "upgrade")) -> str:
        """A type id for `value`: itself if it is one, else the id of the one unit or upgrade it names exactly."""
        named = self.named(value, tables)
        return named[0] if len(named) == 1 else value

    def cast_order(self, value: str, ability_ids: list[str]) -> str:
        """An order name for `value`: itself, or the cast order of the unit's ability that it names."""
        for ability_id in ability_ids:
            if self.name(ability_id).lower() == value.strip().lower() or ability_id == value:
                for level in (self.abilities.get(ability_id, {}).get("levels") or {}).values():
                    for order in level.get("orders") or ():
                        if order.get("name"):
                            return order["name"]
        return value


@cache
def map_info(name: str) -> dict:
    """wc3env's prepared facts about a stock map (start locations, gold mines, creep camps), or {}."""
    try:
        from wc3env.data import MAPS
    except ImportError:
        return {}
    stem = Path(name.replace("\\", "/")).stem
    path = MAPS / f"{stem}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def at(u: dict) -> str:
    return f"({u['x']:.0f},{u['y']:.0f})"


def distance(a: dict, b: dict) -> float:
    return math.hypot(a["x"] - b["x"], a["y"] - b["y"])


def footer(obs: dict, *, limit: float | None, queued: int, result: str = "") -> str:
    p = obs.get("player") or {}
    t = clock(obs.get("game_time_seconds", 0)) + (f"/{clock(limit)}" if limit else "")
    parts = [f"t {t}", f"gold {p.get('gold', 0)}", f"lumber {p.get('lumber', 0)}",
             f"food {p.get('food_used', 0)}/{p.get('food_cap', 0)}"]
    if queued:
        parts.append(f"{queued} queued")
    if result:
        parts.append(f"GAME OVER: {RESULTS.get(result, result.upper())}")
    return "[" + " · ".join(parts) + "]"


def order_text(order: dict | None, ref: Reference) -> str:
    if not order:
        return "idle"
    name = order.get("name") or "?"
    if name in ref.units:   # a build order is named by the structure's type
        name = f"build {ref.name(name)}"
    target = f" → {order['target_id']}" if order.get("target_id") else (
        f" → ({order['x']:.0f},{order['y']:.0f})" if order.get("x") is not None else "")
    return name + target


def hero_skills(hero: dict, ref: Reference) -> tuple[dict[str, int], int, list[str]]:
    """A hero's learned skills by level, its skill points left to spend (its level less the levels learned), and the
    skills it can learn now."""
    options = (ref.units.get(hero["type_id"]) or {}).get("potential_hero_abilities") or []
    learned = {a["ability_id"]: a.get("level", 0) for a in hero.get("abilities") or () if a["ability_id"] in options}
    left = max(0, (hero.get("level") or 0) - sum(learned.values()))

    def open_now(raw: str) -> bool:
        ability, have = ref.abilities.get(raw) or {}, learned.get(raw, 0)
        return (have < (ability.get("max_level") or 3) and (hero.get("level") or 0)
                >= (ability.get("required_level") or 1) + (ability.get("level_skip") or 2) * have)

    return learned, left, [raw for raw in options if left and open_now(raw)]


def skills_text(hero: dict, ref: Reference) -> str:
    """`skills Blizzard (AHbz) 1 · 2 skill points to spend: learn AHab or AHwe`, for get_state and list_units."""
    learned, left, ready = hero_skills(hero, ref)
    text = "skills " + (", ".join(f"{ref.label(k)} {v}" for k, v in learned.items()) if learned else "none")
    if left:
        text += (f" · {left} skill point{'s' if left > 1 else ''} to spend"
                 + (f": learn {' or '.join(ready)}" if ready else ""))
    return text


def skill_to_learn(hero: dict, ref: Reference) -> str | None:
    """The skill a hero spends its next point on: the deepest it can learn now, a tie to the first it lists; None
    with no point left (its level less the levels learned) or nothing open. A skill's next level needs hero level
    required_level + level_skip (the game's 2 when the data says 0) per level learned."""
    learned, _, ready = hero_skills(hero, ref)
    options = (ref.units.get(hero["type_id"]) or {}).get("potential_hero_abilities") or []
    return max(ready, key=lambda raw: (learned.get(raw, 0), -options.index(raw)), default=None)


def autocast_orders(u: dict, ref: Reference) -> list[str]:
    """The orders that switch on each of a unit's autocast abilities (heal, slow, curse, inner fire, ...)."""
    names = []
    for a in u.get("abilities") or ():
        level = ((ref.abilities.get(a["ability_id"]) or {}).get("levels") or {}).get("1") or {}
        names += [o["name"] for o in level.get("orders") or () if o.get("kind") == "enable_autocast"]
    return names


def inventories(obs: dict) -> dict[int, list[dict]]:
    """Each own unit's items by its unit id, in slot order."""
    held: dict[int, list[dict]] = {}
    for item in obs.get("inventory") or ():
        held.setdefault(item["unit_id"], []).append(item)
    return {unit: sorted(items, key=lambda i: i["slot"]) for unit, items in held.items()}


def items_text(items: list[dict], ref: Reference) -> str:
    """`items: slot 0 Potion of Healing (phea) x1, slot 1 ...`, the slots use_item and drop_item take."""
    return "items: " + ", ".join(f"slot {i['slot']} {ref.label(i['type_id'])}"
                                 + (f" x{i['charges']}" if i.get("charges") else "") for i in items)


def unit_line(u: dict, ref: Reference, *, own: bool, details: bool = False, items: list[dict] | None = None) -> str:
    parts = [f"{u['unit_id']} {ref.label(u['type_id'])} {at(u)} hp {u['hp']:.0f}/{u['max_hp']:.0f}"]
    if u.get("max_mana"):
        parts[0] += f" mana {u['mana']:.0f}/{u['max_mana']:.0f}"
    if u.get("hero"):
        parts[0] += f" level {u.get('level', 0)}"
    if not own:
        if u.get("owner") is not None:
            parts.append(f"player {u['owner']}")
    elif u.get("structure"):
        if u.get("state"):
            parts.append(f"{u['state']} ({u.get('state_seconds', 0):.0f} s in all)")
        queue = u.get("queue") or []
        if queue:
            parts.append(f"producing {ref.name(queue[0])} ({u.get('queue_seconds', 0):.0f} s)"
                         + (f", then {', '.join(ref.name(q) for q in queue[1:])}" if queue[1:] else ""))
        elif not u.get("state"):
            parts.append("idle")
    else:
        parts.append(order_text(u.get("order"), ref))
    if u.get("buffs"):
        parts.append("buffs " + ", ".join(ref.name(b) for b in u["buffs"]))
    if own and u.get("hero"):
        parts.append(skills_text(u, ref))
    if items:
        parts.append(items_text(items, ref))
    line = " · ".join(parts)
    if details and own:
        line += "".join("\n    " + s for s in unit_details(u, ref))
    return line


def unit_details(u: dict, ref: Reference) -> list[str]:
    """What a unit of yours can do: its castable abilities, and what it trains, builds and researches."""
    out = []
    info = ref.units.get(u["type_id"], {})
    for key, verb in (("trains", "trains"), ("builds", "builds"), ("researches", "researches"),
                      ("upgrades_to", "upgrades to")):
        if info.get(key):
            out.append(f"{verb}: " + ", ".join(cost_label(t, ref) for t in info[key]))
    if u.get("hero") and info.get("potential_hero_abilities"):
        out.append("hero skills (learn): " + ", ".join(ref.label(a) for a in info["potential_hero_abilities"]))
    casts = []
    for a in u.get("abilities") or ():
        levels = ref.abilities.get(a["ability_id"], {}).get("levels") or {}
        level = levels.get(str(a.get("level", 1))) or next(iter(levels.values()), {})
        orders = [o for o in level.get("orders") or () if o.get("kind") == "cast" and o.get("name")
                  and o["name"] not in COMMAND_ORDERS and not o["name"].endswith("build")]
        if not orders or level.get("passive"):
            continue
        o = orders[0]
        cd = f", cooldown {a['cooldown_remaining']:.0f} s left" if a.get("cooldown_remaining") else ""
        casts.append(f"{ref.name(a['ability_id'])} (cast order \"{o['name']}\", {target_form(o)}, "
                     f"{a.get('mana_cost', 0)} mana{cd})")
    if casts:
        out.append("casts: " + "; ".join(casts))
    return out


def target_form(order: dict) -> str:
    form = order.get("target_form")
    return "no target" if form in (None, "", "none") else f"{form} target"


def cost_label(type_id: str, ref: Reference) -> str:
    entry = ref.units.get(type_id) or ((ref.upgrades.get(type_id) or {}).get("levels") or [{}])[0]
    gold, lumber = entry.get("gold"), entry.get("lumber")
    cost = f" {gold}g/{lumber}l" if gold is not None else ""
    return f"{ref.name(type_id)} ({type_id}{cost})"


class Names:
    """The type of every unit the agent has seen, so events about it can name it after it is gone."""

    def __init__(self):
        self.types: dict[int, str] = {}

    def see(self, obs: dict) -> None:
        for u in [*(obs.get("units") or ()), *(obs.get("visible_enemies") or ()), *(obs.get("inside") or ())]:
            self.types[u["unit_id"]] = u["type_id"]

    def of(self, unit_id: int | None, ref: Reference) -> str:
        if not unit_id:
            return "an unseen unit"
        type_id = self.types.get(unit_id)
        return f"{unit_id} {ref.name(type_id)}" if type_id else str(unit_id)


def event_line(e: dict, ref: Reference, names: Names, me: int) -> str:
    kind, who = e.get("kind", "?"), names.of(e.get("unit_id"), ref)
    t = e.get("type_id")
    match kind:
        case "death":
            whose = "yours" if e.get("owner") == me else f"player {e.get('owner')}'s"
            return f"died: {e.get('unit_id')} {ref.name(t)} ({whose})"
        case "train_finish":
            return f"trained: {e.get('trained_id')} {ref.name(t)} at {who}"
        case "construct_finish" | "upgrade_finish":
            return f"{kind.split('_')[0]}ed: {e.get('unit_id')} {ref.name(t)}"
        case "research_finish":
            return f"researched: {ref.name(t)} at {who}"
        case "hero_level":
            return f"level up: {who} is level {e.get('level')}"
        case "attacked":
            return f"attacked: {who} by {names.of(e.get('attacker_id'), ref)}"
        case _:
            extra = f" {ref.name(t)}" if t else (f" {ref.name(e['ability_id'])}" if e.get("ability_id") else "")
            return f"{kind.replace('_', ' ')}: {who}{extra}"


def events_text(events: list[dict], ref: Reference, names: Names, me: int, limit: int = 30) -> list[str]:
    """Events, with every `attacked` folded into one line per victim (a fight fires hundreds)."""
    attacked = Counter(e.get("unit_id") for e in events if e.get("kind") == "attacked")
    lines, seen = [], set()
    for e in events:
        if e.get("kind") == "attacked":
            if e.get("unit_id") in seen:
                continue
            seen.add(e.get("unit_id"))
            n = attacked[e.get("unit_id")]
            lines.append(event_line(e, ref, names, me) + (f" (x{n})" if n > 1 else ""))
        else:
            lines.append(event_line(e, ref, names, me))
    return lines[-limit:] if len(lines) <= limit else [f"... {len(lines) - limit} earlier events", *lines[-limit:]]


def state(obs: dict, ref: Reference, *, me: int, race: str | None, map_name: str, limit: float | None,
          queued: int, result: str, events: list[str]) -> str:
    """get_state: the whole situation on one page."""
    units = obs.get("units") or []
    structures = [u for u in units if u.get("structure")]
    army = [u for u in units if not u.get("structure")]
    idle_workers = [u for u in army if u["type_id"] in WORKERS and not u.get("order")]
    p = obs.get("player") or {}
    lines = [f"WARCRAFT III · {Path(map_name.replace(chr(92), '/')).name} · you are player {me}"
             + (f", {race.replace('_', ' ').title()}" if race else ""),
             f"Game time {clock(obs.get('game_time_seconds', 0))}" + (f" of {clock(limit)}" if limit else ""),
             f"Gold {p.get('gold', 0)} · Lumber {p.get('lumber', 0)} · Food {p.get('food_used', 0)}/"
             f"{p.get('food_cap', 0)}"]
    if result:
        lines.append(f"GAME OVER: {RESULTS.get(result, result.upper())}")
    counts = Counter(ref.name(u["type_id"]) for u in army)
    lines.append(f"Units ({len(army)}): " + (", ".join(f"{n} {name}" for name, n in counts.most_common()) or "none"))
    held = inventories(obs)
    if heroes := [u for u in army if u.get("hero")]:
        lines.append("Heroes:")
        lines += [f"  {unit_line(u, ref, own=True, items=held.get(u['unit_id']))}" for u in heroes]
    if inside := obs.get("inside"):
        held = ", ".join(f"{u['unit_id']} {ref.name(u['type_id'])}" for u in inside[:8])
        lines.append(f"Inside mines, buildings or transports: {len(inside)} ({held}"
                     + (", ..." if len(inside) > 8 else "") + ")")
    if idle_workers:
        lines.append("IDLE WORKERS: " + ", ".join(str(u["unit_id"]) for u in idle_workers))
    lines.append(f"Structures ({len(structures)}):")
    lines += [f"  {unit_line(u, ref, own=True)}" for u in structures] or ["  none"]
    enemies = [u for u in obs.get("visible_enemies") or () if _relation(obs, u.get("owner")) == "enemy"]
    neutrals = [u for u in obs.get("visible_enemies") or () if _relation(obs, u.get("owner")) != "enemy"]
    if enemies:
        lines.append(f"ENEMIES IN VIEW ({len(enemies)}): "
                     + ", ".join(f"{n} {name}" for name, n in Counter(ref.name(u['type_id']) for u in enemies)
                                 .most_common()) + " (list_units who=enemy)")
    mines = [u for u in neutrals if u["type_id"] == GOLD_MINE]
    if mines and units:
        home = structures[0] if structures else units[0]
        nearest = sorted(mines, key=lambda m: distance(m, home))[:3]
        lines.append("Gold mines in view: " + ", ".join(f"{m['unit_id']} {at(m)}" for m in nearest))
    starts = map_info(map_name).get("start_locations") or []
    if starts:
        base = min(starts, key=lambda s: distance(s, structures[0])) if structures else None
        lines.append("Start locations (bases may be shuffled): " + ", ".join(
            f"({s['x']},{s['y']})" + (" your base" if s is base else "") for s in starts))
    bounds = (obs.get("map") or {}).get("bounds")
    if bounds:
        lines.append(f"Map: x {bounds['min_x']:.0f}..{bounds['max_x']:.0f}, y {bounds['min_y']:.0f}.."
                     f"{bounds['max_y']:.0f}")
    if obs.get("events_lost"):
        lines.append(f"({obs['events_lost']} events were lost: too many since your last look)")
    if events:
        lines.append("Since your last advance:")
        lines += [f"  {e}" for e in events]
    lines.append(footer(obs, limit=limit, queued=queued, result=result))
    return "\n".join(lines)


def _relation(obs: dict, owner: int | None) -> str:
    for p in obs.get("players") or ():
        if p.get("id") == owner:
            return p.get("relation") or "?"
    return "enemy" if owner not in (12, 13, 14, 15) else "neutral"


def units_list(obs: dict, ref: Reference, *, who: str, type_filter: str | None, near: dict | None,
               radius: float | None, details: bool, limit: int) -> str:
    if who == "own":
        pool = list(obs.get("units") or [])
    elif who == "inside":
        pool = list(obs.get("inside") or [])
    else:
        want = "enemy" if who == "enemy" else None
        pool = [u for u in obs.get("visible_enemies") or ()
                if (_relation(obs, u.get("owner")) == "enemy") == (want == "enemy")]
    if type_filter:
        t = type_filter.strip().lower()
        pool = [u for u in pool if u["type_id"].lower() == t or ref.name(u["type_id"]).lower() == t]
    if near is not None:
        pool = [u for u in pool if radius is None or distance(u, near) <= radius]
        pool.sort(key=lambda u: distance(u, near))
    if not pool:
        return f"No {who} units match."
    shown = pool[:limit]
    own, held = who in ("own", "inside"), inventories(obs)
    lines = [unit_line(u, ref, own=own, details=details, items=held.get(u["unit_id"]) if own else None)
             if "max_hp" in u else
             f"{u['unit_id']} {ref.label(u['type_id'])} {at(u)} · {order_text(u.get('order'), ref)}" for u in shown]
    if len(pool) > limit:
        lines.append(f"... and {len(pool) - limit} more (narrow with type, near or radius)")
    return "\n".join(lines)


def resources(obs: dict, ref: Reference, *, near: dict, radius: float, limit: int) -> str:
    mines = sorted((u for u in obs.get("visible_enemies") or () if u["type_id"] == GOLD_MINE),
                   key=lambda m: distance(m, near))
    trees = sorted((d for d in obs.get("destructables") or ()
                    if d.get("resource") == "lumber" and distance(d, near) <= radius),
                   key=lambda d: distance(d, near))
    items = sorted((i for i in obs.get("items") or () if distance(i, near) <= radius), key=lambda i: distance(i, near))
    lines = ["Gold mines in view: " + (", ".join(f"{m['unit_id']} {at(m)} {distance(m, near):.0f} away"
                                                 for m in mines[:limit]) or "none")]
    lines.append(f"Trees within {radius:.0f} ({len(trees)}): "
                 + (", ".join(f"{d['id']} {at(d)}" for d in trees[:limit]) or "none")
                 + (f", ... {len(trees) - limit} more" if len(trees) > limit else ""))
    if items:
        lines.append("Items on the ground: " + ", ".join(f"{i['item_id']} {ref.label(i['type_id'])} {at(i)}"
                                                         for i in items[:limit]))
    return "\n".join(lines)


def lookup(query: str, ref: Reference) -> str:
    hits = ref.find(query)
    if not hits:
        return f"Nothing named {query!r}. Try a unit, building, upgrade, item or ability name, e.g. \"Footman\"."
    return "\n\n".join(_entry(table, type_id, ref) for table, type_id in hits[:4])


def _entry(table: str, type_id: str, ref: Reference) -> str:
    if table == "unit":
        u = ref.units[type_id]
        lines = [f"{ref.label(type_id)}: {'structure' if u.get('structure') else 'hero' if u.get('hero') else 'unit'}"
                 + (f", {u['race']}" if u.get("race") else ""),
                 f"cost {u.get('gold', 0)} gold, {u.get('lumber', 0)} lumber"
                 + (f", {u['food']} food" if u.get("food") else "") + (f", gives {u['food_made']} food"
                                                                       if u.get("food_made") else "")
                 + (f", {u['build_seconds']:.0f} s to make" if u.get("build_seconds") else ""),
                 f"hp {u.get('hp')}, armor {u.get('armor')}" + (f", damage {u['damage'][0]:.0f}-{u['damage'][1]:.0f}"
                                                                 if u.get("damage") else "")
                 + (f", range {u['base_attack_range']:.0f}" if u.get("base_attack_range") else "")
                 + (f", speed {u['base_move_speed']:.0f}" if u.get("base_move_speed") else "")]
        if u.get("requires"):
            lines.append("requires: " + ", ".join(ref.label(r) for r in u["requires"]))
        for key in ("trains", "builds", "researches", "upgrades_to", "sells_items", "sells_units"):
            if u.get(key):
                lines.append(f"{key.replace('_', ' ')}: " + ", ".join(cost_label(t, ref) for t in u[key]))
        if u.get("potential_hero_abilities"):
            lines.append("hero skills: " + ", ".join(ref.label(a) for a in u["potential_hero_abilities"]))
        if u.get("description"):
            lines.append(u["description"])
        return "\n".join(lines)
    if table == "upgrade":
        up = ref.upgrades[type_id]
        lines = [f"{ref.label(type_id)}: upgrade (research it with research, type_id {type_id})"]
        for level in up.get("levels") or ():
            lines.append(f"level {level.get('level')}: {level.get('name')}, {level.get('gold')} gold, "
                         f"{level.get('lumber')} lumber, {level.get('seconds', 0):.0f} s"
                         + (f"; requires {', '.join(ref.label(r) for r in level['requires'])}"
                            if level.get("requires") else "")
                         + (f". {level['description']}" if level.get("description") else ""))
        return "\n".join(lines)
    if table == "item":
        it = ref.items[type_id]
        return (f"{ref.label(type_id)}: item, {it.get('gold', 0)} gold, {it.get('lumber', 0)} lumber"
                + (f". {it['description']}" if it.get("description") else ""))
    levels = ref.abilities[type_id].get("levels") or {}
    lines = [f"{ref.label(type_id)}: ability"]
    for n, level in list(levels.items())[:3]:
        orders = ", ".join(f"\"{o['name']}\" ({target_form(o)})" for o in level.get("orders") or () if o.get("name"))
        lines.append(f"level {n}: " + (f"cast order {orders}; " if orders else "")
                     + (f"range {level['range']:.0f}; " if level.get("range") else "")
                     + (level.get("description") or ""))
    return "\n".join(lines)
