"""The Hub dataset: the v1 bundles, how a sweep's run is filed under its v1 task, WC3's rows, the card and the check
before anything leaves the machine."""

import importlib.util
import io
import json
import re
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from agent_env.bundle.parse import BundleKind, parse_bundle
from huggingface_hub import DatasetCard

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "hub_dataset.py"
DRILL = {"drill:wc3": {"score": 0.5, "results": [
    {"name": "units_trained", "criterion": "units_trained >= 8", "result": True, "weight": 1,
     "evidence": "measured 15"},
    {"name": "idle_worker_seconds", "criterion": "idle_worker_seconds <= 30", "result": False, "weight": 1,
     "evidence": "measured 80.0"},
    {"name": "game_ran", "criterion": "the game kept running", "result": True, "weight": -100},
    {"name": "finish", "criterion": "how the match ended (information only)", "result": True, "weight": 0}]}}
DUEL = {"duel:wc3": {"score": 0.0, "results": [
    {"name": "outcome", "criterion": "the outcome", "result": False, "score": 0.0, "weight": 1},
    {"name": "army_kept_percent", "criterion": "army_kept_percent", "result": None, "weight": 0,
     "evidence": "measured 26"},
    {"name": "enemy_army_destroyed_percent", "criterion": "enemy_army_destroyed_percent", "result": None,
     "weight": 0, "evidence": "measured 32"}]}}


@pytest.fixture(scope="module")
def hub():
    spec = importlib.util.spec_from_file_location("hub_dataset", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def row(**axes) -> dict:
    return {"model": "openai/gpt-5.4-mini", "map": "(2)EchoIsles.w3x", "race": "human", "opponent": None, "seed": 1,
            "outcome": "loss", "result": "defeat", "grade": 0.0, "score": 100, "opponent_score": 300,
            "units_killed": 2, "orders": 40, "refused": 3, "decisions": 20, "game_seconds": 60.0, "cost_usd": 0.1,
            **axes}


def test_the_v1_bundles_are_the_sweeps_tasks_without_the_model(hub, tmp_path):
    expected = {"wc3-v1-drills": (25, 7, 0.5), "wc3-v1-ladder": (12, 3, 2.0), "wc3-v1-duels": (16, 1, 0.5)}
    for v1 in hub.BUNDLES:
        combos = hub.write_bundle(v1, tmp_path / v1.bundle)
        bundle = parse_bundle(tmp_path / v1.bundle)
        tasks, cap = [e for e in bundle.entries if e.kind == BundleKind.TASK], expected[v1.bundle][2]
        assert (len(combos), len([e for e in bundle.entries if e.kind == BundleKind.EVAL])) == expected[v1.bundle][:2]
        assert sorted(e.name for e in tasks) == sorted(combos)
        for name in combos:
            steps = json.loads((tmp_path / v1.bundle / "tasks" / f"{name}.json").read_text())
            play = next(s for s in steps if s["id"] == "play")
            agent = next(s for s in steps if s["type"] == "deploy_agent" and s["agent_name"] == "wc3")
            assert "model" not in play and play["prompt_id"] == name
            assert agent["env_vars"]["WC3_MAX_COST_USD"] == str(cap)
    assert {"drill-opening", "ladder-terenasstand-vs-insane-orc-s2", "mirror-nightelf-s4"} <= {
        p.stem for p in tmp_path.glob("*/tasks/*.json")}


def test_a_sweeps_run_is_filed_under_its_v1_task_by_its_axes(hub):
    drills, ladder, duels = (hub.spec(v1) for v1 in hub.BUNDLES)
    assert hub.v1_task(drills, row(template="drill-fight-even")) == "drill-fight-even"
    frontier = row(template="vs-ai", model="anthropic/claude-opus-5-5", seed=2,
                   opponent={"computer": "normal", "race": "orc"})
    assert hub.v1_task(ladder, frontier) == "ladder-echoisles-vs-normal-orc-s2"
    assert hub.v1_task(duels, row(template="mirror-orc", race="orc", seed=3)) == "mirror-orc-s3"
    assert hub.v1_task(duels, row(template="mirror-orc-baseline", race="orc", seed=3)) == "mirror-orc-s3"


def test_wc3_rows_carry_each_check_the_duels_strengths_and_the_references_task(hub):
    drills, ladder, duels = hub.BUNDLES
    drill = hub.episode_row(drills, "wc3-v1-drills/x", "drill-opening", "drills-final", row(), DRILL)
    assert (drill["skill"], drill["checks_met"], drill["num_checks"], drill["passed"]) == ("economy", 1, 2, False)
    assert json.loads(drill["checks"])[1] == {"check": "idle_worker_seconds <= 30", "met": False, "measured": 80.0}
    assert (drill["score_share"], drill["orders_refused"]) == (0.25, 3)
    duel = hub.episode_row(duels, "wc3-v1-duels/x", "mirror-human-s1", "duels-final", row(), DUEL)
    assert (duel["army_kept_percent"], duel["enemy_army_destroyed_percent"]) == (26.0, 32.0)
    tasks = {"mirror-human-s1"}
    assert hub.reference_row(hub.spec(duels), tasks, row(template="mirror-human-baseline"))["task"] == "mirror-human-s1"
    assert hub.reference_row(hub.spec(duels), tasks, row(template="mirror-human-baseline", seed=5))["task"] is None


def test_the_card_pins_the_plugin_and_lists_every_config_with_one_default(hub):
    plugin = hub.PLUGIN.format(ref=f"v{hub.VERSION}")
    data = DatasetCard(hub.card_text(plugin)).data
    names = [c["config_name"] for c in data.configs]
    assert [c["config_name"] for c in data.configs if c.get("default")] == ["ladder_tasks"]
    assert {f"{v1.bundle}_{t}" for v1 in hub.BUNDLES for t in ("tasks", "episodes")} <= set(names)
    assert {f"{f}_{t}" for f in ("ladder", "drills", "duels") for t in ("tasks", "episodes")} <= set(names)
    assert data.agentenv["default"] == "wc3-v1-drills"
    assert data.agentenv["bundles"] == {v1.bundle: {"plugins": [plugin], "setup": hub.SETUP} for v1 in hub.BUNDLES}
    pins = re.findall(r"agentenv-wc3-plugin(?:@|/tree/|/blob/)(v[\d.]+)", hub.CARD.read_text())
    assert pins and set(pins) == {f"v{hub.VERSION}"}
    for text in (hub.CARD.read_text(), (hub.ROOT / "README.md").read_text()):
        assert set(re.findall(r"wc3env-AgentEnv@(v[\d.]+)", text)) == {f"v{hub.VERSION}"}


def test_the_check_stops_a_path_or_host_of_this_machine_in_any_file(hub):
    hub.check({"runs/x/report.md": b"ladder: 36 games", "bundles/b/README.md": b"~/runs/ladder"})
    with pytest.raises(ValueError, match="raw/b.jsonl"):
        hub.check({"raw/b.jsonl": b'{"cwd": "/home/ubuntu/runs"}'})
    sink = io.BytesIO()
    pq.write_table(pa.Table.from_pylist([{"error": "connect to proxy.example.scale.com failed"}]), sink)
    with pytest.raises(ValueError, match="scale.com"):
        hub.check({"episodes/x.parquet": sink.getvalue()})
