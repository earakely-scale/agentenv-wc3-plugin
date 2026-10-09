"""The Hub dataset: the v1 bundles, how a sweep's run is filed under its v1 task, WC3's rows, the card and the check
before anything leaves the machine."""

import importlib.util
import io
import json
import re
import types
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from agent_env.bundle.parse import BundleKind, parse_bundle
from huggingface_hub import DatasetCard, SpaceCard
from PIL import Image, ImageDraw

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
    card = hub.CARD.read_text()
    pins = re.findall(r"agentenv-wc3-plugin(?:@|/tree/|/blob/)(v[\d.]+)", card)
    pins += re.findall(r"wc3env-AgentEnv/resolve/(v[\d.]+)/", card) + re.findall(r'rev = "[^"]+", "(v[\d.]+)"', card)
    assert len(pins) > 5 and set(pins) == {f"v{hub.VERSION}"}
    for text in (card, (hub.ROOT / "README.md").read_text()):
        assert set(re.findall(r"wc3env-AgentEnv@(v[\d.]+)", text)) == {f"v{hub.VERSION}"}


def test_the_check_stops_a_path_or_host_of_this_machine_in_any_file(hub):
    hub.check({"runs/x/report.md": b"ladder: 36 games", "bundles/b/README.md": b"~/runs/ladder"})
    with pytest.raises(ValueError, match="raw/b.jsonl"):
        hub.check({"raw/b.jsonl": b'{"cwd": "/home/ubuntu/runs"}'})
    sink = io.BytesIO()
    pq.write_table(pa.Table.from_pylist([{"error": "connect to proxy.example.scale.com failed"}]), sink)
    with pytest.raises(ValueError, match="scale.com"):
        hub.check({"episodes/x.parquet": sink.getvalue()})


def test_a_runs_files_are_kept_under_its_episode_id_and_its_row_names_them(hub, monkeypatch):
    saved = [{"kind": kind, "name": name, "artifact_id": name, "version": 1} for kind, name in (
        ("html_replay", "wc3-g-1.html"), ("timeline", "wc3-g-1-timeline.json"), ("map_video", "wc3-g-1.mp4"),
        ("replay", "wc3-g-1.w3g"), ("replay", "wc3-g-1.w3g.json"),
        ("agent_files", "wc3-g-1-p0-wc3-llm-transcript.json"))]
    record = types.SimpleNamespace(context={"metadata": {"match_files": {"files": saved}}})
    monkeypatch.setattr(hub, "find_task_instance", lambda instance: record)
    monkeypatch.setattr(hub.FileArtifact, "get", lambda artifact_id, version: types.SimpleNamespace(
        load=lambda: artifact_id.encode()))
    files, columns = hub.run_files("@local/~/runs/x/t-abc", "wc3-v1-duels/t-abc")
    assert files == {"replays/wc3-v1-duels/t-abc.html": b"wc3-g-1.html",
                     "timelines/wc3-v1-duels/t-abc.json": b"wc3-g-1-timeline.json",
                     "w3g/wc3-v1-duels/t-abc.w3g": b"wc3-g-1.w3g",
                     "w3g/wc3-v1-duels/t-abc.w3g.json": b"wc3-g-1.w3g.json",
                     "transcripts/wc3-v1-duels/t-abc.json": b"wc3-g-1-p0-wc3-llm-transcript.json"}
    assert columns == {"replay": "replays/wc3-v1-duels/t-abc.html", "timeline": "timelines/wc3-v1-duels/t-abc.json",
                       "w3g": "w3g/wc3-v1-duels/t-abc.w3g", "transcript": "transcripts/wc3-v1-duels/t-abc.json"}


def write_parquet(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), path)


def test_the_space_lists_every_run_in_order_and_loads_replays_from_the_dataset_tag(hub, tmp_path, monkeypatch):
    data = tmp_path / "dataset"
    files = {"replay": "replays/x.html", "timeline": "timelines/x.json", "w3g": "w3g/x.w3g", "transcript": None}
    base = {"outcome": "loss", "reward": 0.0, "score_share": 0.3, "game_seconds": 60.0, "turns": 10, "cost_usd": 0.1,
            "orders": 9, "orders_refused": 1, "units_killed": 0, **files}
    ladder = {"map": "Echo Isles", "race": "human", "seed": 1, "opponent_race": "orc"}
    write_parquet(data / "tasks/ladder.parquet", [{"task": "ladder-insane", "opponent_ai": "insane", **ladder},
                                                   {"task": "ladder-easy", "opponent_ai": "easy", **ladder}])
    write_parquet(data / "episodes/ladder.parquet", [
        {"episode_id": "l/1", "task": "ladder-insane", "model": "openai/gpt-5.4-mini", **base},
        {"episode_id": "l/2", "task": "ladder-easy", "model": "anthropic/claude-haiku-4-5", **base}])
    write_parquet(data / "tasks/drills.parquet", [{"task": "drill-opening", "map": "Echo Isles", "race": "human",
                                                   "seed": 1, "opponent_ai": None, "opponent_race": None,
                                                   "opponent_script": "attack"}])
    write_parquet(data / "episodes/drills.parquet", [
        {"episode_id": "d/1", "task": "drill-opening", "model": "openai/gpt-5.4-mini", "skill": "economy",
         "checks_met": 1, "num_checks": 2, "passed": False,
         "checks": json.dumps([{"check": "workers >= 11", "met": True, "measured": 14.0}]), **base}])
    write_parquet(data / "tasks/duels.parquet", [{"task": "mirror-orc-s1", "map": "Echo Isles", "race": "orc",
                                                  "seed": 1}])
    write_parquet(data / "episodes/duels.parquet", [{"episode_id": "u/1", "task": "mirror-orc-s1",
                                                     "model": "fireworks_ai/deepseek-v4p1-flash", **base}])
    write_parquet(data / "references/duels.parquet", [{"episode_id": "duels-references/r1", "task": None,
                                                       "race": "orc", "seed": 5, **base}])
    (data / "assets").mkdir()
    Image.new("RGB", (8, 8)).save(data / "assets/thumbnail.jpg")
    monkeypatch.setattr(hub, "FEATURED", (("u/1", "a duel"),))
    hub.space(data, tmp_path / "space", "v9.9.9")
    listed = json.loads((tmp_path / "space/runs.json").read_text())
    assert (listed["dataset"], listed["revision"]) == (hub.REPO, "v9.9.9")
    assert listed["featured"] == [{"id": "u/1", "why": "a duel"}]
    runs = listed["runs"]
    assert [r["id"] for r in runs] == ["l/2", "l/1", "d/1", "u/1", "duels-references/r1"]
    assert (runs[0]["model"], runs[0]["opponent"]) == ("Claude Haiku 4.5", "the easy Orc AI")
    assert (runs[2]["opponent"], runs[2]["checks"][0]["met"]) == ("a scripted attacker", True)
    assert runs[2]["replay"] == "replays/x.html"
    assert (runs[4]["model"], runs[4]["task"]) == (hub.SCRIPTED, "mirror-orc-s5")
    assert {p.name for p in (tmp_path / "space").iterdir()} >= {"index.html", "app.js", "app.css", "README.md",
                                                                "runs.json", "thumbnail.jpg"}
    monkeypatch.setattr(hub, "FEATURED", (("gone/1", "missing"),))
    with pytest.raises(ValueError, match="gone/1"):
        hub.space(data, tmp_path / "space", "v9.9.9")


def test_the_space_is_a_static_page_of_the_dataset(hub):
    data = SpaceCard((hub.SPACE / "README.md").read_text()).data
    assert (data.sdk, data.datasets) == ("static", [hub.REPO])
    page = (hub.SPACE / "index.html").read_text()
    assert 'src="app.js"' in page and 'href="app.css"' in page
    assert "resolve/${DATA.revision}/" in (hub.SPACE / "app.js").read_text()


def test_the_clip_is_cut_to_where_the_game_moves_under_its_title_bar(hub):
    frames = []
    for i in range(10):
        frame = Image.new("RGB", (960, hub.BAR + 600), (40, 70, 40))
        ImageDraw.Draw(frame).rectangle((0, 0, 960, hub.BAR), fill=(20, 20, 20))
        ImageDraw.Draw(frame).ellipse((700 + i * 10, hub.BAR + 100, 710 + i * 10, hub.BAR + 110), fill=(255, 0, 0))
        frames.append(frame)
    left, top, right, bottom = hub.action(frames)
    assert left <= 700 and right >= 800 and top <= 100 <= bottom
    assert abs((right - left) * 9 - (bottom - top) * 16) <= 16 and right <= 960 and bottom <= 600
    shot = hub.shot(frames[5], (left, top, right, bottom))
    assert shot.size == (800, (hub.BAR + 540) * 800 // 960)
