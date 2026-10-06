"""wc3agent's scenarios as drill tasks: what the importer makes of them, and that the bundle's 25 are exactly what it
makes, so the two never drift."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from wc3agent.scenarios.scenario import DEFINITIONS

from agentenv_wc3 import drills
from agentenv_wc3.cli import wc3

TASKS = Path(__file__).resolve().parents[1] / "src/agentenv_wc3/bundles/wc3/tasks"
DRILLS = sorted(p.stem for p in TASKS.glob("drill-*.json"))
STEP_TYPES = {"deploy_env", "deploy_agent", "wc3_license", "create_match", "add_player_slot", "start_match",
              "apply_server_config", "prompt_agent", "rts_finish", "rts_grade", "save_wc3_replay", "save_rts_recording"}


def definition(name: str) -> dict:
    return json.loads((DEFINITIONS / f"{name}.json").read_text(encoding="utf-8"))


def steps_of(name: str, given: dict | None = None) -> dict:
    return {s["id"]: s for s in drills.convert(name, given or definition(name))}


def ops(steps: dict) -> list[dict]:
    return steps["stage"]["directives"][0]["args"]["ops"]


def test_fight_even_is_the_design_docs_drill():
    goal = definition("fight_even")["goal"]
    assert drills.convert("fight_even", definition("fight_even")) == [
        {"id": "deploy", "type": "deploy_env", "env_id": "wc3"},
        {"id": "agent", "type": "deploy_agent", "agent_name": "wc3", "a2a_agent_id": "wc3-macro-micro", "env_ids": [],
         "env_vars": {"WC3_MICRO_MODEL": "anthropic/claude-haiku-4-5", "WC3_GOAL": "prompt"},
         "depends_on": ["deploy"]},
        {"id": "opponent", "type": "deploy_agent", "agent_name": "opponent", "a2a_agent_id": "wc3-scripted",
         "env_ids": [], "env_vars": {"SCRIPT": "attack", "SCRIPT_AFTER_SECONDS": "0", "SCRIPT_EVERY_SECONDS": "5"},
         "depends_on": ["deploy"]},
        {"id": "license", "type": "wc3_license", "env_id": "wc3", "depends_on": ["deploy"]},
        {"id": "match", "type": "create_match", "env_id": "wc3", "additional_settings": {
            "map": "(2)EchoIsles.w3x", "seed": 1, "time_limit_seconds": 150, "mode": "stepping"},
         "depends_on": ["deploy"]},
        {"id": "slot-wc3", "type": "add_player_slot", "env_id": "wc3", "occupant": {"kind": "agent", "name": "wc3"},
         "slot": 0, "additional_settings": {"faction": "human"}, "depends_on": ["match", "agent"]},
        {"id": "slot-opponent", "type": "add_player_slot", "env_id": "wc3",
         "occupant": {"kind": "agent", "name": "opponent"}, "slot": 1,
         "additional_settings": {"faction": "orc", "omniscient": True}, "depends_on": ["match", "opponent"]},
        {"id": "start", "type": "start_match", "env_id": "wc3", "depends_on": ["slot-wc3", "slot-opponent", "license"]},
        {"id": "stage", "type": "apply_server_config", "env_id": "wc3", "timeout_seconds": 600, "directives": [
            {"service": "wc3", "uri": "urn:wc3:stage/v1", "args": {"warmup_seconds": 0, "ops": [
                {"op": "spawn", "player": "wc3", "type": "Hamg", "at": "home", "dx": -1300, "as": "hero"},
                {"op": "level", "unit": "hero", "level": 3}, {"op": "give", "unit": "hero", "type": "phea"},
                {"op": "spawn", "player": "wc3", "type": "hfoo", "n": 5, "at": "home", "dx": -1150, "as": "army"},
                {"op": "spawn", "player": "wc3", "type": "hrif", "n": 3, "at": "home", "dx": -1450, "as": "army"},
                {"op": "spawn", "player": "opponent", "type": "Ofar", "at": "home", "dx": -2600, "as": "enemy"},
                {"op": "level", "unit": "enemy", "level": 3},
                {"op": "spawn", "player": "opponent", "type": "ogru", "n": 4, "at": "home", "dx": -2500, "dy": 150,
                 "as": "enemy"},
                {"op": "spawn", "player": "opponent", "type": "ohun", "n": 3, "at": "home", "dx": -2700, "dy": -150,
                 "as": "enemy"}]}}],
         "depends_on": ["start"]},
        {"id": "play", "type": "prompt_agent", "agent_name": "wc3", "model": "anthropic/claude-haiku-4-5",
         "prompt_id": "drill-fight-even", "prompt": goal, "timeout_seconds": 1800, "depends_on": ["stage"]},
        {"id": "play-opponent", "type": "prompt_agent", "agent_name": "opponent",
         "prompt_id": "drill-fight-even-opponent", "prompt": "Play your seat with the script.", "timeout_seconds": 1800,
         "depends_on": ["stage"]},
        {"id": "finish", "type": "rts_finish", "env_id": "wc3", "depends_on": ["play", "play-opponent"]},
        {"id": "grade", "type": "rts_grade", "env_id": "wc3", "seats": ["wc3"], "rubric": "checks", "checks": [
            {"metric": "enemy_army_destroyed_percent", "op": ">=", "value": 100},
            {"metric": "army_kept_percent", "op": ">=", "value": 40},
            {"metric": "hero_alive", "op": "==", "value": True},
            {"metric": "unspent_skill_points", "op": "<=", "value": 0},
            {"metric": "units_lost", "op": "<=", "value": 4}], "verifier_id": "drill", "depends_on": ["finish"]},
        {"id": "replay", "type": "save_wc3_replay", "env_id": "wc3", "depends_on": ["grade"]},
        {"id": "recording", "type": "save_rts_recording", "env_id": "wc3", "depends_on": ["grade"]}]


def test_a_computer_opponent_is_a_computer_seat_with_its_ai_paused_by_the_stage():
    steps = steps_of("expansion")
    assert list(steps) == ["deploy", "agent", "license", "match", "slot-wc3", "slot-ai", "start", "stage", "play",
                           "finish", "grade", "replay", "recording"]
    assert steps["slot-wc3"]["additional_settings"] == {"faction": "human"}
    assert (steps["slot-ai"]["occupant"], steps["slot-ai"]["additional_settings"]) == (
        {"kind": "ai"}, {"faction": "orc", "ai_level": "easy"})
    assert steps["match"]["depends_on"] == ["deploy"]
    assert steps["match"]["additional_settings"]["time_limit_seconds"] == 300
    assert ops(steps)[:5] == [
        {"op": "ai", "player": "opponent", "paused": True},
        {"op": "resources", "player": "wc3", "gold": 1500, "lumber": 800},
        {"op": "spawn", "player": "wc3", "type": "Hamg", "at": "home", "dx": -1200, "as": "hero"},
        {"op": "level", "unit": "hero", "level": 3}, {"op": "give", "unit": "hero", "type": "phea"}]
    assert steps["play"]["timeout_seconds"] == 3600


def test_a_raid_is_a_scripted_agent_seat_with_its_own_prompt():
    steps = steps_of("build_towers")
    assert steps["opponent"]["a2a_agent_id"] == "wc3-scripted"
    assert steps["opponent"]["env_vars"] == {"SCRIPT": "raid", "SCRIPT_AFTER_SECONDS": "150",
                                             "SCRIPT_EVERY_SECONDS": "5"}
    assert steps["slot-opponent"]["additional_settings"] == {"faction": "orc", "omniscient": True}
    assert steps["match"]["additional_settings"]["time_limit_seconds"] == 270
    assert steps["play-opponent"]["agent_name"] == "opponent" and "model" not in steps["play-opponent"]
    assert steps["play-opponent"]["depends_on"] == steps["play"]["depends_on"] == ["stage"]
    assert not any(o["op"] == "ai" for o in ops(steps))
    assert ops(steps)[-1] == {"op": "spawn", "player": "opponent", "type": "ogru", "n": 3,
                              "at": "toward:enemy_home:7500", "dx": 0, "dy": 0, "as": "enemy"}


def test_places_pass_through_and_checks_are_copied_verbatim():
    steps = steps_of("creep_hard")
    assert {o["at"] for o in ops(steps) if o["op"] == "spawn"} == {"toward:camp:9:900"}
    assert [o["dx"] for o in ops(steps) if o["op"] == "spawn"] == [0, 150, -150]
    assert steps["grade"]["checks"] == definition("creep_hard")["checks"]
    assert {"metric": "camp_cleared:camp:9", "op": "==", "value": True} in steps["grade"]["checks"]
    assert not {"finish", "finish_after_seconds"} & {k for s in steps.values() for k in s}   # wc3agent's own


def test_skip_seconds_warm_the_game_up_and_count_toward_the_time_limit():
    steps = steps_of("shopping")
    assert steps["stage"]["directives"][0]["args"]["warmup_seconds"] == 460
    assert steps["match"]["additional_settings"]["time_limit_seconds"] == 2 * 60 + 460
    assert ops(steps)[-2:] == [
        {"op": "spawn", "player": "wc3", "type": "Hmkg", "at": "home", "dx": -750, "dy": -600, "as": "hero"},
        {"op": "level", "unit": "hero", "level": 2}]
    items = [o for o in ops(steps_of("loot")) if o["op"] == "item"]
    assert len(items) == 4 and items[0] == {"op": "item", "type": "tint", "at": "home", "dx": -1200, "dy": -700}


def test_a_spawn_with_its_own_hp_joining_a_filled_handle_gets_a_handle_of_its_own():
    staged = ops(steps_of("build_repair"))
    assert [(o["op"], o.get("as") or o["unit"], o.get("value")) for o in staged[2:]] == [
        ("spawn", "base", None), ("hp", "base", 300), ("spawn", "base-2", None), ("hp", "base-2", 80),
        ("spawn", "base-3", None), ("hp", "base-3", 150), ("spawn", "base-4", None), ("hp", "base-4", 60)]
    hero = steps_of("hero_in_danger")
    assert [o for o in ops(hero) if o.get("unit") == "hero"] == [
        {"op": "level", "unit": "hero", "level": 3}, {"op": "give", "unit": "hero", "type": "phea"},
        {"op": "give", "unit": "hero", "type": "stwp"}, {"op": "hp", "unit": "hero", "value": 180}]
    army = {"title": "t", "goal": "g", "minutes": 1, "checks": [], "setup": [
        {"op": "spawn", "type": "hfoo", "n": 2}, {"op": "spawn", "type": "hrif", "hp": 50}]}
    with pytest.raises(ValueError, match="joins 'army'"):
        drills.convert("x", army)


def test_an_idle_opponent_is_a_paused_computer_seat_and_unknown_definitions_are_refused():
    idle = {"title": "t", "goal": "g", "minutes": 1, "checks": [], "opponent": "idle", "race": "nightelf",
            "setup": [{"op": "resources", "gold": 100}]}
    steps = steps_of("x", idle)
    assert steps["slot-wc3"]["additional_settings"] == {"faction": "night_elf"}
    assert steps["slot-ai"]["additional_settings"] == {"faction": "orc", "ai_level": "easy"}
    assert ops(steps) == [{"op": "ai", "player": "opponent", "paused": True},
                          {"op": "resources", "player": "wc3", "gold": 100, "lumber": 0}]
    paused = ops(steps_of("x", {**idle, "setup": [{"op": "ai"}]}))
    assert paused == [{"op": "ai", "player": "opponent", "paused": True}]
    for bad, message in [({"opponent": "insane"}, "unknown opponent 'insane'"),
                         ({"setup": [{"op": "kill"}]}, "unknown setup op 'kill'"), ({"race": "naga"}, "unknown race"),
                         ({"camera": "hero"}, r"unknown fields \['camera'\]")]:
        with pytest.raises(ValueError, match=message):
            drills.convert("x", {**idle, **bad})


def test_the_bundles_drills_are_what_the_importer_writes(tmp_path):
    written = drills.import_all(DEFINITIONS, tmp_path)
    assert sorted(p.stem for p in written) == DRILLS and len(DRILLS) == 25
    for path in written:
        assert path.read_bytes() == (TASKS / path.name).read_bytes(), path.name


def test_the_cli_imports_from_a_wc3env_checkout(tmp_path):
    checkout = DEFINITIONS.parents[4]
    result = CliRunner().invoke(wc3, ["drills", "import", "--wc3env", str(checkout), "--out", str(tmp_path)])
    assert result.exit_code == 0 and len(list(tmp_path.glob("drill-*.json"))) == 25, result.output
    bad = tmp_path / "bad"
    (bad / "src/wc3env").mkdir(parents=True)
    result = CliRunner().invoke(wc3, ["drills", "import", "--wc3env", str(bad), "--out", str(tmp_path / "x")])
    assert result.exit_code != 0 and "no scenario definitions" in result.output


@pytest.mark.parametrize("task", DRILLS)
def test_every_drill_is_a_consistent_task(task):
    steps = json.loads((TASKS / f"{task}.json").read_text())
    ids = [s["id"] for s in steps]
    assert len(set(ids)) == len(ids) and {s["type"] for s in steps} <= STEP_TYPES
    for i, s in enumerate(steps):
        assert set(s.get("depends_on", [])) <= set(ids[:i]), s["id"]
    match = next(s for s in steps if s["type"] == "create_match")
    deployed = {s["agent_name"]: s["id"] for s in steps if s["type"] == "deploy_agent"}
    prompted = {s["agent_name"] for s in steps if s["type"] == "prompt_agent"}
    slots = [s for s in steps if s["type"] == "add_player_slot"]
    seated = {s["occupant"]["name"]: s for s in slots if s["occupant"]["kind"] == "agent"}
    assert set(deployed) == set(seated) == prompted and match["depends_on"] == ["deploy"]
    assert all(seated[name]["depends_on"] == ["match", deployed[name]] for name in seated)
    start = next(s for s in steps if s["type"] == "start_match")
    assert start["depends_on"] == [*(s["id"] for s in slots), "license"]
    plays = [s for s in steps if s["type"] == "prompt_agent"]
    assert all(s["depends_on"] in (["start"], ["stage"]) for s in plays)
    assert all(s["env_ids"] == [] for s in steps if s["type"] == "deploy_agent")
    finish = next(s for s in steps if s["type"] == "rts_finish")
    assert sorted(finish["depends_on"]) == sorted(s["id"] for s in plays)
    assert next(s for s in steps if s["type"] == "rts_grade")["depends_on"] == [finish["id"]]
    handles = {None}
    for op in next((s["directives"][0]["args"]["ops"] for s in steps if s["type"] == "apply_server_config"), []):
        assert op.get("player", "wc3") in ("wc3", "opponent") and op.get("unit") in handles, op
        handles.add(op.get("as"))
    grade = next(s for s in steps if s["type"] == "rts_grade")
    assert grade["checks"] == drills.checks(definition(task.removeprefix("drill-").replace("-", "_")))


def test_a_check_on_the_finish_time_reads_the_finish_metrics_own_time():
    creep = definition("creep_easy")
    assert {"metric": "seconds", "op": "<=", "value": 90} in creep["checks"]
    assert {"metric": "camp_cleared_time", "op": "<=", "value": 90} in drills.checks(creep)
    assert drills.checks({"checks": [{"metric": "seconds", "op": "<=", "value": 9}]}) == [
        {"metric": "seconds", "op": "<=", "value": 9}]
