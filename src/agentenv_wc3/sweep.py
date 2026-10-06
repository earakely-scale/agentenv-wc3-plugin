"""Sweeps: a task set crossed from one template task, and an eval over it (phase 4 of docs/task-design.md).

A sweep spec (TOML) names a template task (a bundled one, such as vs-ai-quick, or a task file), the axes it varies
(models, maps, races, opponents, seeds) and a per-game cost cap. `generate` writes one task per combination into a
bundle folder, with an eval over them and a manifest of each task's axes. `run` plays the eval under a spend budget,
one `agent-env run` per game: it never starts a game that could take the spend past the budget at the per-game cap.
`report` tabulates the results by model, map and seed.
"""

from __future__ import annotations

import copy
import itertools
import json
import re
import statistics
import subprocess
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from agent_env.store.routing import namespace_routing
from agent_env.task.store import get_task_instance_store

from .drills import TASKS

RACES = {"human": "Human", "orc": "Orc", "undead": "Undead", "night_elf": "Night Elf"}
WORKER = {"human": "Peasant", "orc": "Peon", "undead": "Acolyte", "night_elf": "Wisp"}
SUPPLY = {"human": "Farm", "orc": "Orc Burrow", "undead": "Ziggurat", "night_elf": "Moon Well"}
DIFFICULTIES = ("easy", "normal", "insane")
PROMPT = (
    "You play Warcraft III: The Frozen Throne as {race} on {map}, a {players}-player map, against the game's own "
    "{difficulty} AI ({opponent}). You win by destroying every enemy building; the game ends undecided after "
    "{minutes} minutes of game time, and is then judged on the game's score. You act only through the wc3 tools; "
    "nobody will answer questions, so never ask or wait for confirmation.\n\n"
    "Game time is frozen until you call advance, so think as long as you like. The loop: get_state shows your "
    "resources, units, structures, idle workers and what happened; act queues orders for your units by id; advance "
    "sends them and lets 1-60 game seconds pass (a few seconds in fights, longer while the economy runs). list_units "
    "shows ids, positions and orders; list_units with details=true shows what a unit can train, build, research and "
    "cast; resources lists gold mines and trees; lookup gives any type's cost, requirements and stats. Coordinates are "
    "the game's world units: copy them from the tools' output. Types can be given by name: train "
    "{{\"type_id\": \"{worker}\"}}, build {{\"type_id\": \"{supply}\", \"x\": ..., \"y\": ..., \"auto_place\": true}}. "
    "When the game refuses an order, advance says why; don't repeat it unchanged.\n\n"
    "Start by putting idle workers to work (harvest the gold mine, and trees for lumber), keep training workers, build "
    "supply before you are food-capped, then an army with a hero, and attack the enemy base (its start location is in "
    "get_state). Keep playing until advance reports GAME OVER, then reply with the result.")
SPEC_KEYS = {"name", "template", "models", "maps", "races", "opponents", "seeds", "time_limit_seconds",
             "max_cost_usd", "player_step", "prompt"}
INSTANCE = re.compile(r"(\d+(?:\.\d+)?)s, instance (\S+)")


@dataclass
class Spec:
    name: str
    template: str
    models: list[str]
    maps: list[str] = field(default_factory=lambda: ["(2)EchoIsles.w3x"])
    races: list[str] = field(default_factory=lambda: ["human"])
    opponents: list[dict] = field(default_factory=lambda: [{"computer": "easy", "race": "orc"}])
    seeds: list[int] = field(default_factory=lambda: [1])
    time_limit_seconds: int | None = None
    max_cost_usd: float = 0.5
    player_step: str = "play"
    prompt: str | None = PROMPT

    @classmethod
    def load(cls, path: Path) -> Spec:
        doc = tomllib.loads(path.read_text())
        if unknown := sorted(set(doc) - SPEC_KEYS):
            raise ValueError(f"{path}: unknown keys {unknown}; a sweep takes {', '.join(sorted(SPEC_KEYS))}")
        spec = cls(**doc)
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", spec.name):
            raise ValueError("a sweep's name is lowercase letters, digits and dashes")
        if not spec.models:
            raise ValueError("a sweep needs at least one model")
        for race in spec.races:
            if race not in RACES:
                raise ValueError(f"race {race!r} is not one of {', '.join(RACES)}")
        for o in spec.opponents:
            if o.get("computer") not in DIFFICULTIES or o.get("race") not in RACES:
                raise ValueError(f"an opponent is {{computer = easy|normal|insane, race = ...}}, got {o!r}")
        return spec

    def steps(self) -> list[dict]:
        path = Path(self.template)
        path = path if path.suffix == ".json" else TASKS / f"{self.template}.json"
        return json.loads(path.read_text())

    def combinations(self) -> list[dict]:
        """In rounds: every model plays a seed before any plays the next, so a budget that runs out cuts evenly."""
        return [{"model": m, "map": p, "race": r, "opponent": o, "seed": s}
                for s, p, r, o, m in itertools.product(self.seeds, self.maps, self.races, self.opponents, self.models)]


def short(text: str) -> str:
    """A model or map as a task name part: anthropic/claude-haiku-4-5 → claude-haiku-4-5, (2)EchoIsles.w3x →
    echoisles."""
    text = text.rpartition("/")[2].removesuffix(".w3x").removesuffix(".w3m")
    return re.sub(r"[^a-z0-9.-]+", "-", re.sub(r"^\(\d+\)", "", text).lower()).strip("-")


def task_name(spec: Spec, c: dict) -> str:
    """The task's name, from the axes the sweep varies."""
    parts = [spec.name]
    if len(spec.models) > 1:
        parts.append(short(c["model"]))
    if len(spec.maps) > 1:
        parts.append(short(c["map"]))
    if len(spec.races) > 1:
        parts.append(c["race"].replace("_", ""))
    if len(spec.opponents) > 1:
        parts.append(f"vs-{c['opponent']['computer']}-{c['opponent']['race'].replace('_', '')}")
    if len(spec.seeds) > 1:
        parts.append(f"s{c['seed']}")
    return "-".join(parts)


def map_name(map_file: str) -> tuple[str, int]:
    """(2)TerenasStand.w3x → ("Terenas Stand", 2)."""
    players = re.match(r"\((\d+)\)", Path(map_file).name)
    return (re.sub(r"(?<=[a-z])(?=[A-Z])", " ", Path(map_file).stem.split(")")[-1].split("_")[0]),
            int(players[1]) if players else 2)


def task_of(spec: Spec, template: list[dict], c: dict, name: str) -> list[dict]:
    """The template with one combination's axes: the match's map, seed and time limit; the player's slot's race and
    the AI slot's race and level; the player's model, prompt and per-game cost cap."""
    steps = copy.deepcopy(template)
    settings = next(s for s in steps if s["type"] == "create_match").setdefault("additional_settings", {})
    settings.update({"map": c["map"], "seed": c["seed"]})
    if spec.time_limit_seconds:
        settings["time_limit_seconds"] = spec.time_limit_seconds
    play = next(s for s in steps if s["id"] == spec.player_step)
    play.update({"model": c["model"], "prompt_id": name})
    opponent, slots = c["opponent"], [s for s in steps if s["type"] == "add_player_slot"]
    mine = next(s for s in slots if s["occupant"].get("name") == play.get("agent_name"))
    mine["additional_settings"] = {**mine.get("additional_settings", {}), "faction": c["race"]}
    if ai := next((s for s in slots if s["occupant"]["kind"] == "ai"), None):
        ai["additional_settings"] = {**ai.get("additional_settings", {}), "faction": opponent["race"],
                                     "ai_level": opponent["computer"]}
    if spec.prompt:
        pretty, players = map_name(c["map"])
        play["prompt"] = spec.prompt.format(
            race=RACES[c["race"]], map=pretty, players=players, difficulty=opponent["computer"],
            opponent=RACES[opponent["race"]], minutes=round(settings.get("time_limit_seconds", 1200) / 60),
            worker=WORKER[c["race"]], supply=SUPPLY[c["race"]])
    agent_step = next(s for s in steps if s["type"] == "deploy_agent" and s.get("agent_name") == play.get("agent_name"))
    agent_step["env_vars"] = {**agent_step.get("env_vars", {}), "WC3_MAX_COST_USD": str(spec.max_cost_usd)}
    return steps


def generate(spec: Spec, out: Path) -> list[str]:
    """Writes the sweep's bundle: tasks/<name>.json per combination, evals/<sweep>.toml, and sweep.json."""
    template = spec.steps()
    (out / "tasks").mkdir(parents=True, exist_ok=True)
    (out / "evals").mkdir(exist_ok=True)
    agent = next(s for s in template if s["id"] == spec.player_step).get("agent_name")
    manifest = {"name": spec.name, "template": spec.template, "max_cost_usd": spec.max_cost_usd, "agent": agent,
                "tasks": {}}
    for c in spec.combinations():
        name = task_name(spec, c)
        (out / "tasks" / f"{name}.json").write_text(json.dumps(task_of(spec, template, c, name), indent=2) + "\n")
        manifest["tasks"][name] = c
    (out / "evals" / f"{spec.name}.toml").write_text(
        "tasks = [\n" + "".join(f'  "{n}",\n' for n in manifest["tasks"]) + "]\n")
    (out / "sweep.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return list(manifest["tasks"])


def results(out: Path) -> list[dict]:
    path = out / "results.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.is_file() else []


def metadata(instance: str) -> dict:
    with namespace_routing():   # `agent-env run`'s instances live in the @local namespace's own store
        return ((get_task_instance_store().get(instance).context or {}).get("metadata")) or {}


def outcome(task: str, axes: dict, agent: str | None, instance: str | None, wall: float | None, code: int) -> dict:
    """One game's row: its grade, result, scores and the spend of `agent`'s seat (the one agent seat of a match
    without seats), from the run's stored instance. The cost is None when the run recorded none."""
    row = {"task": task, **axes, "instance": instance, "wall_seconds": wall, "exit": code}
    if instance is None:
        return {**row, "grade": 0.0, "cost_usd": None}
    found = metadata(instance)
    grade = next(iter((found.get("verifications") or {}).values()), {}).get("score", 0.0)
    summary = next(iter((found.get("rts_summary") or {}).values()), {})
    seats = summary.get("seats") or []
    me = next((x for x in seats if agent and x.get("agent") == agent), None) or next(
        (x for x in seats if not x.get("computer")), {})
    them = next((x for x in seats if x.get("team") != me.get("team")), {})
    mine, theirs, spend = me.get("metrics") or {}, them.get("metrics") or {}, me.get("spend") or {}
    return {**row, "grade": round(grade, 4), "result": me.get("result"), "score": mine.get("total"),
            "opponent_score": theirs.get("total"), "units_killed": mine.get("units_killed"),
            "orders": me.get("orders_sent"), "decisions": spend.get("decisions"),
            "cost_usd": spend.get("cost_usd"), "game_seconds": summary.get("game_time_seconds"),
            "error": summary.get("error")}


def cost(row: dict, cap: float) -> float:
    """What a game spent, or, when its run recorded no spend, its cap: the budget counts the worst case."""
    return cap if row.get("cost_usd") is None else row["cost_usd"]


def run(out: Path, budget: float, parallel: int = 2, agent_env: str = "agent-env", echo=print) -> list[dict]:
    """Plays the sweep's games not yet in results.jsonl, `parallel` at a time, while the spend so far plus the
    per-game cap of every game running and the next stays within `budget`."""
    manifest = json.loads((out / "sweep.json").read_text())
    rows, cap = results(out), float(manifest["max_cost_usd"])
    pending = [t for t in manifest["tasks"] if t not in {r["task"] for r in rows}]
    spent = sum(cost(r, cap) for r in rows)
    running: dict[str, subprocess.Popen] = {}
    logs = out / "logs"
    logs.mkdir(exist_ok=True)
    while pending or running:
        while pending and len(running) < parallel and spent + (len(running) + 1) * cap <= budget + 1e-9:
            task = pending.pop(0)
            echo(f"start {task} (spent ${spent:.2f} of ${budget:.2f})")
            running[task] = subprocess.Popen([agent_env, "run", str(out), "--task", task],
                                             stdout=(logs / f"{task}.log").open("w"), stderr=subprocess.STDOUT)
        if not running:
            echo(f"stopped: another game could take the spend past ${budget:.2f} (${spent:.2f} spent, "
                 f"${cap:.2f} a game at most); {len(pending)} not played")
            break
        time.sleep(5)
        for task, proc in list(running.items()):
            if proc.poll() is None:
                continue
            del running[task]
            found = INSTANCE.findall((logs / f"{task}.log").read_text(errors="replace"))
            wall, instance = (float(found[-1][0]), found[-1][1]) if found else (None, None)
            row = outcome(task, manifest["tasks"][task], manifest.get("agent"), instance, wall, proc.returncode)
            rows.append(row)
            spent += cost(row, cap)
            with (out / "results.jsonl").open("a") as f:
                f.write(json.dumps(row) + "\n")
            paid = (f"${row['cost_usd']:.3f}" if row["cost_usd"] is not None else
                    f"no spend recorded, counted as its cap ${cap:.2f}")
            echo(f"done  {task}: grade {row['grade']}, {row.get('result')}, {paid} (spent ${spent:.2f})")
    return rows


def report(out: Path) -> str:
    """The sweep's results as Markdown: by model, by model and map, and each model's spread across seeds."""
    rows = results(out)
    if not rows:
        return "No results yet."
    models = list(dict.fromkeys(r["model"] for r in rows))
    maps = list(dict.fromkeys(r["map"] for r in rows))

    def mean(xs):
        xs = [x for x in xs if x is not None]
        return statistics.fmean(xs) if xs else None

    def sd(xs):
        xs = [x for x in xs if x is not None]
        return statistics.stdev(xs) if len(xs) > 1 else 0.0

    manifest = json.loads((out / "sweep.json").read_text())
    cap, unknown = float(manifest["max_cost_usd"]), sum(r.get("cost_usd") is None for r in rows)
    lines = [f"Sweep `{manifest['name']}`: {len(rows)} games, ${sum(cost(r, cap) for r in rows):.2f} of model spend"
             + (f" ({unknown} with no spend recorded, counted at the cap)." if unknown else "."), "",
             "| Model | Games | Grade (mean ± sd) | Won / limit / lost | Score vs opponent | Score share | Cost a game "
             f"| At the ${cap:.2f} cap | Turns a game |", "|---|---|---|---|---|---|---|---|---|"]
    ranked = sorted(models, key=lambda m: -(mean(r["grade"] for r in rows if r["model"] == m) or 0))
    for m in ranked:
        mine = [r for r in rows if r["model"] == m]
        grades = [r["grade"] for r in mine]
        won = sum(r.get("result") == "victory" for r in mine)
        lost = sum(r.get("result") == "defeat" for r in mine)
        scored = [r for r in mine if r.get("score") is not None and r.get("opponent_score") is not None]
        versus = (f"{mean(r['score'] for r in scored) / 1000:.1f}k vs "
                  f"{mean(r['opponent_score'] for r in scored) / 1000:.1f}k") if scored else "–"
        share = mean(r["score"] / (r["score"] + r["opponent_score"]) for r in scored
                     if r["score"] + r["opponent_score"])
        paid, turns = mean(r.get("cost_usd") for r in mine) or 0, mean(r.get("decisions") for r in mine) or 0
        capped = sum((r.get("cost_usd") or 0) >= cap for r in mine)
        lines.append(f"| {m} | {len(mine)} | {mean(grades):.2f} ± {sd(grades):.2f} | {won} / {len(mine) - won - lost} "
                     f"/ {lost} | {versus} | {'–' if share is None else f'{share:.0%}'} | ${paid:.3f} | {capped} "
                     f"| {turns:.0f} |")
    lines += ["", "Grade by map, one per seed:", "", "| Model | " + " | ".join(short(p) for p in maps)
              + " | Spread across seeds (mean sd) |", "|---|" + "---|" * (len(maps) + 1)]
    for m in ranked:
        cells, spreads = [], []
        for p in maps:
            games = sorted((r for r in rows if r["model"] == m and r["map"] == p), key=lambda r: r["seed"])
            cells.append(" ".join(f"{r['grade']:.2f}" for r in games) or "–")
            spreads.append(sd([r["grade"] for r in games]))
        lines.append(f"| {m} | " + " | ".join(cells) + f" | {mean(spreads):.2f} |")
    if unfinished := [r for r in rows if r["instance"] is None or r.get("error")]:
        lines += ["", "Games that did not finish: " + ", ".join(
            f"{r['task']} ({r.get('error') or 'no instance recorded'})" for r in unfinished)]
    return "\n".join(lines) + "\n"
