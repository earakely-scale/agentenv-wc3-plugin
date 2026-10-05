"""Warcraft III games as the RTS spectator sees them (agentenv_rts.timeline): the map from wc3env's prepared map
data (bounds, the 32-unit pathing grid, trees, gold mines, start locations, creep camps, shops), and a frame per
step from the players' observations. Every player's observation is merged, so the view shows what any of them
sees; enemies outside everyone's sight are not in it."""

from __future__ import annotations

import base64
import math
import zlib
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


def frame(observations: dict[int, dict], result: str = "", events: list[str] | None = None) -> dict:
    """One step: every unit any player sees (each once), and each player's own resources, score and counts."""
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
                              "structures": sum(bool(u.get("structure")) for u in own)}
    return {"t": round(t, 3), "players": players, "units": units, "events": list(events or ()), "result": result}


NEUTRAL_PASSIVE = 15   # gold mines, shops and critters: never a fight
CAMERA_FIGHT = 900.0   # a unit of ours this close to a hostile one is in a fight, and the camera goes there
CAMERA_ARMY = 1200.0   # without a fight, the camera shows our units this close to our strongest hero


def camera_spot(obs: dict) -> tuple[float, float] | None:
    """Where the game's camera looks, as wc3agent films a game (its policies.camera_spot): the fight of ours with
    the most hostile units in it, else the army around our highest-level hero, else our fighting units; None with no
    fighting unit of ours, which leaves the camera where it is (at first, on our base)."""
    ours = [u for u in obs.get("units") or () if u.get("hp", 0) > 0 and not u.get("structure")
            and u["type_id"] not in render.WORKERS]
    if not ours:
        return None
    enemies = [e for e in obs.get("visible_enemies") or () if e.get("hp", 0) > 0 and not e.get("structure")
               and e.get("owner") != NEUTRAL_PASSIVE]
    near = {u["unit_id"]: [e for e in enemies if math.dist((e["x"], e["y"]), (u["x"], u["y"])) <= CAMERA_FIGHT]
            for u in ours}
    fighter = max(ours, key=lambda u: len(near[u["unit_id"]]))
    if near[fighter["unit_id"]]:
        crowd = [fighter, *near[fighter["unit_id"]]]
    else:
        heroes = [u for u in ours if u.get("hero")]
        lead = max(heroes, key=lambda u: (u.get("level", 0), u["unit_id"])) if heroes else None
        crowd = ([u for u in ours if math.dist((u["x"], u["y"]), (lead["x"], lead["y"])) <= CAMERA_ARMY]
                 if lead else ours)
    return sum(u["x"] for u in crowd) / len(crowd), sum(u["y"] for u in crowd) / len(crowd)
