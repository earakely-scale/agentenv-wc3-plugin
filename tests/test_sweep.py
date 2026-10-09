"""Sweeps: the tasks a spec generates, the budget the runner keeps to, and the report."""

import json
import stat

import pytest
from agent_env.task_step.registry import get_task_step_registry

from agentenv_wc3 import sweep

SPEC = """
name = "first-eval"
template = "vs-ai-quick"
models = ["anthropic/claude-haiku-4-5", "openai/gpt-5.4-mini"]
maps = ["(2)EchoIsles.w3x", "(2)TerenasStand.w3x"]
races = ["orc"]
seeds = [1, 2]
max_cost_usd = 0.5
"""


@pytest.fixture
def generated(tmp_path):
    (tmp_path / "spec.toml").write_text(SPEC)
    out = tmp_path / "sweep"
    names = sweep.generate(sweep.Spec.load(tmp_path / "spec.toml"), out)
    return out, names


def test_a_spec_becomes_one_task_per_combination_and_an_eval_over_them(generated, local_stores):
    out, names = generated
    assert len(names) == 8 and "first-eval-claude-haiku-4-5-terenasstand-s2" in names
    assert names[:3] == ["first-eval-claude-haiku-4-5-echoisles-s1", "first-eval-gpt-5.4-mini-echoisles-s1",
                         "first-eval-claude-haiku-4-5-terenasstand-s1"]   # in rounds: each model before the next
    assert (out / "evals/first-eval.toml").read_text().count('"first-eval-') == 8
    steps = json.loads((out / "tasks/first-eval-gpt-5.4-mini-terenasstand-s2.json").read_text())
    registry = get_task_step_registry()
    for s in steps:
        registry[s["type"]].from_dict(s)
    by_id = {s["id"]: s for s in steps}
    settings = by_id["match"]["game_settings"]
    assert (settings["map"], settings["seed"], settings["time_limit_seconds"]) == ("(2)TerenasStand.w3x", 2, 300)
    assert by_id["slot-wc3"]["game_settings"] == {"faction": "orc", "team": 1}
    assert by_id["slot-ai"]["game_settings"] == {"faction": "orc", "team": 2, "ai_level": "easy"}
    play = by_id["play"]
    assert play["model"] == "openai/gpt-5.4-mini" and play["prompt_id"] == "first-eval-gpt-5.4-mini-terenasstand-s2"
    assert "as Orc on Terenas Stand, a 2-player map" in play["prompt"] and '{"type_id": "Peon"}' in play["prompt"]
    assert "after 5 minutes" in play["prompt"]
    assert by_id["agent"]["env_vars"] == {"WC3_MAX_COST_USD": "0.5"}
    manifest = json.loads((out / "sweep.json").read_text())
    assert manifest["tasks"]["first-eval-gpt-5.4-mini-terenasstand-s2"]["seed"] == 2


def test_a_template_of_agents_gets_the_race_on_the_players_slot_and_the_opponent_on_the_ais(tmp_path):
    template = [{"id": "match", "type": "open_lobby"},
                {"id": "slot-wc3", "type": "add_player_slot", "player_kind": "agent", "player_name": "wc3",
                 "game_settings": {"faction": "human", "team": 1}},
                {"id": "slot-ai", "type": "add_player_slot", "player_kind": "ai",
                 "game_settings": {"faction": "orc", "team": 2}},
                {"id": "agent", "type": "deploy_agent", "agent_name": "wc3"},
                {"id": "play", "type": "prompt_agent", "agent_name": "wc3", "prompt": "x"}]
    (tmp_path / "t.json").write_text(json.dumps(template))
    spec = sweep.Spec(name="s", template=str(tmp_path / "t.json"), models=["m"], races=["undead"],
                      opponents=[{"computer": "insane", "race": "night_elf"}], prompt=None)
    steps = sweep.task_of(spec, spec.templates()["t"], spec.combinations()[0], "s")
    assert steps[1]["game_settings"] == {"faction": "undead", "team": 1}
    assert steps[2]["game_settings"] == {"faction": "night_elf", "team": 2, "ai_level": "insane"}
    assert steps[4]["prompt"] == "x"


@pytest.mark.parametrize("extra, problem", [('maps_ = ["x"]', "unknown keys"), ('races = ["elf"]', "not one of"),
                                            ('opponents = [{computer = "hard", race = "orc"}]', "an opponent")])
def test_a_spec_with_a_mistake_is_refused(tmp_path, extra, problem):
    (tmp_path / "spec.toml").write_text('name = "s"\ntemplate = "vs-ai-quick"\nmodels = ["m"]\n' + extra)
    with pytest.raises(ValueError, match=problem):
        sweep.Spec.load(tmp_path / "spec.toml")


def fake_agent_env(tmp_path):
    script = tmp_path / "agent-env"
    script.write_text('#!/bin/sh\necho "  tasks/$4.json v1: scored below 1 (wc3: 0.4), 12.5s, instance @local/x/$4"\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def summary(cost: float | None, score: int) -> dict:
    """A shorthand match's summary, as the real game's: the agent's player slot has no agent name."""
    spend = {} if cost is None else {"cost_usd": cost, "decisions": 30, "tokens": 9000}
    return {"verifications": {"wc3": {"score": 0.4}}, "rts_summary": {"wc3": {
        "game_time_seconds": 300, "player_slots": [
        {"player_id": "0", "player_kind": "agent", "player_name": None, "team": 1, "result": "time_limit",
         "orders_sent": 40, "spend": spend, "metrics": {"total": score, "units_killed": 3}},
        {"player_id": "1", "player_kind": "ai", "player_name": None, "ai_level": "easy", "team": 2,
         "result": "time_limit", "orders_sent": 0, "spend": {}, "metrics": {"total": 1000}}]}}}


def test_the_runner_stops_before_a_game_could_take_the_spend_past_the_budget_and_resumes(generated, tmp_path,
                                                                                       monkeypatch):
    out, names = generated
    monkeypatch.setattr(sweep.time, "sleep", lambda _: None)
    monkeypatch.setattr(sweep, "metadata", lambda instance: summary(0.3, 500))
    said = []
    rows = sweep.run(out, budget=1.0, parallel=1, agent_env=fake_agent_env(tmp_path), echo=said.append)
    assert [r["task"] for r in rows] == names[:2]   # 0.3 spent + 0.5 fits in 1.0, 0.6 + 0.5 does not
    assert rows[0]["instance"] == f"@local/x/{names[0]}" and rows[0]["wall_seconds"] == 12.5
    assert (rows[0]["grade"], rows[0]["cost_usd"], rows[0]["score"], rows[0]["opponent_score"]) == (0.4, 0.3, 500, 1000)
    assert (rows[0]["result"], rows[0]["outcome"], rows[0]["void"], rows[0]["agent_error"]) == ("time_limit", "draw",
                                                                                                None, None)
    assert (rows[0]["orders"], rows[0]["decisions"]) == (40, 30)
    assert said[-1].startswith("stopped: another game could take the spend past $1.00")
    rows = sweep.run(out, budget=10.0, parallel=3, agent_env=fake_agent_env(tmp_path), echo=said.append)
    assert sorted(r["task"] for r in rows) == sorted(names) and len(sweep.results(out)) == 8


def test_a_game_without_a_recorded_spend_counts_as_its_cap(generated, tmp_path, monkeypatch):
    out, names = generated
    monkeypatch.setattr(sweep.time, "sleep", lambda _: None)
    monkeypatch.setattr(sweep, "metadata", lambda instance: summary(None, 500))
    said = []
    rows = sweep.run(out, budget=1.4, parallel=1, agent_env=fake_agent_env(tmp_path), echo=said.append)
    assert len(rows) == 2 and rows[0]["cost_usd"] is None   # 0.5 + 0.5 fits in 1.4, 1.0 + 0.5 does not
    assert "no spend recorded, counted as its cap $0.50 (spent $0.50)" in said[1]
    assert "2 games, $1.00 of model spend (2 with no spend recorded, counted at the cap)." in sweep.report(out)


def test_a_player_slot_named_in_player_slots_is_found_by_its_agent(generated, monkeypatch):
    out, _ = generated
    found = summary(0.2, 500)
    players = found["rts_summary"]["wc3"]["player_slots"]
    players[:] = [{**players[1], "player_kind": "agent", "player_name": "rival", "spend": {"cost_usd": 9.0}},
                  {**players[0], "player_name": "wc3"}]
    monkeypatch.setattr(sweep, "metadata", lambda instance: found)
    row = sweep.outcome("t", {}, json.loads((out / "sweep.json").read_text())["agent"], "i", 1.0, 0)
    assert (row["cost_usd"], row["score"], row["opponent_score"]) == (0.2, 500, 1000)


def test_the_report_ranks_the_models_and_shows_each_seed(generated, tmp_path, monkeypatch):
    out, names = generated
    monkeypatch.setattr(sweep.time, "sleep", lambda _: None)
    monkeypatch.setattr(sweep, "metadata",
                        lambda instance: summary(0.1, 3000 if "gpt" in instance else 500)
                        | {"verifications": {"wc3": {"score": 0.6 if "gpt" in instance else 0.2}}})
    sweep.run(out, budget=10.0, parallel=4, agent_env=fake_agent_env(tmp_path), echo=lambda _: None)
    text = sweep.report(out)
    assert "8 games, $0.80 of model spend" in text
    ranking = [line.split(" | ")[0] for line in text.splitlines() if line.startswith("| openai") or
               line.startswith("| anthropic")]
    assert ranking[:2] == ["| openai/gpt-5.4-mini", "| anthropic/claude-haiku-4-5"]
    assert "| openai/gpt-5.4-mini | 4 | 0.60 ± 0.00 | 0 / 4 / 0 | 0 | 75% | 3.0k vs 1.0k | $0.100 | 0 | 30 |" in text
    assert "| openai/gpt-5.4-mini | 0-4-0 | none |" in text
    assert "| anthropic/claude-haiku-4-5 | 0.20 0.20 | 0.20 0.20 | 0.00 |" in text


def test_a_game_without_a_grade_is_void_and_an_agent_that_failed_keeps_its_outcome(generated, monkeypatch):
    out, _ = generated
    void = {**summary(0.1, 500), "verifications": {}, "failed_steps": [
        {"step_id": "grade", "error": "wc3: no outcome to grade, the match was cancelled", "is_fatal": True}]}
    monkeypatch.setattr(sweep, "metadata", lambda instance: void)
    row = sweep.outcome("void", {}, None, "i", 1.0, 1)
    assert (row["grade"], row["outcome"]) == (None, None)
    assert row["void"] == "wc3: no outcome to grade, the match was cancelled"
    crashed = {**summary(0.1, 500), "failed_steps": [{"step_id": "play", "error": "agent died", "is_fatal": False}]}
    monkeypatch.setattr(sweep, "metadata", lambda instance: crashed)
    row = sweep.outcome("crashed", {}, None, "i", 1.0, 0)
    assert (row["grade"], row["outcome"], row["void"], row["agent_error"]) == (0.4, "draw", None, "agent died")
    assert sweep.outcome("lost", {}, None, None, None, 1)["void"] == "no instance recorded"


LADDER = """
name = "ladder"
template = "vs-ai-quick"
models = ["m1", "m2"]
opponents = [
  {computer = "easy", race = "orc"}, {computer = "normal", race = "orc"}, {computer = "insane", race = "orc"},
]
seeds = [1, 2]
time_limit_seconds = 1800
max_cost_usd = 0.5
"""


def test_the_report_gives_each_ai_levels_won_drawn_lost_and_the_highest_level_beaten(tmp_path, monkeypatch):
    (tmp_path / "spec.toml").write_text(LADDER)
    out = tmp_path / "sweep"
    sweep.generate(sweep.Spec.load(tmp_path / "spec.toml"), out)
    manifest = json.loads((out / "sweep.json").read_text())
    assert manifest["player_step"] == "play" and len(manifest["tasks"]) == 12

    def game(instance):
        task = instance.rpartition("/")[2]
        level, model = manifest["tasks"][task]["opponent"]["computer"], manifest["tasks"][task]["model"]
        result = {"easy": "victory", "normal": "victory" if model == "m1" else "defeat"}.get(level, "time_limit")
        if level == "insane" and task.endswith("s2") and model == "m1":
            return {**summary(0.1, 500), "verifications": {}, "failed_steps": [
                {"step_id": "grade", "error": "the game engine failed", "is_fatal": True}]}
        found = summary(0.1, 500)
        found["rts_summary"]["wc3"]["player_slots"][0]["result"] = result
        return found | {"verifications": {"wc3": {"score": {"victory": 1.0, "defeat": 0.0}.get(result, 0.5)}}}

    monkeypatch.setattr(sweep.time, "sleep", lambda _: None)
    monkeypatch.setattr(sweep, "metadata", game)
    sweep.run(out, budget=10.0, parallel=4, agent_env=fake_agent_env(tmp_path), echo=lambda _: None)
    text = sweep.report(out)
    assert "| m1 | 6 | 0.90 ± 0.22 | 4 / 1 / 0 | 1 |" in text and "| m2 | 6 | 0.50 ± 0.45 | 2 / 2 / 2 | 0 |" in text
    assert "| Model | easy | normal | insane | Highest level beaten |" in text
    assert "| m1 | 2-0-0 | 2-0-0 | 0-1-0 | normal (2 of 2 won) |" in text
    assert "| m2 | 2-0-0 | 0-0-2 | 0-2-0 | easy (2 of 2 won) |" in text
    assert "Void games, left out of the points: ladder-m1-vs-insane-orc-s2 (the game engine failed)" in text



DRILLS = """
name = "drills"
template = "drill-*"
models = ["m1", "m2"]
prompt = ""
max_cost_usd = 0.5
"""


def test_a_drills_sweep_keeps_each_drills_own_race_board_and_prompt(tmp_path):
    (tmp_path / "spec.toml").write_text(DRILLS)
    spec = sweep.Spec.load(tmp_path / "spec.toml")
    names = sweep.generate(spec, tmp_path / "sweep")
    assert len(names) == 50 and names[:2] == ["drills-build-production-m1", "drills-build-production-m2"]
    manifest = json.loads((tmp_path / "sweep" / "sweep.json").read_text())
    elf = manifest["tasks"]["drills-build-supply-nightelf-m2"]
    assert (elf["template"], elf["race"], elf["opponent"]) == ("drill-build-supply-nightelf", "night_elf",
                                                               {"computer": "easy", "race": "orc"})
    assert manifest["tasks"]["drills-fight-even-m1"]["opponent"] is None   # a scripted opponent, no computer
    original = json.loads((sweep.TASKS / "drill-build-supply-nightelf.json").read_text())
    made = json.loads((tmp_path / "sweep/tasks/drills-build-supply-nightelf-m2.json").read_text())
    by_id = {s["id"]: s for s in made}
    assert by_id["play"]["prompt"] == next(s for s in original if s["id"] == "play")["prompt"]
    assert by_id["play"]["model"] == "m2" and by_id["agent"]["env_vars"] == {"WC3_MAX_COST_USD": "0.5"}
    assert by_id["stage"] == next(s for s in original if s["id"] == "stage")
    (tmp_path / "prompted.toml").write_text(DRILLS.replace('prompt = ""', ""))
    with pytest.raises(ValueError, match="drills and duels keep their own prompts"):
        sweep.Spec.load(tmp_path / "prompted.toml")
    (tmp_path / "duels.toml").write_text(DRILLS.replace("drill-*", "mirror-*").replace('prompt = ""', ""))
    with pytest.raises(ValueError, match="drills and duels keep their own prompts"):
        sweep.Spec.load(tmp_path / "duels.toml")
    (tmp_path / "none.toml").write_text(DRILLS.replace("drill-*", "nothing-*"))
    with pytest.raises(ValueError, match="no bundled task matches"):
        sweep.Spec.load(tmp_path / "none.toml")


def test_the_report_gives_drills_by_model_and_skill(tmp_path, monkeypatch):
    (tmp_path / "spec.toml").write_text(DRILLS.replace('"drill-*"', '"drill-[cf]*"'))
    out = tmp_path / "sweep"
    sweep.generate(sweep.Spec.load(tmp_path / "spec.toml"), out)

    def drill(instance):
        found = summary(0.05, 500)
        met = 1.0 if "m1" in instance or "creep" in instance else 0.5
        return found | {"verifications": {"drill:wc3": {"score": met}}}

    monkeypatch.setattr(sweep.time, "sleep", lambda _: None)
    monkeypatch.setattr(sweep, "metadata", drill)
    sweep.run(out, budget=10.0, parallel=4, agent_env=fake_agent_env(tmp_path), echo=lambda _: None)
    text = sweep.report(out)
    assert "| m1 | 5 | 5 of 5, 100% | 0 | $0.050 | 0 | 30 |" in text and "| m2 | 5 | 2 of 5, 70% | 0 |" in text
    assert "| Model | combat | creeping | full-game |" in text
    assert "| m2 | 0 of 2, 50% | 2 of 2, 100% | 0 of 1, 50% |" in text
    assert "| creep-hard | creeping | 1.00 | 1.00 |" in text and "Won-drawn-lost" not in text
