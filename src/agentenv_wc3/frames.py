"""Warcraft III games as the RTS spectator sees them (agentenv_rts.timeline): the map from wc3env's prepared map
data (bounds, the 32-unit pathing grid, trees, gold mines, start locations, creep camps, shops), and a frame per
step from the players' observations. Every player's observation is merged, so the view shows what any of them
sees; enemies outside everyone's sight are not in it."""

from __future__ import annotations

import base64
import math
import zlib
from collections import Counter
from pathlib import Path

from agentenv_rts.timeline import HERO, STRUCTURE, UNIT, WORKER, player_color

from . import render

NO_WALK, NO_FLY, NO_BUILD = 0x02, 0x04, 0x08
CONTROLLERS = {"agent": "agent", "computer": "the game's AI"}


def terrain(pathing: dict | None) -> dict | None:
    """The map's pathing grid as the timeline's terrain codes: 0 open, 1 open but not buildable, 2 water (flyable,
    not walkable), 3 blocked."""
    if not pathing or not pathing.get("flags"):
        return None
    try:
        flags = zlib.decompress(base64.b64decode(pathing["flags"]))
    except (ValueError, zlib.error):
        return None
    if len(flags) != pathing["width"] * pathing["height"]:
        return None
    codes = bytes(3 if f & NO_WALK and f & NO_FLY else 2 if f & NO_WALK else 1 if f & NO_BUILD else 0 for f in flags)
    return {"origin": list(pathing["origin"]), "cell": pathing["cell"], "width": pathing["width"],
            "height": pathing["height"], "codes": base64.b64encode(codes).decode()}


def static(game: str, scenario: dict, setup: dict | None, observations: dict[int, dict], ref: render.Reference,
           labels: dict[int, str] | None = None) -> dict:
    info = render.map_info(scenario["map"])
    first = next(iter(observations.values()), {}) if observations else {}
    bounds = info.get("bounds") or (first.get("map") or {}).get("bounds") or {
        "min_x": -8192, "min_y": -8192, "max_x": 8192, "max_y": 8192}
    points = [{"kind": "start", "x": s["x"], "y": s["y"], "label": f"start {s.get('player', '')}".strip()}
              for s in info.get("start_locations") or ()]
    points += [{"kind": "gold", "x": g["x"], "y": g["y"], "label": f"gold mine ({g.get('gold', '?')})"}
               for g in info.get("gold_mines") or ()]
    points += [{"kind": "camp", "x": c["x"], "y": c["y"], "label": "creep camp"}
               for c in info.get("creep_camps") or () if "x" in c and "y" in c]
    points += [{"kind": "shop", "x": b["x"], "y": b["y"], "label": b.get("name") or "shop"}
               for b in info.get("neutral_buildings") or ()]
    trees = info.get("trees") or {}
    players = []
    for p in (setup or {}).get("players") or [{"slot": 0, "control": "agent", "race": scenario.get("race")},
                                              {"slot": 1, "control": "computer",
                                               "race": scenario.get("opponent_race")}]:
        slot = p["slot"]
        players.append({"slot": slot, "race": (p.get("race") or "").replace("_", " "),
                        "controller": CONTROLLERS.get(p.get("control"), p.get("control") or ""),
                        "label": (labels or {}).get(slot) or (f"Player {slot + 1}" if p.get("control") == "agent"
                                                              else f"{scenario.get('ai_difficulty', '')} AI".strip()),
                        "color": player_color(slot)})
    types = {}
    for obs in observations.values():
        for u in [*(obs.get("units") or ()), *(obs.get("visible_enemies") or ())]:
            types.setdefault(u["type_id"], ref.name(u["type_id"]))
    return {"game": game, "title": scenario.get("title") or f"Warcraft III · {Path(scenario['map']).stem}",
            "map": scenario["map"], "bounds": bounds, "terrain": terrain(info.get("pathing")),
            "trees": [[x, y] for x, y in zip(trees.get("x") or (), trees.get("y") or (), strict=False)],
            "points": points, "players": players, "types": types,
            "time_limit": scenario.get("time_limit_seconds")}


def kind(u: dict) -> int:
    if u.get("structure"):
        return STRUCTURE
    if u.get("hero"):
        return HERO
    return WORKER if u.get("type_id") in render.WORKERS else UNIT


def frame(observations: dict[int, dict], result: str = "", events: list | None = None,
          ref: render.Reference | None = None, notes: list[dict] | None = None, wall: float | None = None) -> dict:
    """One step: every unit any player sees (each once), each player's own resources, score, counts and army value,
    the feed's new events, what players told the spectators, and the wall time into the game's picture."""
    units, seen, players, t = [], set(), {}, 0.0
    for slot, obs in sorted(observations.items()):
        t = max(t, obs.get("game_time_seconds") or 0.0)
        own = obs.get("units") or []
        for u in [*own, *(obs.get("visible_enemies") or ())]:
            if u["unit_id"] in seen:
                continue
            seen.add(u["unit_id"])
            hp = round(100 * u.get("hp", 0) / u["max_hp"]) if u.get("max_hp") else 100
            units.append([u["unit_id"], u.get("owner", -1), u["type_id"], round(u["x"]), round(u["y"]), hp, kind(u)])
        player = obs.get("player") or {}
        players[str(slot)] = {"gold": player.get("gold"), "lumber": player.get("lumber"),
                              "food": [player.get("food_used"), player.get("food_cap")],
                              "score": (obs.get("score") or {}).get("total"),
                              "units": sum(not u.get("structure") for u in own),
                              "structures": sum(bool(u.get("structure")) for u in own),
                              "army": army_value(own, ref) if ref is not None else None}
    out = {"t": round(t, 3), "players": players, "units": units, "events": list(events or ()), "result": result}
    if notes:
        out["notes"] = notes
    if wall is not None:
        out["w"] = round(wall, 2)
    return out


def army_value(units: list[dict], ref: render.Reference) -> int:
    """What a player's living army cost, in gold and lumber: no workers or buildings."""
    total = 0
    for u in units:
        if u.get("hp", 0) > 0 and not u.get("structure") and u["type_id"] not in render.WORKERS:
            facts = ref.units.get(u["type_id"]) or {}
            total += (facts.get("gold") or 0) + (facts.get("lumber") or 0)
    return total


HALLS = ({"htow", "ogre", "unpl", "etol"}, {"hkee", "ostr", "unp1", "etoa"}, {"hcas", "ofrt", "unp2", "etoe"})
CREEPS = 12            # neutral hostile: the map's creep camps
FIGHT_GAP = 8.0        # game seconds without a blow that end a fight
FIGHT_RADIUS = 1400.0  # blows this close are one fight
FIGHT_SHOWN = 4        # a fight is in the feed once this many blows have landed
MOMENT_SECONDS = 8.0   # the camera may still show a key moment this long after it


class Feed:
    """The spectators' feed from every player's raw events (wc3env's): names instead of ids, one line when a fight
    starts and one when it ends instead of one per blow, and the moments that matter marked `major`. It remembers
    where the key moments happened, for the camera (`moments`)."""

    def __init__(self, ref: render.Reference, static: dict):
        self.ref = ref
        self.labels = {p["slot"]: p["label"] for p in static.get("players") or ()}
        self.points = [p for p in static.get("points") or () if p["kind"] in ("camp", "shop", "gold", "start")]
        b = static.get("bounds") or {"min_x": 0, "max_x": 0, "min_y": 0, "max_y": 0}
        self.center = ((b["min_x"] + b["max_x"]) / 2, (b["min_y"] + b["max_y"]) / 2)
        self.bases: dict[int, tuple[float, float]] = {}
        self.where: dict[int, tuple[float, float, int, str]] = {}   # unit id → x, y, owner, type
        self.fights: list[dict] = []
        self.moments: list[dict] = []

    def side(self, owner: int | None) -> str:
        if owner == CREEPS:
            return "the creeps"
        return self.labels.get(owner) or ("neutral" if owner in (13, 14, 15) else f"player {owner}")

    def place(self, x: float, y: float) -> str:
        for slot, (bx, by) in self.bases.items():
            if math.dist((x, y), (bx, by)) < 1800:
                return f"{self.side(slot)}'s base"
        near = min(self.points, key=lambda p: math.dist((x, y), (p["x"], p["y"])), default=None)
        if near is not None and math.dist((x, y), (near["x"], near["y"])) < 1200:
            return {"camp": "a creep camp", "gold": "a gold mine", "start": "a start location"}.get(
                near["kind"], f"the {near.get('label') or 'shop'}")
        dx, dy = x - self.center[0], y - self.center[1]
        ns = "north" if dy > 1500 else "south" if dy < -1500 else ""
        ew = "east" if dx > 1500 else "west" if dx < -1500 else ""
        return f"the {ns}{'-' if ns and ew else ''}{ew}" if ns or ew else "the middle of the map"

    def see(self, observations: dict[int, dict]) -> list[dict]:
        """This step's feed items, from every player's observation."""
        t, raw, taken = 0.0, [], Counter()
        for obs in observations.values():
            t = max(t, obs.get("game_time_seconds") or 0.0)
            for u in [*(obs.get("units") or ()), *(obs.get("visible_enemies") or ())]:
                self.where[u["unit_id"]] = (u["x"], u["y"], u.get("owner", -1), u["type_id"])
                if u.get("structure") and u["type_id"] in HALLS[0] | HALLS[1] | HALLS[2]:
                    self.bases.setdefault(u.get("owner", -1), (u["x"], u["y"]))
            here = Counter()   # an event both players saw is in both observations: keep it once
            for e in obs.get("events") or ():
                key = (e.get("kind"), e.get("unit_id"), e.get("trained_id"), e.get("type_id"), e.get("attacker_id"))
                here[key] += 1
                if here[key] > taken[key]:
                    taken[key] += 1
                    raw.append(e)
        items = []
        for e in raw:
            items += self._event(e, t)
        for fight in [f for f in self.fights if t - f["last"] > FIGHT_GAP]:
            self.fights.remove(fight)
            if fight["shown"]:
                items.append(self._fight_over(fight, t))
        self.moments = [m for m in self.moments if t - m["t"] <= MOMENT_SECONDS]
        return items

    def _item(self, text: str, side: int | None, kind: str, major: bool = False, at=None, t: float = 0.0,
              sides=None) -> dict:
        if major and at is not None:
            self.moments.append({"t": t, "x": at[0], "y": at[1], "kind": kind, "sides": sorted(sides or {side})})
        return {"text": text, "side": side, "kind": kind, "major": major}

    def _event(self, e: dict, t: float) -> list[dict]:
        kind, uid = e.get("kind"), e.get("unit_id")
        x, y, owner, type_id = self.where.get(uid, (None, None, e.get("owner"), e.get("type_id")))
        owner = e.get("owner", owner)
        at = (x, y) if x is not None else None
        name = self.ref.name(e.get("type_id") or type_id or "")
        facts = self.ref.units.get(e.get("type_id") or type_id or "") or {}
        who = self.side(owner)
        if kind == "attacked":
            return self._blow(e, t)
        if kind == "death":
            fight = self._fight_at(at, t) if at else None
            if fight is not None:
                fight["lost"].setdefault(owner, Counter())[name] += 1
            if facts.get("hero"):
                return [self._item(f"{who}'s {name} has fallen", owner, "death", True, at, t)]
            if facts.get("structure"):
                return [self._item(f"{who} lost a {name}", owner, "death", True, at, t)]
            return [] if fight is not None else [self._item(f"{who} lost a {name}", owner, "death")]
        if kind == "train_finish":
            trained = self.ref.units.get(e.get("type_id") or "") or {}
            if trained.get("hero"):
                return [self._item(f"{who} summoned a {name}", owner, "hero", True, at, t)]
            return [self._item(f"{who} trained a {name}", owner, "train")]
        if kind == "upgrade_finish" and (tier := next((i for i, h in enumerate(HALLS) if type_id in h or
                                                        e.get("type_id") in h), None)):
            return [self._item(f"{who} reached tier {tier + 1}: {name}", owner, "tier", True, at, t)]
        if kind == "construct_finish":
            if (e.get("type_id") or type_id) in HALLS[0] and at and owner in self.bases and math.dist(
                    at, self.bases[owner]) > 1800:
                return [self._item(f"{who} expanded: a {name} at {self.place(*at)}", owner, "expand", True, at, t)]
            return [self._item(f"{who} built a {name}", owner, "build")]
        if kind == "research_finish":
            return [self._item(f"{who} researched {name}", owner, "research")]
        if kind == "hero_level":
            hero = self.ref.name(type_id or "")
            return [self._item(f"{who}'s {hero} reached level {e.get('level')}", owner, "level", True, at, t)]
        if kind == "hero_learn" and e.get("ability_id"):
            hero = self.ref.name(type_id or "")
            return [self._item(f"{who}'s {hero} learned {self.ref.name(e['ability_id'])}", owner, "learn")]
        return []

    def _fight_at(self, at, t: float) -> dict | None:
        return next((f for f in self.fights if math.dist(at, (f["x"], f["y"])) <= FIGHT_RADIUS
                     and t - f["last"] <= FIGHT_GAP), None)

    def _blow(self, e: dict, t: float) -> list[dict]:
        """Folds a blow into its fight; the fight's opening line once it is a real fight."""
        victim, attacker = self.where.get(e.get("unit_id")), self.where.get(e.get("attacker_id"))
        if victim is None:
            return []
        fight = self._fight_at(victim[:2], t)
        if fight is None:
            fight = {"x": victim[0], "y": victim[1], "start": t, "last": t, "blows": 0, "shown": False,
                     "sides": {}, "lost": {}}
            self.fights.append(fight)
        fight["last"], fight["blows"] = t, fight["blows"] + 1
        for unit in (victim, attacker):
            if unit is not None:
                fight["sides"].setdefault(unit[2], set()).add(self.ref.name(unit[3]))
        if fight["shown"] or fight["blows"] < FIGHT_SHOWN or len(fight["sides"]) < 2:
            return []
        fight["shown"] = True
        sides = " vs ".join(self.side(o) for o in sorted(fight["sides"]))
        return [self._item(f"Fight at {self.place(fight['x'], fight['y'])}: {sides}", min(fight["sides"]), "fight",
                           True, (fight["x"], fight["y"]), t, fight["sides"])]

    def _fight_over(self, fight: dict, t: float) -> dict:
        losses = [f"{self.side(o)} lost " + ", ".join(f"{n} {name}" for name, n in c.most_common())
                  for o, c in sorted(fight["lost"].items())]
        text = (f"Fight at {self.place(fight['x'], fight['y'])} over after {t - fight['start']:.0f} s"
                + (": " + "; ".join(losses) if losses else ", no losses"))
        return self._item(text, None, "fight_end", bool(losses))


NEUTRAL_PASSIVE = 15   # gold mines, shops and critters: never a fight
CAMERA_FIGHT = 900.0   # a unit of ours this close to a hostile one is in a fight, and the camera goes there
CAMERA_ARMY = 1200.0   # without a fight, the camera shows our units this close to our strongest hero
CAMERA_STILL = 300.0   # the camera stays put while its spot moves less than this


def _ours(obs: dict) -> list[dict]:
    return [u for u in obs.get("units") or () if u.get("hp", 0) > 0 and not u.get("structure")
            and u["type_id"] not in render.WORKERS]


def fight_spot(obs: dict) -> tuple[float, float] | None:
    """The fight of ours with the most hostile units in it, as wc3agent films one; None when we are not fighting."""
    ours = _ours(obs)
    enemies = [e for e in obs.get("visible_enemies") or () if e.get("hp", 0) > 0 and not e.get("structure")
               and e.get("owner") != NEUTRAL_PASSIVE]
    if not ours or not enemies:
        return None
    near = {u["unit_id"]: [e for e in enemies if math.dist((e["x"], e["y"]), (u["x"], u["y"])) <= CAMERA_FIGHT]
            for u in ours}
    fighter = max(ours, key=lambda u: len(near[u["unit_id"]]))
    if not near[fighter["unit_id"]]:
        return None
    crowd = [fighter, *near[fighter["unit_id"]]]
    return sum(u["x"] for u in crowd) / len(crowd), sum(u["y"] for u in crowd) / len(crowd)


def army_spot(obs: dict) -> tuple[float, float] | None:
    """The army around our highest-level hero, else all our fighting units; None with none."""
    ours = _ours(obs)
    if not ours:
        return None
    heroes = [u for u in ours if u.get("hero")]
    lead = max(heroes, key=lambda u: (u.get("level", 0), u["unit_id"])) if heroes else None
    crowd = ([u for u in ours if math.dist((u["x"], u["y"]), (lead["x"], lead["y"])) <= CAMERA_ARMY]
             if lead else ours)
    return sum(u["x"] for u in crowd) / len(crowd), sum(u["y"] for u in crowd) / len(crowd)


def camera_spot(obs: dict) -> tuple[float, float] | None:
    """Where wc3agent's filmed game looks (its policies.camera_spot): our biggest fight, else our army; None with no
    fighting unit of ours."""
    return fight_spot(obs) or army_spot(obs)


def base_spot(obs: dict) -> tuple[float, float] | None:
    halls = [u for u in obs.get("units") or () if u.get("structure") and u["type_id"] in HALLS[0] | HALLS[1] | HALLS[2]]
    return (halls[0]["x"], halls[0]["y"]) if halls else None


MIN_SHOT = 4.0      # wall seconds a shot holds before the camera moves on
FIGHT_CUT = 1.5     # a fight of ours may cut into another shot after this long
BASE_EVERY = 30.0   # in quiet times the camera looks in on the base this often
BASE_SHOT = 5.0     # and stays this long


class Director:
    """Where the game's camera goes, for spectators watching the agent's picture: our fights first, then our key
    moments (a hero, a tier, an expansion, a building lost; Feed.moments), then the army around our strongest hero,
    with a look at the base now and then. A shot holds MIN_SHOT so a viewer can follow it; a fight follows its
    crowd as it moves."""

    def __init__(self, me: int = 0):
        self.me = me
        self.spot: tuple[float, float] | None = None
        self.reason = ""
        self.since = -math.inf
        self.base_at = 0.0
        self.shown: set[tuple] = set()

    def choose(self, obs: dict, moments: list[dict], now: float) -> tuple[float, float] | None:
        """The camera's next spot, or None to stay where it is."""
        held = now - self.since
        fight = fight_spot(obs)
        if fight is not None and (self.reason == "fight" or held >= FIGHT_CUT):
            return self._go(fight, "fight", now, follow=self.reason == "fight")
        if held < MIN_SHOT or (self.reason == "base" and held < BASE_SHOT):
            return None
        moment = next((m for m in reversed(moments) if self.me in m["sides"] and m["kind"] != "fight"
                       and (m["t"], m["kind"]) not in self.shown), None)
        if moment is not None:
            self.shown.add((moment["t"], moment["kind"]))
            return self._go((moment["x"], moment["y"]), moment["kind"], now)
        army, base = army_spot(obs), base_spot(obs)
        if base is not None and (army is None or now - self.base_at >= BASE_EVERY):
            self.base_at = now
            return self._go(base, "base", now)
        return self._go(army, "army", now, follow=self.reason == "army") if army is not None else None

    def _go(self, spot, reason: str, now: float, follow: bool = False) -> tuple[float, float] | None:
        if self.spot is not None and math.dist(spot, self.spot) < CAMERA_STILL:
            if not follow:
                self.reason, self.since = reason, now
            return None
        if not follow or reason != self.reason or self.spot is None or math.dist(spot, self.spot) > CAMERA_ARMY:
            self.since = now   # a new shot: following a crowd a little way is the same one
        self.spot, self.reason = spot, reason
        return spot
