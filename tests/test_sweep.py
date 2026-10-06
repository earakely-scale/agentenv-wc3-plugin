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
    assert (by_id["match"]["map"], by_id["match"]["seed"], by_id["match"]["race"]) == ("(2)TerenasStand.w3x", 2, "orc")
    play = by_id["play"]
    assert play["model"] == "openai/gpt-5.4-mini" and play["prompt_id"] == "first-eval-gpt-5.4-mini-terenasstand-s2"
    assert "as Orc on Terenas Stand, a 2-player map" in play["prompt"] and '{"type_id": "Peon"}' in play["prompt"]
    assert "after 5 minutes" in play["prompt"]
    assert by_id["agent"]["env_vars"] == {"WC3_MAX_COST_USD": "0.5"}
    manifest = json.loads((out / "sweep.json").read_text())
    assert manifest["tasks"]["first-eval-gpt-5.4-mini-terenasstand-s2"]["seed"] == 2


def test_a_template_with_seats_gets_the_race_and_the_opponent_on_its_seats(tmp_path):
    template = [{"id": "match", "type": "wc3_match", "seats": [{"agent": "wc3", "race": "human"},
                                                                {"computer": "easy", "race": "orc"}]},
                {"id": "agent", "type": "deploy_agent", "agent_name": "wc3"},
                {"id": "play", "type": "prompt_agent", "agent_name": "wc3", "prompt": "x"}]
    (tmp_path / "t.json").write_text(json.dumps(template))
    spec = sweep.Spec(name="s", template=str(tmp_path / "t.json"), models=["m"], races=["undead"],
                      opponents=[{"computer": "insane", "race": "night_elf"}], prompt=None)
    steps = sweep.task_of(spec, spec.steps(), spec.combinations()[0], "s")
    assert steps[0]["seats"] == [{"agent": "wc3", "race": "undead"}, {"computer": "insane", "race": "night_elf"}]
    assert steps[2]["prompt"] == "x"


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
    """A shorthand match's summary, as the real game's: the agent's seat has no agent name."""
    spend = {} if cost is None else {"cost_usd": cost, "decisions": 30, "tokens": 9000}
    return {"verifications": {"wc3": {"score": 0.4}}, "rts_summary": {"wc3": {"game_time_seconds": 300, "seats": [
        {"slot": 0, "agent": None, "computer": None, "team": 1, "result": "time_limit", "orders_sent": 40,
         "spend": spend, "metrics": {"total": score, "units_killed": 3}},
        {"slot": 1, "agent": None, "computer": "easy", "team": 2, "result": "time_limit", "orders_sent": 0,
         "spend": {}, "metrics": {"total": 1000}}]}}}


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
    assert (rows[0]["result"], rows[0]["orders"], rows[0]["decisions"]) == ("time_limit", 40, 30)
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


def test_a_seat_named_in_seats_is_found_by_its_agent(generated, monkeypatch):
    out, _ = generated
    found = summary(0.2, 500)
    seats = found["rts_summary"]["wc3"]["seats"]
    seats[:] = [{**seats[1], "computer": None, "agent": "rival", "spend": {"cost_usd": 9.0}},
                {**seats[0], "agent": "wc3"}]
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
    assert "| openai/gpt-5.4-mini | 4 | 0.60 ± 0.00 | 0 / 4 / 0 | 3.0k vs 1.0k | 75% | $0.100 | 0 | 30 |" in text
    assert "| anthropic/claude-haiku-4-5 | 0.20 0.20 | 0.20 0.20 | 0.00 |" in text
