"""Sweeps: a task set crossed from template tasks, and an eval over it (phase 4 of docs/task-design.md).

A sweep spec (TOML) names its template tasks (a bundled one, such as vs-ai-quick, a glob of bundled ones, such as
drill-*, or a task file), the axes it varies (models, maps, races, opponents, seeds; an axis left out keeps each
template's own) and a per-game cost cap. `generate` writes one task per combination into a bundle folder, with an eval
over them and a manifest of each task's axes. `run` plays the eval under a spend budget, one `agent-env run` per game:
it never starts a game that could take the spend past the budget at the per-game cap. `report` tabulates full games
by model, AI level, map and seed (the ladder is the highest AI level a model beats), and drills by model and skill.
"""

from __future__ import annotations

import copy
import fnmatch
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

from .drills import SKILLS, TASKS, skill
from .prompts import HOW_TO_PLAY, PLAY_TO_THE_END, RACES, SUPPLY, WORKER

DIFFICULTIES = ("easy", "normal", "insane")
OUTCOMES = {"victory": "win", "draw": "draw", "time_limit": "draw", "finished": "draw", "defeat": "loss"}
BEATEN = 0.75   # a level is beaten when at least three in four of its games are won
PROMPT = (
    "You play Warcraft III: The Frozen Throne as {race} on {map}, a {players}-player map, against the game's own "
    "{difficulty} AI ({opponent}). You win by destroying every enemy building; the game ends after {minutes} "
    "minutes of game time, and a game nobody has won by then is a draw. You act only through the wc3 tools; "
    "nobody will answer questions, so never ask or wait for confirmation.\n\n" + HOW_TO_PLAY + "\n\n"
    "Start by putting idle workers to work (harvest the gold mine, and trees for lumber), keep training workers, build "
    "supply before you are food-capped, then an army with a hero, and attack the enemy base (its start location is in "
    "get_state). " + PLAY_TO_THE_END)
SPEC_KEYS = {"name", "template", "models", "maps", "races", "opponents", "seeds", "time_limit_seconds",
             "max_cost_usd", "player_step", "prompt"}
INSTANCE = re.compile(r"(\d+(?:\.\d+)?)s, instance (\S+)")


@dataclass
class Spec:
    name: str
    template: str | list[str]
    models: list[str]
    maps: list[str] | None = None
    races: list[str] | None = None
    opponents: list[dict] | None = None
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
        for race in spec.races or ():
            if race not in RACES:
                raise ValueError(f"race {race!r} is not one of {', '.join(RACES)}")
        for o in spec.opponents or ():
            if o.get("computer") not in DIFFICULTIES or o.get("race") not in RACES:
                raise ValueError(f"an opponent is {{computer = easy|normal|insane, race = ...}}, got {o!r}")
        templates = spec.templates()
        if not templates:
            raise ValueError(f"no bundled task matches the template {spec.template!r}")
        if spec.prompt and (own_prompts := [t for t, steps in templates.items()
                                            if skill(t) or own(steps, spec.player_step)["opponent"] is None]):
            raise ValueError(f"drills and duels keep their own prompts ({', '.join(own_prompts)}): set prompt = "
                             "\"\"")
        return spec

    def templates(self) -> dict[str, list[dict]]:
        """The template tasks' steps by name: for each of `template` (one, or a list), a task file, or the bundled
        tasks it names or matches."""
        out = {}
        for given in [self.template] if isinstance(self.template, str) else self.template:
            path = Path(given)
            if path.suffix == ".json":
                out[path.stem] = json.loads(path.read_text())
                continue
            for name in sorted(p.stem for p in TASKS.glob("*.json") if fnmatch.fnmatchcase(p.stem, given)):
                out[name] = json.loads((TASKS / f"{name}.json").read_text())
        return out

    def combinations(self) -> list[dict]:
        """In rounds: every model plays a seed before any plays the next, so a budget that runs out cuts evenly. An
        axis the spec leaves out keeps each template's own."""
        out = []
        templates = {name: own(steps, self.player_step) for name, steps in self.templates().items()}
        for s in self.seeds:
            for name, mine in templates.items():
                out += [{"template": name, "model": m, "map": p, "race": r, "opponent": o, "seed": s}
                        for p, r, o, m in itertools.product(self.maps or [mine["map"]], self.races or [mine["race"]],
                                                            self.opponents or [mine["opponent"]], self.models)]
        return out


def own(steps: list[dict], player_step: str) -> dict:
    """A template's own map, its player's race and its computer opponent (None when it has none)."""
    settings = next(s for s in steps if s["type"] == "open_lobby").get("game_settings", {})
    play = next(s for s in steps if s["id"] == player_step)
    slots = [s for s in steps if s["type"] == "add_player_slot"]
    mine = next(s for s in slots if s.get("player_name") == play.get("agent_name")).get("game_settings", {})
    ai = next((s.get("game_settings", {}) for s in slots if s["player_kind"] == "ai"), None)
    return {"map": settings.get("map"), "race": mine.get("faction", "human"),
            "opponent": None if ai is None else {"computer": ai.get("ai_level") or "normal",
                                                 "race": ai.get("faction", "orc")}}


def short(text: str) -> str:
    """A model or map as a task name part: anthropic/claude-haiku-4-5 → claude-haiku-4-5, (2)EchoIsles.w3x →
    echoisles."""
    text = text.rpartition("/")[2].removesuffix(".w3x").removesuffix(".w3m")
    return re.sub(r"[^a-z0-9.-]+", "-", re.sub(r"^\(\d+\)", "", text).lower()).strip("-")


def task_name(spec: Spec, c: dict) -> str:
    """The task's name, from the templates and axes the sweep varies."""
    parts = [spec.name]
    if len(spec.templates()) > 1:
        parts.append(c["template"].removeprefix("drill-").removeprefix("mirror-"))
    if len(spec.models) > 1:
        parts.append(short(c["model"]))
    if len(spec.maps or ()) > 1:
        parts.append(short(c["map"]))
    if len(spec.races or ()) > 1:
        parts.append(c["race"].replace("_", ""))
    if len(spec.opponents or ()) > 1:
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
    settings = next(s for s in steps if s["type"] == "open_lobby").setdefault("game_settings", {})
    settings.update({"map": c["map"], "seed": c["seed"]})
    if spec.time_limit_seconds:
        settings["time_limit_seconds"] = spec.time_limit_seconds
    play = next(s for s in steps if s["id"] == spec.player_step)
    play.update({"model": c["model"], "prompt_id": name})
    opponent, slots = c["opponent"], [s for s in steps if s["type"] == "add_player_slot"]
    mine = next(s for s in slots if s.get("player_name") == play.get("agent_name"))
    mine["game_settings"] = {**mine.get("game_settings", {}), "faction": c["race"]}
    if opponent and (ai := next((s for s in slots if s["player_kind"] == "ai"), None)):
        ai["game_settings"] = {**ai.get("game_settings", {}), "faction": opponent["race"],
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
    templates = spec.templates()
    (out / "tasks").mkdir(parents=True, exist_ok=True)
    (out / "evals").mkdir(exist_ok=True)
    agent = next(s for s in next(iter(templates.values())) if s["id"] == spec.player_step).get("agent_name")
    manifest = {"name": spec.name, "template": spec.template, "max_cost_usd": spec.max_cost_usd, "agent": agent,
                "player_step": spec.player_step, "tasks": {}}
    for c in spec.combinations():
        name = task_name(spec, c)
        steps = task_of(spec, templates[c["template"]], c, name)
        (out / "tasks" / f"{name}.json").write_text(json.dumps(steps, indent=2) + "\n")
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


def outcome(task: str, axes: dict, agent: str | None, instance: str | None, wall: float | None, code: int,
            player_step: str = "play") -> dict:
    """One game's row: its grade and outcome, scores and the spend of `agent`'s player slot (the one agent of a match
    without player slots), from the run's stored instance. A game with no grade is void, with the reason; an agent
    whose play step failed had its game played out, and the row keeps its error. The cost is None when the run
    recorded none."""
    row = {"task": task, **axes, "instance": instance, "wall_seconds": wall, "exit": code}
    if instance is None:
        return {**row, "grade": None, "outcome": None, "void": "no instance recorded", "cost_usd": None}
    found = metadata(instance)
    verification = next(iter((found.get("verifications") or {}).values()), None)
    failed = found.get("failed_steps") or []
    void = None if verification else next((f["error"] for f in failed if f.get("is_fatal", True)), "not graded")
    agent_error = next((f["error"] for f in failed if f["step_id"] == player_step and not f.get("is_fatal", True)),
                       None)
    summary = next(iter((found.get("rts_summary") or {}).values()), {})
    players = summary.get("player_slots") or []
    me = next((x for x in players if agent and x.get("player_name") == agent), None) or next(
        (x for x in players if x.get("player_kind") == "agent"), {})
    them = next((x for x in players if x.get("team") != me.get("team")), {})
    mine, theirs, spend = me.get("metrics") or {}, them.get("metrics") or {}, me.get("spend") or {}
    return {**row, "grade": None if void else round(verification["score"], 4), "result": me.get("result"),
            "outcome": None if void else OUTCOMES.get(me.get("result")), "void": void, "agent_error": agent_error,
            "score": mine.get("total"),
            "opponent_score": theirs.get("total"), "units_killed": mine.get("units_killed"),
            "orders": me.get("orders_sent"), "refused": me.get("orders_refused"), "decisions": spend.get("decisions"),
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
            row = outcome(task, manifest["tasks"][task], manifest.get("agent"), instance, wall, proc.returncode,
                          manifest.get("player_step", "play"))
            rows.append(row)
            spent += cost(row, cap)
            with (out / "results.jsonl").open("a") as f:
                f.write(json.dumps(row) + "\n")
            paid = (f"${row['cost_usd']:.3f}" if row["cost_usd"] is not None else
                    f"no spend recorded, counted as its cap ${cap:.2f}")
            result = f"void ({row['void']})" if row.get("void") else f"{row['outcome']}, grade {row['grade']}"
            echo(f"done  {task}: {result}, {paid} (spent ${spent:.2f})")
    return rows


def won_drawn_lost(games: list[dict], sep: str = "-") -> str:
    """Games as won-drawn-lost, void games left out."""
    if not games:
        return "–"
    played = [g for g in games if not g.get("void")]
    return sep.join(str(sum(g.get("outcome") == o for g in played)) for o in ("win", "draw", "loss"))


def beaten(games_by_level: dict[str, list[dict]]) -> str:
    """The highest AI level a model beat, at least three in four of its games there won."""
    best = None
    for level in DIFFICULTIES:
        played = [g for g in games_by_level.get(level, []) if not g.get("void")]
        won = sum(g.get("outcome") == "win" for g in played)
        if played and won >= BEATEN * len(played):
            best = f"{level} ({won} of {len(played)} won)"
    return best or "none"


def mean(xs) -> float | None:
    xs = [x for x in xs if x is not None]
    return statistics.fmean(xs) if xs else None


def sd(xs) -> float:
    xs = [x for x in xs if x is not None]
    return statistics.stdev(xs) if len(xs) > 1 else 0.0


def share(games: list[dict]) -> float | None:
    """The mean share of the game's score a model took against its opponent."""
    return mean(g["score"] / (g["score"] + g["opponent_score"]) for g in games if g.get("score") is not None
                and g.get("opponent_score") is not None and g["score"] + g["opponent_score"])


def games_report(rows: list[dict], cap: float) -> list[str]:
    """Full games: by model (ranked on points, a win 1 and a draw 0.5, then on score share), by model and AI level with
    the highest level beaten, and by model and map."""
    models, maps = list(dict.fromkeys(r["model"] for r in rows)), list(dict.fromkeys(r["map"] for r in rows))
    levels = [d for d in DIFFICULTIES if any((r.get("opponent") or {}).get("computer") == d for r in rows)]
    lines = ["| Model | Games | Points (mean ± sd) | Won / drawn / lost | Void | Score share | Score vs opponent "
             f"| Cost a game | At the ${cap:.2f} cap | Turns a game | Orders a game (refused) |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    ranked = sorted(models, key=lambda m: (-(mean(r["grade"] for r in rows if r["model"] == m) or 0),
                                           -(share([r for r in rows if r["model"] == m]) or 0)))
    for m in ranked:
        mine = [r for r in rows if r["model"] == m]
        played = [r for r in mine if not r.get("void")]
        points = [r["grade"] for r in played]
        scored = [r for r in mine if r.get("score") is not None and r.get("opponent_score") is not None]
        versus = (f"{mean(r['score'] for r in scored) / 1000:.1f}k vs "
                  f"{mean(r['opponent_score'] for r in scored) / 1000:.1f}k") if scored else "–"
        ratio, mean_points = share(mine), "–" if not points else f"{mean(points):.2f} ± {sd(points):.2f}"
        paid, turns = mean(r.get("cost_usd") for r in mine) or 0, mean(r.get("decisions") for r in mine) or 0
        capped = sum((r.get("cost_usd") or 0) >= cap for r in mine)
        counted = [r for r in mine if r.get("orders") and r.get("refused") is not None]
        refused = sum(r["refused"] for r in counted) / sum(r["orders"] for r in counted) if counted else 0
        orders = f"{mean(r['orders'] for r in counted):.0f} ({refused:.0%})" if counted else "–"
        lines.append(f"| {m} | {len(mine)} | {mean_points} | {won_drawn_lost(mine, ' / ')} | {len(mine) - len(played)} "
                     f"| {'–' if ratio is None else f'{ratio:.0%}'} | {versus} | ${paid:.3f} | {capped} "
                     f"| {turns:.0f} | {orders} |")
    if levels:
        lines += ["", "Won-drawn-lost against each AI level, and the highest level beaten (three in four games won):",
                  "", "| Model | " + " | ".join(levels) + " | Highest level beaten |",
                  "|---|" + "---|" * (len(levels) + 1)]
    for m in ranked if levels else ():
        by_level = {d: [r for r in rows if r["model"] == m and (r.get("opponent") or {}).get("computer") == d]
                    for d in levels}
        lines.append(f"| {m} | " + " | ".join(won_drawn_lost(by_level[d]) for d in levels) + f" | {beaten(by_level)} |")
    lines += ["", "Points by map, one per seed:", "", "| Model | " + " | ".join(short(p) for p in maps)
              + " | Spread across seeds (mean sd) |", "|---|" + "---|" * (len(maps) + 1)]
    for m in ranked:
        cells, spreads = [], []
        for p in maps:
            games = sorted((r for r in rows if r["model"] == m and r["map"] == p), key=lambda r: r["seed"])
            cells.append(" ".join("void" if r.get("void") else f"{r['grade']:.2f}" for r in games) or "–")
            spreads.append(sd([r["grade"] for r in games if not r.get("void")]))
        lines.append(f"| {m} | " + " | ".join(cells) + f" | {mean(spreads):.2f} |")
    return lines


def drills_report(rows: list[dict], cap: float) -> list[str]:
    """Drills: by model (ranked on the share of checks met), by model and skill, and by drill. A drill is passed when
    every one of its checks is met."""
    models, drills = list(dict.fromkeys(r["model"] for r in rows)), list(dict.fromkeys(r["template"] for r in rows))
    skills = [g for g in SKILLS if any(skill(d) == g for d in drills)]

    def graded(model, where=lambda r: True):
        return [r for r in rows if r["model"] == model and not r.get("void") and where(r)]

    def passed(games):
        if not games:
            return "–"
        return f"{sum(r['grade'] >= 1 for r in games)} of {len(games)}, {mean(r['grade'] for r in games):.0%}"

    ranked = sorted(models, key=lambda m: -(mean(r["grade"] for r in graded(m)) or 0))
    lines = [f"| Model | Drills | Passed, checks met | Void | Cost a drill | At the ${cap:.2f} cap | Turns a drill |",
             "|---|---|---|---|---|---|---|"]
    for m in ranked:
        mine = [r for r in rows if r["model"] == m]
        paid, turns = mean(r.get("cost_usd") for r in mine) or 0, mean(r.get("decisions") for r in mine) or 0
        lines.append(f"| {m} | {len(mine)} | {passed(graded(m))} | {sum(bool(r.get('void')) for r in mine)} "
                     f"| ${paid:.3f} | {sum((r.get('cost_usd') or 0) >= cap for r in mine)} | {turns:.0f} |")
    lines += ["", "By skill: drills passed, and the share of checks met:", "",
              "| Model | " + " | ".join(skills) + " |", "|---|" + "---|" * len(skills)]
    for m in ranked:
        lines.append(f"| {m} | " + " | ".join(passed(graded(m, lambda r, g=g: skill(r["template"]) == g))
                                             for g in skills) + " |")
    lines += ["", "By drill, the share of checks met, one per seed:", "",
              "| Drill | Skill | " + " | ".join(ranked) + " |", "|---|---|" + "---|" * len(ranked)]
    for d in drills:
        cells = [" ".join("void" if r.get("void") else f"{r['grade']:.2f}"
                          for r in sorted((r for r in rows if r["model"] == m and r["template"] == d),
                                          key=lambda r: r["seed"])) or "–" for m in ranked]
        lines.append(f"| {d.removeprefix('drill-')} | {skill(d)} | " + " | ".join(cells) + " |")
    return lines


def report(out: Path) -> str:
    """The sweep's results as Markdown: full games and drills apart, then the games that were void and the agents
    that failed mid-game."""
    rows = results(out)
    if not rows:
        return "No results yet."
    manifest = json.loads((out / "sweep.json").read_text())
    cap, unknown = float(manifest["max_cost_usd"]), sum(r.get("cost_usd") is None for r in rows)
    drills = [r for r in rows if skill(r.get("template", ""))]
    games = [r for r in rows if not skill(r.get("template", ""))]
    lines = [f"Sweep `{manifest['name']}`: {len(rows)} games, ${sum(cost(r, cap) for r in rows):.2f} of model spend"
             + (f" ({unknown} with no spend recorded, counted at the cap)." if unknown else "."), ""]
    lines += games_report(games, cap) if games else []
    lines += ([""] if games and drills else []) + (drills_report(drills, cap) if drills else [])
    if void := [r for r in rows if r.get("void")]:
        lines += ["", "Void games, left out of the points: " + ", ".join(f"{r['task']} ({r['void']})" for r in void)]
    if failed := [r for r in rows if r.get("agent_error")]:
        lines += ["", "Agents that failed mid-game, their games played out and graded: " + ", ".join(
            f"{r['task']} ({r['agent_error']})" for r in failed)]
    return "\n".join(lines) + "\n"
