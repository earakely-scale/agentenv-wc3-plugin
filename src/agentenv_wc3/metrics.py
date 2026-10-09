"""Each player's measures over a game, for grading (agentenv_rts.grade), under wc3agent's metric names so its scenario
checks carry over unchanged. They come from every observation the env takes and from the units a stage step names
(handles: `army` and `hero` are the player's staged army, `enemy` its staged opponents). The definitions follow
wc3agent's Scenario.metric, and a unit's strength is wc3agent's (game/strength.py), so its checks' thresholds keep
their meaning."""

from __future__ import annotations

import math

from . import render
from .frames import CREEPS, HALLS, army_value

CAMP_REACH, CAMP_CLEAR = 450.0, 900.0   # a unit this close to a camp, and no creep this close, has cleared it
ALL_HALLS = HALLS[0] | HALLS[1] | HALLS[2]


ARMOR_FACTOR = 0.06   # each point of armor adds 6% effective health
COMMON_ATTACKS = ("normal", "pierce", "magic", "siege")
HERO_SPELLS = (1.5, 0.15)   # wc3agent's guess at what a hero's spells add: x1.5 at level 1, 0.15 more a level


def hero_numbers(facts: dict, level: int) -> tuple[float, float, float]:
    """A hero's hit points, armor and bonus damage at `level`, from its attributes and their growth."""
    a, gained = facts["attributes"], max(level, 1) - 1
    points = {k: a[k] + a[k + "_per_level"] * gained for k in ("str", "agi", "int")}
    return (facts["hp"] + 25 * a["str_per_level"] * gained, facts["armor"] - 2 + 0.3 * points["agi"],
            a[a["primary"] + "_per_level"] * gained if a.get("primary") else 0.0)


def unit_strength(facts: dict, multipliers: dict, hp: float | None = None, level: int = 0) -> float:
    """wc3agent's fighting value of one unit: sqrt(effective hit points x damage per second), its effective health
    from armor and how its armor class takes the common attacks; a hero's from its attributes, times its spells."""
    damage, period = facts.get("damage") or [0, 0], facts.get("base_attack_period") or 0
    if not any(damage) or period <= 0 or facts.get("structure"):
        return 0.0
    health, armor, bonus = facts["hp"], facts.get("armor") or 0.0, 0.0
    if facts.get("hero") and facts.get("attributes"):
        health, armor, bonus = hero_numbers(facts, level)
    health = health if hp is None else hp
    taken = [multipliers[a].get(facts.get("base_armor_class"), 1.0) for a in COMMON_ATTACKS if a in multipliers]
    toughness = len(taken) / sum(taken) if taken and sum(taken) else 1.0
    value = math.sqrt(max(0.0, health * (1 + ARMOR_FACTOR * max(armor, 0)) * toughness
                          * (sum(damage) / 2 + bonus) / period))
    return value * (HERO_SPELLS[0] + HERO_SPELLS[1] * (max(level, 1) - 1)) if facts.get("hero") else value


def strength(units: list[dict], ref: render.Reference) -> float:
    """What units are worth in a fight as they stand: wounded ones less, heroes by their level."""
    return sum(unit_strength(ref.units[u["type_id"]], ref.damage_multipliers, u.get("hp"), u.get("level") or 0)
               for u in units if u["type_id"] in ref.units)


class Metrics:
    """Per player: samples of its state every observation, the events it saw, first sightings, camps cleared, and the
    handles' units, so `of(slot)` can say any metric at the end."""

    def __init__(self, ref: render.Reference, info: dict, slots: list[int]):
        self.ref = ref
        self.camps = [{"number": i + 1, **c} for i, c in enumerate(info.get("creep_camps") or ()) if "x" in c]
        self.starts = [s for s in info.get("start_locations") or () if "x" in s]
        self.samples = {s: [] for s in slots}
        self.events = {s: [] for s in slots}
        self.first_seen = {s: {} for s in slots}
        self.cleared = {s: set() for s in slots}
        self.cleared_at: dict[int, dict[int, float]] = {s: {} for s in slots}
        self.base_seen: dict[int, float | None] = {s: None for s in slots}
        self.live: dict[int, dict] = {}
        self.deaths: set[int] = set()
        self.handles: dict[str, dict] = {}   # name -> {"slot", "ids", "strength"}
        self.enemies: dict[int, set[int]] = {s: set() for s in slots}
        self.spots: dict[int, dict[int, tuple[float, float]]] = {s: {} for s in slots}

    def facts(self, type_id: str) -> dict:
        return self.ref.units.get(type_id) or {}

    def worker(self, u: dict) -> bool:
        return not u.get("structure") and bool(self.facts(u["type_id"]).get("builds"))

    def see(self, observations: dict[int, dict]) -> None:
        for slot, obs in observations.items():
            if slot not in self.samples:
                continue
            now, units = obs.get("game_time_seconds") or 0.0, obs.get("units") or []
            if not obs.get("result"):
                self.live[slot] = obs
            player = obs.get("player") or {}
            workers = [u for u in units if self.worker(u)]
            types: dict[str, int] = {}
            for u in units:
                if u.get("hp", 0) > 0:
                    types[u["type_id"]] = types.get(u["type_id"], 0) + 1
            self.samples[slot].append({
                "t": now, "types": types, "gold": player.get("gold") or 0,
                "food_left": (player.get("food_cap") or 0) - (player.get("food_used") or 0),
                "food_cap": player.get("food_cap") or 0,
                "idle_workers": sum(w.get("order") is None for w in workers), "workers_outside": len(workers),
                "workers": len(workers) + sum(1 for u in obs.get("inside") or ()
                                              if self.facts(u["type_id"]).get("builds")),
                "uprooted": sum(self._walking(slot, u) for u in units)})
            for u in units:
                if not (u.get("structure") and u.get("state") == "constructing"):
                    self.first_seen[slot].setdefault(u["type_id"], now)
            self.events[slot] += [{**e, "t": now} for e in obs.get("events") or ()]
            self.deaths |= {e.get("unit_id") for e in obs.get("events") or () if e.get("kind") == "death"}
            enemies = {p["id"] for p in obs.get("players") or () if p.get("kind") == "player"
                       and p.get("relation") == "enemy"}
            self.enemies[slot] = enemies   # as they stand: allies show as enemies until the game's first step
            if self.base_seen[slot] is None and any(u.get("structure") and u.get("owner") in enemies
                                                    for u in obs.get("visible_enemies") or ()):
                self.base_seen[slot] = now
            hostile = [u for u in obs.get("visible_enemies") or () if u.get("owner") == CREEPS]
            mobile = [u for u in units if not u.get("structure")]
            cleared = {c["number"] for c in self.camps
                       if any(math.dist((u["x"], u["y"]), (c["x"], c["y"])) < CAMP_REACH for u in mobile)
                       and not any(math.dist((u["x"], u["y"]), (c["x"], c["y"])) < CAMP_CLEAR for u in hostile)}
            self.cleared[slot] |= cleared
            for number in cleared:
                self.cleared_at[slot].setdefault(number, now)
        for handle in self.handles.values():
            if handle["strength"] is None and handle["slot"] in observations:
                mine = [u for u in observations[handle["slot"]].get("units") or () if u["unit_id"] in handle["ids"]]
                handle["strength"] = strength(mine, self.ref) or None

    def stage(self, name: str, slot: int, ids: list[int]) -> None:
        handle = self.handles.setdefault(name, {"slot": slot, "ids": [], "strength": None})
        handle["ids"] += [i for i in ids if i not in handle["ids"]]

    def standing(self, name: str) -> float | None:
        """The strength of a handle's units still alive, from its player's latest observation; None without it."""
        handle = self.handles.get(name)
        if handle is None:
            return None
        units = (self.live.get(handle["slot"]) or {}).get("units") or ()
        return strength([u for u in units if u["unit_id"] in handle["ids"] and u.get("hp", 0) > 0], self.ref)

    def home(self, obs: dict) -> dict | None:
        hall = next((u for u in obs.get("units") or () if u.get("structure")), None)
        if hall is None or not self.starts:
            return None
        return min(self.starts, key=lambda s: math.dist((s["x"], s["y"]), (hall["x"], hall["y"])))

    def _walking(self, slot: int, u: dict) -> bool:
        """A building on the move: an uprooted Night Elf ancient. The game keeps such a unit a structure, so it shows
        by walking: it stands somewhere else than at the last observation."""
        facts = self.facts(u["type_id"])
        if not facts.get("structure") or not facts.get("base_move_speed"):
            return False
        last, self.spots[slot][u["unit_id"]] = self.spots[slot].get(u["unit_id"]), (u["x"], u["y"])
        return not u.get("structure") or last is not None and math.dist(last, (u["x"], u["y"])) > 1

    def _dt(self, slot: int, test) -> float:
        s = self.samples[slot]
        return round(sum(b["t"] - a["t"] for a, b in zip(s, s[1:], strict=False) if test(a)), 1)

    def _count(self, slot: int, kind: str, test=lambda e: True) -> int:
        return sum(1 for e in self.events[slot] if e.get("kind") == kind and test(e))

    def _time(self, slot: int, kind: str, test=lambda e: True) -> float | None:
        return next((round(e["t"], 1) for e in self.events[slot] if e.get("kind") == kind and test(e)), None)

    def of(self, slot: int, obs: dict) -> dict:
        """Every metric for a player, from its last observation before the game ended (once it has ended the game
        lists none of its units, as wc3agent's score() notes)."""
        obs = self.live.get(slot) or obs
        units = obs.get("units") or []
        heroes = [u for u in units if u.get("hero")]
        samples = self.samples[slot] or [{"workers": 0, "workers_outside": 0, "gold": 0}]
        score = obs.get("score") or {}
        own = {u["unit_id"] for u in units}
        mine = set(self.handle_ids(slot, "army", "hero"))
        staged = self.handle_ids(None, "enemy")
        alive = [u for u in units if u["unit_id"] in mine]
        start = sum((h["strength"] or 0) for n, h in self.handles.items()
                    if n in ("army", "hero") and h["slot"] == slot)
        halls = [u for u in units
                 if u.get("structure") and u["type_id"] in ALL_HALLS and u.get("state") != "constructing"]
        standing = [u for u in units if u.get("structure") and u.get("hp", 0) > 0 and u.get("state") != "constructing"]
        lost = [e for e in self.events[slot] if e.get("kind") == "death" and e.get("owner") == slot]
        enemy_deaths = {e.get("unit_id"): e for s in self.events.values() for e in s if e.get("kind") == "death"
                        and e.get("owner") in self.enemies[slot]}
        home = self.home(obs)
        nearest = min(self.camps, key=lambda c: math.dist((c["x"], c["y"]), (home["x"], home["y"])), default=None) \
            if home else None
        skills = {u["unit_id"]: sum(a.get("level", 0) for a in u.get("abilities") or ()
                                    if a.get("ability_id") in (self.facts(u["type_id"]).get("potential_hero_abilities")
                                                               or ())) for u in heroes}
        return {
            "total": score.get("total", 0), "gold_mined": score.get("gold_mined", 0),
            "lumber_total": score.get("lumber_total", 0), "units_killed": score.get("units_killed", 0),
            "units_trained": score.get("units_trained", 0),
            "army": army_value(units, self.ref), "workers": samples[-1]["workers"],
            "fewest_workers": min(s["workers_outside"] for s in samples),
            "structures": len(standing),
            "hero_alive": bool(heroes), "hero_level": max((h.get("level", 0) for h in heroes), default=0),
            "hero_health_percent": round(100 * min((h["hp"] / h["max_hp"] for h in heroes if h.get("max_hp")),
                                                   default=0)),
            "unspent_skill_points": sum(h.get("level", 0) - skills.get(h["unit_id"], 0) for h in heroes)
            if heroes else None,
            "items_carried": len(obs.get("inventory") or ()),
            "structure_health_percent": round(100 * min((u["hp"] / u["max_hp"] for u in standing if u.get("max_hp")),
                                                        default=0)),
            "tier": max((i + 1 for i, h in enumerate(HALLS) for u in halls if u["type_id"] in h), default=0),
            "expansions": max(0, len(halls) - 1),
            "units_lost": sum(1 for e in lost if not self.facts(e.get("type_id", "")).get("structure")
                              and not self.facts(e.get("type_id", "")).get("builds")),
            "workers_lost": sum(1 for e in lost if self.facts(e.get("type_id", "")).get("builds")),
            "structures_lost": sum(1 for e in lost if self.facts(e.get("type_id", "")).get("structure")),
            "buildings_destroyed": sum(1 for e in enemy_deaths.values()
                                       if self.facts(e.get("type_id", "")).get("structure")),
            "items_picked_up": self._count(slot, "item_pickup"), "items_used": self._count(slot, "item_use"),
            "items_bought": self._count(slot, "item_sold", lambda e: e.get("buyer_id") in (mine or own)),
            "researches_done": self._count(slot, "research_finish"),
            "upgrade_started_time": self._time(slot, "upgrade_start"),
            "expansion_started_time": self._time(slot, "construct_start", lambda e: e.get("type_id") in {
                t for t, u in self.ref.units.items() if u.get("structure") and (u.get("food_made") or 0) >= 10}),
            "hero_time": min((t for raw, t in self.first_seen[slot].items() if self.facts(raw).get("hero")),
                             default=None),
            "enemy_base_seen_time": self.base_seen[slot],
            "idle_worker_seconds": self._dt(slot, lambda s: s["idle_workers"] > 0),
            "supply_blocked_seconds": self._dt(slot, lambda s: s["food_left"] <= 0 and s["food_cap"] < 100),
            "average_unspent_gold": round(sum(s["gold"] for s in samples) / len(samples)),
            "uprooted_seconds": self._dt(slot, lambda s: s["uprooted"] > 0),
            "army_kept_percent": round(100 * strength(alive, self.ref) / start) if start else None,
            "enemy_army_destroyed_percent": round(100 * sum(i in self.deaths for i in staged) / len(staged))
            if staged else None,
            "camp_cleared": nearest is not None and nearest["number"] in self.cleared[slot],
            "camp_cleared_time": self.cleared_at[slot].get(nearest["number"]) if nearest else None,
            "camps_cleared": sorted(self.cleared[slot]),
            "seconds": round(obs.get("game_time_seconds") or 0, 1),
            "count": {t: sum(1 for u in units if u["type_id"] == t and u.get("hp", 0) > 0
                             and u.get("state") != "constructing") for t in {u["type_id"] for u in units}},
            "first_time": dict(self.first_seen[slot]),
            "present_seconds": {t: self._dt(slot, lambda s, t=t: s["types"].get(t, 0) > 0)
                                for t in {t for s in self.samples[slot] for t in s["types"]}},
        }

    def handle_ids(self, slot: int | None, *names: str) -> list[int]:
        return [i for n in names if n in self.handles and (slot is None or self.handles[n]["slot"] == slot)
                for i in self.handles[n]["ids"]]
