"""wc3agent's mirror duels (duel.py) as tasks: two identical armies of one race fight on Echo Isles, staged either
side of the middle of the starts (the map is point-symmetric) as mirror images, with every upgrade of their race,
full mana and the same skills, both player slots casting on their own. The opponent fights the way Warcraft does:
`wc3-scripted` attack-moves its army at the other one every few seconds. The match is decided once one army is down
to 0.4 of the other's strength, and is a draw after 150 seconds. `mirror-<race>` puts a model (wc3-llm) against it;
`mirror-<race>-baseline` puts the scripted fighter on both sides, Warcraft against Warcraft, the duel's floor.
The match's seed nudges every unit and, odd or even, puts each army on either side of the middle, so sweeping seeds
varies the fight and gives each player each ground while both armies stay mirror images."""

from __future__ import annotations

import json
from pathlib import Path

from .drills import AGENT, EVALS, MODEL, TASKS, WALL_SECONDS_PER_GAME_SECOND
from .license import LICENSE_SECRETS
from .prompts import HOW_TO_PLAY, PLAY_TO_THE_END, SUPPLY, WORKER
from .render import Reference

MAP = "(2)EchoIsles.w3x"
SECONDS = 150   # wc3agent's DUEL_SECONDS: undecided by then, a draw
DECIDED = 0.4   # wc3agent's DECIDED_RATIO
ORDER_SECONDS = 3   # wc3agent's ORDER_SECONDS: how often Warcraft's side is re-aimed at the other army
FULL_MANA = 100000   # above any unit's maximum: the game caps it
FRONT, HEROES, RANGED, CASTERS, SIEGE = 0, 150, 280, 420, 560   # row depth behind the front line
HERO_LEVELS = (5, 3)   # the first and second hero
# wc3agent's armies, (type, count, row), heroes first: about 60 food each, every role.
ARMIES = {
    "human": [("Hamg", 1, HEROES), ("Hmkg", 1, HEROES), ("hfoo", 4, FRONT), ("hkni", 2, FRONT),
              ("hrif", 3, RANGED), ("hgyr", 2, RANGED), ("hmpr", 2, CASTERS), ("hsor", 2, CASTERS),
              ("hspt", 1, CASTERS), ("hmtm", 1, SIEGE)],
    "orc": [("Ofar", 1, HEROES), ("Otch", 1, HEROES), ("ogru", 4, FRONT), ("otau", 1, FRONT),
            ("orai", 2, FRONT), ("ohun", 3, RANGED), ("ospw", 1, RANGED), ("oshm", 2, CASTERS),
            ("odoc", 2, CASTERS), ("ocat", 1, SIEGE)],
    "undead": [("Udea", 1, HEROES), ("Ulic", 1, HEROES), ("ugho", 4, FRONT), ("uabo", 2, FRONT),
               ("ucry", 3, RANGED), ("ugar", 2, RANGED), ("unec", 2, CASTERS), ("uban", 2, CASTERS),
               ("umtw", 1, SIEGE)],
    "nightelf": [("Edem", 1, HEROES), ("Emoo", 1, HEROES), ("esen", 3, FRONT), ("edoc", 2, FRONT),
                 ("emtg", 1, FRONT), ("earc", 3, RANGED), ("ehip", 1, RANGED), ("edry", 2, CASTERS),
                 ("edot", 2, CASTERS), ("ebal", 1, SIEGE)],
}
FACTIONS = {"human": "human", "orc": "orc", "undead": "undead", "nightelf": "night_elf"}
GOAL = ("This is a duel: your army and an identical {race} army stand facing each other across the middle of the "
        "map, every upgrade researched, heroes leveled, full mana. The enemy fights the way Warcraft's own AI does. "
        "Destroy the enemy army while keeping as much of yours alive as you can: focus fire, pull wounded units "
        "back, cast your spells and use your heroes. The duel is decided once either army is down to 40% of the "
        "other's strength; after {seconds} seconds of game time it is a draw. Advance a few seconds at a time while "
        "the armies fight.")


def task_name(race: str, baseline: bool = False) -> str:
    return f"mirror-{race}" + ("-baseline" if baseline else "")


def army(race: str) -> list[list]:
    """The race's army as the formation op takes it: [type, count, row, level] for each hero, else [type, count,
    row]."""
    out, heroes = [], 0
    for raw, n, depth in ARMIES[race]:
        if raw[0].isupper():   # a hero's type id starts with a capital
            out.append([raw, n, depth, HERO_LEVELS[min(heroes, 1)]])
            heroes += 1
        else:
            out.append([raw, n, depth])
    return out


def upgrades(race: str, ref: Reference) -> list[tuple[str, int]]:
    """Every upgrade of the race at its highest level, as wc3agent's duel owns them for both players."""
    return sorted((raw, len(u["levels"])) for raw, u in ref.upgrades.items()
                  if u.get("race") == race and u.get("levels"))


def stage_ops(race: str, ref: Reference) -> list[dict]:
    researched = [{"op": "research", "player": player, "type": raw, "level": level}
                  for player in ("wc3", "opponent") for raw, level in upgrades(race, ref)]
    return [{"op": "formation", "player": "wc3", "army": army(race), "as": "army"},
            {"op": "formation", "player": "opponent", "army": army(race), "as": "enemy"},
            *researched,
            {"op": "learn", "unit": "army"}, {"op": "learn", "unit": "enemy"},
            {"op": "autocast", "unit": "army"}, {"op": "autocast", "unit": "enemy"},
            {"op": "mana", "unit": "army", "value": FULL_MANA}, {"op": "mana", "unit": "enemy", "value": FULL_MANA},
            {"op": "clear"}]


def scripted(name: str) -> dict:
    return {"id": name, "type": "deploy_agent", "agent_name": name, "a2a_agent_id": "wc3-scripted", "env_ids": [],
            "env_vars": {"SCRIPT": "attack", "SCRIPT_EVERY_SECONDS": str(ORDER_SECONDS)}, "depends_on": ["deploy"]}


def task(race: str, ref: Reference, baseline: bool = False) -> list[dict]:
    name, faction = task_name(race, baseline), FACTIONS[race]
    timeout = max(1800, WALL_SECONDS_PER_GAME_SECOND * SECONDS)
    player = ({**scripted("wc3"), "id": "agent"} if baseline else
              {"id": "agent", "type": "deploy_agent", "agent_name": "wc3", "a2a_agent_id": AGENT, "env_ids": [],
               "depends_on": ["deploy"]})
    goal = GOAL.format(race=faction.replace("_", " ").title(), seconds=SECONDS)
    prompt = (f"{goal}\n\n{HOW_TO_PLAY.format(worker=WORKER[faction], supply=SUPPLY[faction])}\n\nYou act only "
              f"through the wc3 tools; nobody will answer questions, so never ask or wait for confirmation. "
              f"{PLAY_TO_THE_END}")
    play = ({"id": "play", "type": "prompt_agent", "agent_name": "wc3", "prompt_id": name,
             "prompt": "Play with the script.", "timeout_seconds": timeout, "depends_on": ["stage"],
             "fail_task_on_error": False} if baseline else
            {"id": "play", "type": "prompt_agent", "agent_name": "wc3", "model": MODEL, "prompt_id": name,
             "prompt": prompt, "timeout_seconds": timeout, "depends_on": ["stage"], "fail_task_on_error": False})
    settings = {"faction": faction, "autocast": True}
    return [
        {"id": "deploy", "type": "deploy_env", "env_id": "wc3"},
        player,
        scripted("opponent"),
        {"id": "license", "type": "add_license", "env_id": "wc3", "files": LICENSE_SECRETS, "depends_on": ["deploy"]},
        {"id": "match", "type": "open_lobby", "env_id": "wc3",
         "game_settings": {"map": MAP, "seed": 1, "time_limit_seconds": SECONDS,
                           "mode": "stepping", "decide_ratio": DECIDED}, "depends_on": ["deploy"]},
        {"id": "slot-wc3", "type": "add_player_slot", "env_id": "wc3", "player_id": "0", "player_kind": "agent",
         "player_name": "wc3", "game_settings": {**settings, "omniscient": True} if baseline else settings,
         "depends_on": ["match", "agent"]},
        {"id": "slot-opponent", "type": "add_player_slot", "env_id": "wc3", "player_id": "1",
         "player_kind": "agent", "player_name": "opponent", "game_settings": {**settings, "omniscient": True},
         "depends_on": ["match", "opponent"]},
        {"id": "start", "type": "close_lobby", "env_id": "wc3", "depends_on": ["slot-wc3", "slot-opponent", "license"]},
        {"id": "stage", "type": "apply_server_config", "env_id": "wc3", "timeout_seconds": 600,
         "directives": [{"service": "wc3", "uri": "urn:wc3:stage/v1", "args": {"ops": stage_ops(race, ref)}}],
         "depends_on": ["start"]},
        play,
        {"id": "play-opponent", "type": "prompt_agent", "agent_name": "opponent", "prompt_id": f"{name}-opponent",
         "prompt": "Play with the script.", "timeout_seconds": timeout, "depends_on": ["stage"],
         "fail_task_on_error": False},
        {"id": "finish", "type": "finish_match", "env_id": "wc3", "depends_on": ["play", "play-opponent"]},
        {"id": "grade", "type": "rts_grade", "env_id": "wc3", "player_names": ["wc3"], "verifier_id": "duel",
         "checks": [{"metric": "army_kept_percent"}, {"metric": "enemy_army_destroyed_percent"}],
         "depends_on": ["finish"]},
        {"id": "files", "type": "save_match_files", "env_id": "wc3", "depends_on": ["grade"]},
    ]


def tasks(ref: Reference) -> dict[str, list[dict]]:
    """Every mirror duel, by task name: each race's with a model, and its baseline."""
    return {task_name(race, baseline): task(race, ref, baseline) for race in ARMIES for baseline in (False, True)}


def evals() -> dict[str, str]:
    """The bundle's duel eval, by file name: each race's duel with a model."""
    return {"duels.toml": "tasks = [\n" + "".join(f'  "{task_name(race)}",\n' for race in ARMIES) + "]\n"}


def write_all(ref: Reference, tasks_dir: Path = TASKS, evals_dir: Path = EVALS) -> list[Path]:
    written = []
    for name, steps in tasks(ref).items():
        (path := tasks_dir / f"{name}.json").write_text(json.dumps(steps, indent=2) + "\n", encoding="utf-8")
        written.append(path)
    for name, text in evals().items():
        (path := evals_dir / name).write_text(text, encoding="utf-8")
        written.append(path)
    return written
