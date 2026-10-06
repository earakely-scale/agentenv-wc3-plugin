"""wc3agent's scenarios as drill tasks: each definition becomes an ordinary task of player slots, a stage step
(`urn:wc3:stage/v1`), an opponent seat and a `checks` rubric. `agent-env wc3 drills import` writes them into the bundle
once; the bundle owns them from then on."""

from __future__ import annotations

import json
import math
from pathlib import Path

from .license import LICENSE_SECRETS

DEFINITIONS = Path("wc3agent/src/wc3agent/scenarios/definitions")   # in a wc3env checkout
TASKS = Path(__file__).with_name("bundles") / "wc3" / "tasks"
MAP = "(2)EchoIsles.w3x"   # wc3agent's MeleeConfig default, which its scenarios run on
AI_DIFFICULTY = "easy"   # likewise; most drills pause the AI anyway
MODEL = "anthropic/claude-haiku-4-5"
WALL_SECONDS_PER_GAME_SECOND = 12   # what the bundle's stepped tasks allow an agent
RACES = {"human": "human", "orc": "orc", "undead": "undead", "nightelf": "night_elf"}
SCRIPTED = ("attack", "raid")
OPPONENTS = ("computer", "idle", *SCRIPTED)
TIMED = {"camp_cleared_time"}   # metrics that say when their condition first held
KEYS = {"title", "goal", "minutes", "setup", "checks", "race", "opponent", "opponent_after_seconds", "finish",
        "finish_after_seconds", "skip_seconds"}
METRIC_HANDLES = ("army", "hero", "enemy")   # what army_kept_percent and enemy_army_destroyed_percent read


def task_name(name: str) -> str:
    return "drill-" + name.replace("_", "-")


def _place(op: dict) -> dict:
    return {"at": op.get("at", "home"), **{k: op[k] for k in ("dx", "dy") if k in op}}


def _stage_ops(name: str, definition: dict) -> list[dict]:
    """wc3agent's setup as the stage extension's ops. Level, items, hp and mana apply to a whole handle, so a spawn
    that has them and joins a handle earlier ops already fill gets a handle of its own."""
    setup, ops, spawned = definition["setup"], [], {}
    if definition.get("opponent") == "idle" and not any(o["op"] == "ai" and o.get("paused", True) for o in setup):
        ops.append({"op": "ai", "player": "opponent", "paused": True})
    for op in setup:
        kind = op["op"]
        if kind == "resources":
            ops.append({"op": "resources", "player": "wc3", "gold": op.get("gold", 0), "lumber": op.get("lumber", 0)})
        elif kind == "ai":
            ops.append({"op": "ai", "player": "opponent", "paused": op.get("paused", True)})
        elif kind == "item":
            ops.append({"op": "item", "type": op["type"], **_place(op)})
        elif kind in ("spawn", "hero"):
            enemy = op.get("owner") == "enemy"
            tag = op.get("tag", "enemy" if enemy else "army")
            spawned[tag] = spawned.get(tag, 0) + 1
            after = [*([{"op": "level", "level": op["level"]}] if op.get("level") else []),
                     *({"op": "give", "type": item} for item in op.get("items", [])),
                     *([{"op": "hp", "value": op["hp"]}] if op.get("hp") else []),
                     *([{"op": "mana", "value": op["mana"]}] if op.get("mana") is not None else [])]
            handle = tag
            if after and spawned[tag] > 1:
                if tag in METRIC_HANDLES:
                    raise ValueError(f"{name}: a {op['type']} joins {tag!r} with its own level, items, hp or mana, "
                                     f"which would apply to every unit there; give it a tag of its own")
                handle = f"{tag}-{spawned[tag]}"
            n = op.get("n", 1)
            ops.append({"op": "spawn", "player": "opponent" if enemy else "wc3", "type": op["type"],
                        **({"n": n} if n != 1 else {}), **_place(op), "as": handle})
            ops += [{"op": a["op"], "unit": handle, **a} for a in after]
        else:
            raise ValueError(f"{name}: unknown setup op {kind!r}")
    return ops


def checks(definition: dict) -> list[dict]:
    """The definition's checks. wc3agent's `seconds` is the time its finish condition first held; drills play on,
    so a check on it reads the finish metric's own time (`camp_cleared` → `camp_cleared_time`)."""
    finish = definition.get("finish")
    timed = f"{finish}_time" if isinstance(finish, str) and f"{finish}_time" in TIMED else None
    return [{**c, "metric": timed} if c.get("metric") == "seconds" and timed else c for c in definition["checks"]]


def convert(name: str, definition: dict) -> list[dict]:
    """The drill task's steps for wc3agent's scenario `name`. Its `finish` and `finish_after_seconds` are dropped:
    drills play to their time limit, and the summary's first-time metrics record when a goal was met."""
    if unknown := sorted(set(definition) - KEYS):
        raise ValueError(f"{name}: unknown fields {unknown}")
    opponent, race = definition.get("opponent", "computer"), definition.get("race", "human")
    if opponent not in OPPONENTS:
        raise ValueError(f"{name}: unknown opponent {opponent!r}; known: {', '.join(OPPONENTS)}")
    if race not in RACES:
        raise ValueError(f"{name}: unknown race {race!r}; known: {', '.join(RACES)}")
    task, scripted, warmup = task_name(name), opponent in SCRIPTED, definition.get("skip_seconds", 0)
    seconds = math.ceil(definition["minutes"] * 60)
    timeout = max(1800, WALL_SECONDS_PER_GAME_SECOND * seconds)
    ops = _stage_ops(name, definition)
    steps = [{"id": "deploy", "type": "deploy_env", "env_id": "wc3"},
             {"id": "agent", "type": "deploy_agent", "agent_name": "wc3", "a2a_agent_id": "wc3-macro-micro",
              "env_ids": [], "env_vars": {"WC3_MICRO_MODEL": MODEL, "WC3_GOAL": "prompt"}, "depends_on": ["deploy"]}]
    if scripted:
        steps.append({"id": "opponent", "type": "deploy_agent", "agent_name": "opponent",
                      "a2a_agent_id": "wc3-scripted", "env_ids": [],
                      "env_vars": {"SCRIPT": opponent,
                                   "SCRIPT_AFTER_SECONDS": str(definition.get("opponent_after_seconds", 0)),
                                   "SCRIPT_EVERY_SECONDS": "5"}, "depends_on": ["deploy"]})
    steps.append({"id": "license", "type": "add_license", "env_id": "wc3", "files": LICENSE_SECRETS,
                  "depends_on": ["deploy"]})
    steps.append({"id": "match", "type": "create_match", "env_id": "wc3",
                  "additional_settings": {"map": MAP, "seed": 1, "time_limit_seconds": seconds + warmup,
                                          "mode": "stepping"}, "depends_on": ["deploy"]})
    steps.append({"id": "slot-wc3", "type": "add_player_slot", "env_id": "wc3",
                  "occupant": {"kind": "agent", "name": "wc3"}, "slot": 0,
                  "additional_settings": {"faction": RACES[race]}, "depends_on": ["match", "agent"]})
    if scripted:
        steps.append({"id": "slot-opponent", "type": "add_player_slot", "env_id": "wc3",
                      "occupant": {"kind": "agent", "name": "opponent"}, "slot": 1,
                      "additional_settings": {"faction": "orc", "omniscient": True},
                      "depends_on": ["match", "opponent"]})
    else:
        steps.append({"id": "slot-ai", "type": "add_player_slot", "env_id": "wc3", "occupant": {"kind": "ai"},
                      "slot": 1, "additional_settings": {"faction": "orc", "ai_level": AI_DIFFICULTY},
                      "depends_on": ["match"]})
    steps.append({"id": "start", "type": "start_match", "env_id": "wc3",
                  "depends_on": ["slot-wc3", "slot-opponent" if scripted else "slot-ai", "license"]})
    start = "start"
    if ops or warmup:
        start = "stage"
        steps.append({"id": "stage", "type": "apply_server_config", "env_id": "wc3", "timeout_seconds": 600,
                      "directives": [{"service": "wc3", "uri": "urn:wc3:stage/v1",
                                      "args": {"warmup_seconds": warmup, "ops": ops}}],
                      "depends_on": ["start"]})
    steps.append({"id": "play", "type": "prompt_agent", "agent_name": "wc3", "model": MODEL, "prompt_id": task,
                  "prompt": definition["goal"], "timeout_seconds": timeout, "depends_on": [start]})
    if scripted:
        steps.append({"id": "play-opponent", "type": "prompt_agent", "agent_name": "opponent",
                      "prompt_id": f"{task}-opponent", "prompt": "Play your seat with the script.",
                      "timeout_seconds": timeout, "depends_on": [start]})
    return [*steps,
            {"id": "finish", "type": "rts_finish", "env_id": "wc3",
             "depends_on": ["play", "play-opponent"] if scripted else ["play"]},
            {"id": "grade", "type": "rts_grade", "env_id": "wc3", "seats": ["wc3"], "rubric": "checks",
             "checks": checks(definition), "verifier_id": "drill", "depends_on": ["finish"]},
            {"id": "replay", "type": "save_wc3_replay", "env_id": "wc3", "depends_on": ["grade"]},
            {"id": "recording", "type": "save_rts_recording", "env_id": "wc3", "depends_on": ["grade"]}]


def import_all(definitions_dir: Path, out_dir: Path) -> list[Path]:
    """Write `drill-<name>.json` into `out_dir` for every definition in `definitions_dir`; converts them all first,
    so a definition it refuses leaves nothing half written."""
    sources = sorted(Path(definitions_dir).glob("*.json"))
    if not sources:
        raise ValueError(f"no scenario definitions in {definitions_dir}")
    tasks = {task_name(p.stem): convert(p.stem, json.loads(p.read_text(encoding="utf-8"))) for p in sources}
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, steps in tasks.items():
        path = out_dir / f"{name}.json"
        path.write_text(json.dumps(steps, indent=2) + "\n", encoding="utf-8")
        written.append(path)
    return written
