"""The RTS pieces (agentenv_rts) on the WC3 env served over HTTP, on wc3env's fake game: a program playing through
the urn:rts session, the spectator's live data and page, the recording and its task step, the game's own picture
(display capture, camera), and the choice-question transport any chat model answers in Jev's shape."""

import asyncio
import base64
import http.server
import json
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request

import pytest
from agent_env.artifact import FileArtifact
from agent_env.env.env import DeployedEnv
from agent_env.task_step.context import DeployedAgent, TaskStepContext
from agentenv_protocol import client
from agentenv_protocol.types import WELL_KNOWN_PATH
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from test_steps import deployed, run_context

from agentenv_rts import choices, display, highlights, recording
from agentenv_rts.grade import RTSGradeTaskStep
from agentenv_rts.seats import Lockstep
from agentenv_rts.session import RemoteSession, SessionError
from agentenv_rts.steps import RTSFinishTaskStep, RTSSeatAgentsTaskStep, SaveRTSRecordingTaskStep, seat_agents
from agentenv_rts.timeline import Timeline, model_name
from agentenv_wc3 import frames, metrics, render
from agentenv_wc3.server import WC3Env, check_scenario, seats_of
from agentenv_wc3.steps import WC3MatchTaskStep

pytestmark = pytest.mark.anyio


async def match(record, **options):
    step = WC3MatchTaskStep(id="match", version=None, env_id="wc3", seed=2, time_limit_seconds=60, **options)
    return await step.execute(run_context(record))


def base(record) -> str:
    return record.mcp_url.removesuffix("/mcp")


async def test_a_program_plays_the_game_through_the_rts_session(env_vars):
    async with deployed(WC3Env()) as record:
        await match(record)
        session = RemoteSession(base(record))
        state = await asyncio.to_thread(session.observe)
        assert set(state["observations"]) == {0, 1} and state["done"] is False
        assert state["scenario"]["time_limit_seconds"] == 60 and state["setup"]["map"] == "(2)EchoIsles.w3x"
        me = state["observations"][0]
        peasant = next(u for u in me["units"] if u["type_id"] == "hpea")
        enemy = me["visible_enemies"][0]
        actions = [{"unit_id": enemy["unit_id"], "command": "move", "arguments": {"x": 0, "y": 0}},
                   {"unit_id": peasant["unit_id"], "command": "move", "arguments": {"x": peasant["x"] + 400,
                                                                                  "y": peasant["y"]}}]
        step = await asyncio.to_thread(session.step, {0: actions})
        assert step["observations"][0]["game_time_seconds"] == 1.0
        assert [r["index"] for r in step["rejected"][0]] == [0]   # not ours: dropped, by its index in the batch
        assert step["rejected"][0][0]["reason"].startswith("dropped: ")
        moved = next(u for u in step["observations"][0]["units"] if u["unit_id"] == peasant["unit_id"])
        assert moved["x"] > peasant["x"]
        step = await asyncio.to_thread(session.step, {0: []}, 60000)
        assert step["done"] and step["result"] == "time_limit"
        assert step["observations"][0]["game_time_seconds"] == 60.0   # a step never runs past the time limit
        assert (await asyncio.to_thread(session.step, {0: []}))["elapsed_ms"] == 0
        summary = (await client.get_data(base(record))).parts[0].data
        assert summary["harness"]["session_steps"] == 2 and summary["harness"]["orders_sent"] == 1
        assert summary["harness"]["orders_rejected"] == 1 and summary["mode"] == "stepping"


async def test_debug_ops_that_stage_the_game_need_the_match_to_allow_them(env_vars):
    async with deployed(WC3Env()) as record:
        await match(record)
        session = RemoteSession(base(record))
        assert (await asyncio.to_thread(session.debug, "speed", factor=1.0))["ignored"] is True
        with pytest.raises(SessionError, match="must allow it"):
            await asyncio.to_thread(session.debug, "spawn", type_id="hfoo")
        await match(record, allow_debug=True)
        assert (await asyncio.to_thread(session.debug, "end", result="victory"))["ended"] is True


async def test_spectators_follow_the_game_live(env_vars):
    async with deployed(WC3Env()) as record:
        await match(record)
        page = await asyncio.to_thread(lambda: urllib.request.urlopen(base(record) + "/live").read().decode())
        assert "window.RTS_DATA = " not in page and "function draw()" in page   # live: nothing embedded
        session = RemoteSession(base(record))
        for _ in range(3):
            await asyncio.to_thread(session.step, {0: []})
        doc = await asyncio.to_thread(lambda: json.load(urllib.request.urlopen(base(record) + "/live/data.json")))
        static = doc["static"]
        assert static["bounds"]["max_x"] > static["bounds"]["min_x"]
        assert static["title"] == "Warcraft III · (2)EchoIsles"
        assert [p["controller"] for p in static["players"]] == ["agent", "the game's AI"]
        assert static["terrain"]["width"] * static["terrain"]["height"] == len(
            base64.b64decode(static["terrain"]["codes"]))
        assert any(p["kind"] == "gold" for p in static["points"]) and static["trees"]
        assert [f["t"] for f in doc["frames"]] == [0.0, 1.0, 2.0, 3.0]
        unit = doc["frames"][-1]["units"][0]
        assert len(unit) == 7 and doc["frames"][-1]["players"]["0"]["gold"] is not None
        later = await asyncio.to_thread(
            lambda: json.load(urllib.request.urlopen(base(record) + "/live/data.json?since=2")))
        assert [f["t"] for f in later["frames"]] == [3.0] and later["live"]["over"] is False


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="the MP4 needs ffmpeg")
async def test_the_recording_is_saved_as_file_artifacts(env_vars, local_stores):
    async with deployed(WC3Env()) as record:
        await match(record)
        session = RemoteSession(base(record))
        for _ in range(4):
            await asyncio.to_thread(session.step, {0: []})
        context = await SaveRTSRecordingTaskStep(id="recording", version=None, env_id="wc3").execute(
            run_context(record))
    saved = context.metadata["recordings"]["recording"]
    assert sorted(f["name"].rpartition(".")[2] for f in saved) == ["html", "json", "mp4"]
    load = {f["name"].rpartition(".")[2]: FileArtifact.get(f["artifact_id"], f["version"]).load() for f in saved}
    assert load["mp4"][4:8] == b"ftyp"
    html = load["html"]
    assert b"window.RTS_DATA = {" in html and b"</script>" in html
    timeline = json.loads(load["json"])
    assert timeline["static"]["game"] and len(timeline["frames"]) == 5 and timeline["frames"][-1]["t"] == 4.0


async def test_each_file_of_the_recording_is_its_own_artifact(env_vars, local_stores, monkeypatch):
    names = ["g-client.mp4", "g-highlights.mp4", "g.mp4"]

    async def invoke(url, card, uri, params, timeout=None):
        return {"files": [{"name": n, "content_type": "video/mp4", "base64": base64.b64encode(n.encode()).decode()}
                          for n in names]}

    monkeypatch.setattr(client, "invoke_extension", invoke)
    async with deployed(WC3Env()) as record:
        context = await SaveRTSRecordingTaskStep(id="recording", version=None, env_id="wc3",
                                                 formats=["client", "highlights", "mp4"]).execute(run_context(record))
    saved = context.metadata["recordings"]["recording"]
    assert [FileArtifact.get(f["artifact_id"], f["version"]).load() for f in saved] == [n.encode() for n in names]
    assert len({f["artifact_id"] for f in saved}) == 3 and {f["version"] for f in saved} == {1}


async def test_client_view_on_the_fake_game_leaves_the_map_alone(env_vars, local_stores):
    async with deployed(WC3Env()) as record:
        await match(record, client_view=True)
        doc = await asyncio.to_thread(lambda: json.load(urllib.request.urlopen(base(record) + "/live/data.json")))
        assert doc["live"]["client"] is False and doc["frames"]
        with pytest.raises(urllib.error.HTTPError, match="404"):
            await asyncio.to_thread(urllib.request.urlopen, base(record) + "/live/client.jpg")
        assert (await client.get_data(base(record))).parts[0].data["client_view"] is True
        context = await SaveRTSRecordingTaskStep(id="recording", version=None, env_id="wc3",
                                                 formats=["client"]).execute(run_context(record))
    assert context.metadata["recordings"]["recording"] == []


async def test_players_tell_spectators_their_names_plans_and_costs(env_vars):
    async with deployed(WC3Env()) as record:
        await match(record, labels={"1": "Orc AI (normal)"})
        session = RemoteSession(base(record))
        await asyncio.to_thread(session.note, "player", "Claude Sonnet 5.5 + Haiku 4.5")
        await asyncio.to_thread(session.note, "plan", "Mine gold, then a Barracks.")
        await asyncio.to_thread(session.note, "stats", data={"cost_usd": 0.42, "decisions": 12, "junk": "x"})
        await asyncio.to_thread(session.step, {0: []})
        with pytest.raises(SessionError, match="kind must be"):
            await asyncio.to_thread(session.note, "gossip", "hi")
        doc = await asyncio.to_thread(lambda: json.load(urllib.request.urlopen(base(record) + "/live/data.json")))
        cast = await asyncio.to_thread(lambda: json.load(urllib.request.urlopen(base(record) + "/live/casting.json")))
        state = await asyncio.to_thread(lambda: json.load(urllib.request.urlopen(base(record) + "/live/state.json")))
    assert [p["label"] for p in doc["static"]["players"]] == ["Claude Sonnet 5.5 + Haiku 4.5", "Orc AI (normal)"]
    last = doc["frames"][-1]
    assert last["notes"] == [{"slot": 0, "kind": "plan", "text": "Mine gold, then a Barracks."}]
    assert last["players"]["0"]["agent"] == {"cost_usd": 0.42, "decisions": 12}
    assert cast["players"][0]["label"] == "Claude Sonnet 5.5 + Haiku 4.5" and "Warcraft III" in cast["desk"]
    assert cast["notes"][0]["text"] == "Mine gold, then a Barracks." and cast["clock"]["limit"] == 60
    assert cast["history"] and state == {"game_over": False, "result": "", "t": 1.0, "client": False}


REF = render.Reference({"units": {"hfoo": {"name": "Footman", "gold": 135, "lumber": 0},
                                  "ogru": {"name": "Grunt", "gold": 200, "lumber": 0},
                                  "Hamg": {"name": "Archmage", "gold": 425, "lumber": 100, "hero": True},
                                  "htow": {"name": "Town Hall", "structure": True}}})


def unit(uid, type_id, x, y=0, owner=0, **more):
    return {"unit_id": uid, "type_id": type_id, "hp": 400, "x": x, "y": y, "owner": owner, **more}


def test_the_feed_names_fights_and_the_moments_that_matter():
    static = {"players": [{"slot": 0, "label": "Claude"}, {"slot": 1, "label": "Orc AI"}], "points": [],
              "bounds": {"min_x": -8000, "max_x": 8000, "min_y": -8000, "max_y": 8000}}
    feed = frames.Feed(REF, static)
    hall = unit(1, "htow", -6000, owner=0, structure=True)
    footman, grunt = unit(2, "hfoo", 0), unit(9, "ogru", 100, owner=1)
    blows = [{"kind": "attacked", "unit_id": 9, "attacker_id": 2}, {"kind": "attacked", "unit_id": 2, "attacker_id": 9}]
    obs = {0: {"game_time_seconds": 10.0, "units": [hall, footman], "visible_enemies": [grunt], "events": blows * 2}}
    opened = feed.see(obs)
    assert [(i["kind"], i["major"]) for i in opened] == [("fight", True)]
    assert opened[0]["text"] == "Fight at the middle of the map: Claude vs Orc AI"
    assert feed.moments[0]["sides"] == [0, 1]
    obs[0].update(game_time_seconds=12.0, events=[{"kind": "death", "unit_id": 9, "type_id": "ogru", "owner": 1}])
    assert feed.see(obs) == []
    obs[0].update(game_time_seconds=30.0, events=[{"kind": "train_finish", "unit_id": 1, "trained_id": 4,
                                                   "type_id": "Hamg"}])
    later = feed.see(obs)
    assert [i["text"] for i in later] == ["Claude summoned a Archmage",
                                          "Fight at the middle of the map over after 20 s: Orc AI lost 1 Grunt"]
    assert frames.army_value([footman, unit(5, "Hamg", 0, hero=True), hall], REF) == 135 + 525
    named = frames.Feed(REF, {"players": [{"slot": 0, "label": "Claude Sonnet 5.5 + Haiku 4.5"},
                                          {"slot": 1, "label": "Orc AI (normal)"}]})
    assert [named.side(0), named.side(1), named.side(frames.CREEPS)] == ["Claude Sonnet 5.5", "Orc AI", "the creeps"]
    murloc = unit(20, "ogru", 4000, owner=frames.CREEPS)
    creep = [{"kind": "attacked", "unit_id": 20, "attacker_id": 2}] * 4
    obs[0].update(game_time_seconds=40.0, units=[hall, unit(2, "hfoo", 3900)], visible_enemies=[murloc], events=creep)
    assert [(i["kind"], i["major"], i["text"]) for i in feed.see(obs)] == [
        ("creeps", False, "Claude creeping at the east")]


def test_the_director_holds_a_shot_and_cuts_to_a_fight():
    director = frames.Director(0)
    army = {"units": [unit(2, "hfoo", 0), unit(3, "hfoo", 200)], "visible_enemies": []}
    assert director.choose(army, [], 0.0) == (100.0, 0.0)
    marching = {"units": [unit(2, "hfoo", 2000), unit(3, "hfoo", 2200)], "visible_enemies": []}
    assert director.choose(marching, [], 1.0) is None   # the shot holds MIN_SHOT
    assert director.choose(marching, [], 4.5) == (2100.0, 0.0)
    fight = {**marching, "visible_enemies": [unit(9, "ogru", 3000, owner=1)]}
    assert director.choose(fight, [], 5.0) is None       # a fight cuts in after FIGHT_CUT
    assert director.choose(fight, [], 6.5) == (2600.0, 0.0)
    moment = [{"t": 9.0, "x": -6000, "y": 0, "kind": "hero", "sides": [0]}]
    assert director.choose(army, moment, 11.0) == (-6000, 0)


def test_models_are_named_as_people_say_them():
    assert [model_name(m) for m in ("anthropic/claude-sonnet-5-5", "openai/gpt-6-sol", "moonshot/kimi-k3")] == [
        "Claude Sonnet 5.5", "GPT-6 Sol", "Kimi K3"]


XWININFO = """
     0xa00008 (has no name): ("warcraft iii.exe" "warcraft iii.exe")  1x1+0+0  +0+0
     0xa00005 "Warcraft III": ("warcraft iii.exe" "warcraft iii.exe")  960x540+4+30  +4+30
     0x400004 (has no name): ("explorer.exe" "explorer.exe")  160x20+3+29  +3+29
"""


def test_the_game_window_is_found_on_the_display(monkeypatch):
    monkeypatch.setattr(display.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, XWININFO, ""))
    assert display.window_region(":99", "Warcraft III") == (4, 30, 960, 540)
    assert display.window_region(":99", "StarCraft II") is None


def test_jpegs_are_cut_from_the_mjpeg_stream():
    one, two = b"\xff\xd8one\xff\xd9", b"\xff\xd8two\xff\xd9"
    assert display.split_jpegs(one + two + b"\xff\xd8thr") == ([one, two], b"\xff\xd8thr")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="the capture needs ffmpeg")
async def test_the_games_picture_is_a_video_and_a_live_stream(tmp_path):
    capture = display.Capture(["-f", "lavfi", "-i", "testsrc=size=320x180:rate=15"], tmp_path / "game.mp4")
    capture.start()
    try:
        for _ in range(100):
            if capture.frames >= 3:
                break
            await asyncio.sleep(0.1)
        assert capture.latest().startswith(b"\xff\xd8")
        parts = display.mjpeg(capture.latest)
        part = await anext(parts)
        await parts.aclose()
        assert part.startswith(b"--frame\r\nContent-Type: image/jpeg\r\n") and part.endswith(b"\xff\xd9\r\n")
    finally:
        video = capture.stop()
    assert video.read_bytes()[4:8] == b"ftyp" and not capture.running
    files, notes = recording.files(Timeline({"game": "g"}), tmp_path / "rec", "g", ("client",), video)
    assert [f["name"] for f in files] == ["g-client.mp4"] and notes == []
    assert files[0]["bytes"] == (tmp_path / "rec" / "g-client.mp4").stat().st_size > 0


EXPANSION = "Claude expanded: a Town Hall at the gold mine; east = far from home, and then some"


def major(text, kind, side=0, major=True):
    return {"text": text, "side": side, "kind": kind, "major": major}


def played() -> Timeline:
    timeline = Timeline({"game": "g"})
    for t, events in ((0.0, []), (10.0, [major("Claude summoned a Archmage", "hero")]),
                      (30.0, [major("Fight at the north: Claude vs Orc AI", "fight"),
                              major("Fight at the north-east: Claude vs the creeps", "fight")]),
                      (38.0, [major("Fight at the north-east over after 8 s, no losses", "fight_end", None, False)]),
                      (40.0, [major("Fight at the north over after 10 s: Orc AI lost 1 Grunt", "fight_end", None)]),
                      (45.0, [major("Orc AI's Blademaster has fallen", "death", 1), "a line",
                              major("Claude trained a Footman", "train", major=False)]),
                      (70.0, [major("Claude reached tier 2: Keep", "tier")]), (95.0, [major(EXPANSION, "expand")])):
        timeline.add({"t": t, "w": t, "units": [], "events": events, "result": ""})
    timeline.add({"t": 99.0, "units": [], "events": [major("Claude's Archmage reached level 3", "level")],
                  "result": "victory"})
    return timeline


def test_the_highlights_are_the_major_moments_on_the_videos_clock():
    moments = highlights.moments(played())
    assert [(m["w"], m["kind"]) for m in moments] == [(10.0, "hero"), (30.0, "fight"), (30.0, "fight"),
                                                      (45.0, "death"), (70.0, "tier"), (95.0, "expand"),
                                                      (None, "level")]
    assert [(m["end"], m["outcome"][:24]) for m in moments if m["kind"] == "fight"] == [
        (40.0, "Fight at the north over "), (38.0, "Fight at the north-east ")]
    crossed = Timeline({"game": "g"})
    for t, events in ((30.0, [major("Fight at the north-east: Claude vs the creeps", "fight"),
                              major("Fight at the north: Claude vs Orc AI", "fight")]),
                      (38.0, [major("Fight at the north over after 8 s, no losses", "fight_end", None, False)]),
                      (50.0, [major("Fight at the north-east over after 20 s, no losses", "fight_end", None, False)])):
        crossed.add({"t": t, "w": t, "units": [], "events": events, "result": ""})
    assert [m["end"] for m in highlights.moments(crossed)] == [50.0, 38.0]
    chapters = highlights.chapters(played(), 100.0)
    assert [(c["start"], c["end"]) for c in chapters] == [(0.0, 30.0), (30.0, 70.0), (70.0, 95.0), (95.0, 100.0)]
    assert [c["title"] for c in chapters] == ["Start", "Fight at the north: Claude vs Orc AI",
                                              "Claude reached tier 2: Keep",
                                              "Claude expanded: a Town Hall at the gold mine; east = far…"]
    assert highlights.clips(played(), 100.0) == [(5.0, 14.0), (26.0, 49.0), (65.0, 74.0), (90.0, 99.0)]
    assert highlights.clips(played(), 100.0, 30) == [(26.0, 49.0), (65.0, 72.0)]   # the fights and death, merged


def ffprobe(path, *show) -> dict:
    return json.loads(subprocess.run(["ffprobe", "-v", "error", *show, "-of", "json", str(path)], capture_output=True,
                                     check=True).stdout)


def packets(path) -> str:
    """The MD5 of a video's encoded stream: equal for a copy, not for a re-encode."""
    return subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v", "-c", "copy", "-f", "md5", "-"],
                          capture_output=True, check=True, text=True).stdout


def gray(path, at: float) -> bytes:
    return subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{at:.3f}", "-i", str(path), "-frames:v", "1", "-f",
                           "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True, check=True).stdout


def embedded(html: bytes) -> dict:
    text = html.decode()
    return json.JSONDecoder().raw_decode(text, text.index("window.RTS_DATA = ") + len("window.RTS_DATA = "))[0]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="the highlights need ffmpeg")
def test_the_recording_has_chapters_a_highlight_reel_and_a_replay_beside_the_video(tmp_path):
    video = tmp_path / "game.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc=size=160x90:rate=10", "-t", "100", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", str(video)], check=True)
    files, notes = recording.files(played(), tmp_path, "g", ("client", "highlights", "html"), video)
    assert [f["name"] for f in files] == ["g-client.mp4", "g-highlights.mp4", "g.html"] and notes == []
    chapters = ffprobe(tmp_path / "g-client.mp4", "-show_chapters")["chapters"]
    assert [(c["start"], c["tags"]["title"]) for c in chapters] == [
        (0, "Start"), (30000, "Fight at the north: Claude vs Orc AI"), (70000, "Claude reached tier 2: Keep"),
        (95000, "Claude expanded: a Town Hall at the gold mine; east = far…")]
    assert packets(tmp_path / "g-client.mp4") == packets(video)
    assert abs(highlights.duration(tmp_path / "g-highlights.mp4") - 50) < 0.2
    at = 0.0
    for a, b in highlights.clips(played(), 100.0):
        shown, source = gray(tmp_path / "g-highlights.mp4", at + (b - a) / 2), gray(video, a + (b - a) / 2)
        assert sum(abs(p - q) for p, q in zip(shown, source, strict=True)) / len(source) < 4
        at += b - a
    assert embedded((tmp_path / "g.html").read_bytes())["video"] == "g-client.mp4"
    assert highlights.reel(played(), video, tmp_path / "short.mp4", max_seconds=30) == [(26.0, 49.0), (65.0, 72.0)]
    assert highlights.duration(tmp_path / "short.mp4") <= 30.05
    files, notes = recording.files(played(), tmp_path / "b", "g", ("highlights", "html"), video)
    assert [f["name"] for f in files] == ["g-highlights.mp4", "g.html"] and notes == []
    assert "video" not in embedded((tmp_path / "b" / "g.html").read_bytes())
    files, notes = recording.files(played(), tmp_path / "c", "g", ("highlights",))
    assert files == [] and notes == ["no highlights: they are cut from the client video (client_view)"]
    (broken := tmp_path / "broken.mp4").write_bytes(b"no video")
    files, notes = recording.files(played(), tmp_path / "d", "g", ("client", "highlights"), broken)
    assert [(f["name"], (tmp_path / "d" / f["name"]).read_bytes()) for f in files] == [("g-client.mp4", b"no video")]
    assert sorted(p.name for p in (tmp_path / "d").iterdir()) == ["g-client.mp4"]
    assert [n.partition(": ")[0] for n in notes] == ["client video without chapters", "no highlights"]


def test_the_camera_follows_the_agents_fighting():
    def unit(uid, type_id, x, y=0, **more):
        return {"unit_id": uid, "type_id": type_id, "hp": 400, "x": x, "y": y, **more}

    peasant = unit(1, "hpea", 0)
    assert frames.camera_spot({"units": [peasant]}) is None
    fight = {"units": [peasant, unit(2, "hfoo", 0), unit(3, "hfoo", 3000)],
             "visible_enemies": [unit(9, "ogru", 3600, owner=1), unit(8, "ngol", 3200, owner=15, structure=True)]}
    assert frames.camera_spot(fight) == (3300.0, 0.0)
    army = {"units": [unit(2, "hfoo", 0), unit(4, "Hamg", -2000, 100, hero=True, level=2), unit(5, "hfoo", -1500)]}
    assert frames.camera_spot(army) == (-1750.0, 50.0)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="the MP4 needs ffmpeg")
async def test_a_runs_recording_is_copied_into_one_folder(env_vars, local_stores, tmp_path):
    from agent_env.config import get_config
    from agent_env.task.store import TASK_INSTANCES_COLLECTION
    from click.testing import CliRunner

    from agentenv_wc3.cli import wc3

    async with deployed(WC3Env()) as record:
        await match(record)
        await asyncio.to_thread(RemoteSession(base(record)).step, {0: []})
        context = await SaveRTSRecordingTaskStep(id="recording", version=None, env_id="wc3").execute(
            run_context(record))
    get_config().get_document_store().insert(TASK_INSTANCES_COLLECTION, {
        "instance_id": "wc3-smoke-abc", "task_id": "wc3-smoke", "created_at_utc": "2026-10-05 18:17 UTC",
        "context": {"metadata": {"recordings": context.metadata["recordings"]}}})
    result = CliRunner().invoke(wc3, ["recordings", "--out", str(tmp_path / "match")])
    assert result.exit_code == 0, result.output
    assert sorted(p.suffix for p in (tmp_path / "match").iterdir()) == [".html", ".json", ".mp4"]
    assert "wc3-smoke-abc" in result.output and "Open " in result.output
    assert CliRunner().invoke(wc3, ["recordings", "nope", "--out", str(tmp_path / "x")]).exit_code != 0


SEATS = [{"agent": "a", "race": "human"}, {"agent": "b", "race": "orc"}]


async def test_two_agents_play_their_own_seats_in_lockstep(env_vars):
    async with deployed(WC3Env()) as record:
        await client.invoke_extension(record.environment_url, record.environment_card, "urn:wc3:new-game/v1",
                                      {"seats": SEATS, "time_limit_seconds": 60, "lockstep": {"stall_seconds": 2}})
        a, b = RemoteSession(base(record) + "/seats/a"), RemoteSession(base(record) + "/seats/b")
        seen = await asyncio.to_thread(b.observe)
        assert set(seen["observations"]) == {1} and seen["you"] == 1 and [x["agent"] for x in seen["seats"]] == [
            "a", "b"]
        first = asyncio.create_task(asyncio.to_thread(a.step, {0: []}, 1000))
        await asyncio.sleep(0.5)
        assert not first.done()   # a's step waits for b's
        await asyncio.to_thread(b.step, {1: []}, 1000)
        assert (await first)["observations"][0]["game_time_seconds"] == 1.0
        alone = await asyncio.to_thread(a.step, {0: []}, 1000)   # b stalls past 2 s: a goes on
        assert alone["observations"][0]["game_time_seconds"] == 2.0
        began = time.monotonic()
        again = await asyncio.to_thread(a.step, {0: []}, 1000)   # and without b from then on
        assert again["observations"][0]["game_time_seconds"] == 3.0 and time.monotonic() - began < 1.5
        with pytest.raises(SessionError, match="only its own units"):
            await asyncio.to_thread(a.step, {1: []}, 1000)
        async with streamable_http_client(base(record) + "/seats/b/mcp") as (read, write, _), \
                ClientSession(read, write) as mcp:
            await mcp.initialize()
            state = (await mcp.call_tool("get_state", {})).content[0].text
        assert state.startswith("You are slot 1, orc, team 2. Against: a (human).")
        summary = (await client.get_data(base(record))).parts[0].data
    assert [(x["agent"], x["slot"], x["stalls"]) for x in summary["seats"]] == [("a", 0, 0), ("b", 1, 1)]
    assert summary["seats"][0]["metrics"]["workers"] == 5


async def test_a_realtime_game_starts_once_every_seat_has_moved(env_vars):
    async with deployed(WC3Env()) as record:
        await client.invoke_extension(record.environment_url, record.environment_card, "urn:wc3:new-game/v1",
                                      {"seats": SEATS, "mode": "realtime", "time_limit_seconds": 60})
        a, b = RemoteSession(base(record) + "/seats/a"), RemoteSession(base(record) + "/seats/b")
        async with streamable_http_client(base(record) + "/seats/b/mcp") as (read, write, _), \
                ClientSession(read, write) as mcp:
            await mcp.initialize()
            state = (await mcp.call_tool("get_state", {})).content[0].text
        assert "The clock starts once every player has made its first move" in state

        def waiting():
            with urllib.request.urlopen(base(record) + "/live/data.json") as r:
                return json.load(r)["live"]["waiting"]

        assert await asyncio.to_thread(waiting) == [0, 1]
        first = asyncio.create_task(asyncio.to_thread(a.step, {0: []}, 1000))
        await asyncio.sleep(1.5)
        assert not first.done() and await asyncio.to_thread(waiting) == [1]   # a is ready; the game waits for b
        await asyncio.to_thread(b.step, {1: []}, 1000)
        assert (await first)["observations"][0]["game_time_seconds"] < 1.0   # the 1.5 s a waited were not played
        assert await asyncio.to_thread(waiting) == []


async def test_lockstep_moves_to_the_nearest_deadline():
    clock, moves = {"t": 0.0}, []

    async def advance(batches, seconds):
        moves.append((sorted(batches), round(seconds, 3)))
        clock["t"] += seconds

    lock = Lockstep([0, 1], 30, advance, lambda: clock["t"], lambda p: False)
    await asyncio.gather(lock.step(0, ["x"], 5.0), lock.step(1, [], 1.0), _later(lock.step(1, [], 4.0)))
    assert moves == [([0, 1], 1.0), ([0, 1], 4.0)] and clock["t"] == 5.0


async def test_a_stalled_player_holds_the_game_once_and_rejoins():
    clock, moves = {"t": 0.0}, []

    async def advance(batches, seconds):
        moves.append(sorted(batches))
        clock["t"] += seconds

    lock = Lockstep([0, 1], 0.2, advance, lambda: clock["t"], lambda p: False)
    began = time.monotonic()
    for _ in range(4):
        await lock.step(0, [], 1.0)
    assert time.monotonic() - began < 0.6 and lock.stalls == {0: 0, 1: 1}   # one 0.2 s wait, not four
    await asyncio.wait_for(asyncio.gather(lock.step(1, [], 1.0), _later(lock.step(0, [], 1.0))), 1)
    assert moves[-1] == [0, 1] and clock["t"] == 5.0 and lock.stalls == {0: 0, 1: 1}


async def test_a_player_who_is_out_holds_nobody_up():
    clock, moves, out = {"t": 0.0}, [], set()

    async def advance(batches, seconds):
        moves.append(sorted(batches))
        clock["t"] += seconds
        out.add(1)

    lock = Lockstep([0, 1], 30, advance, lambda: clock["t"], lambda p: p in out)
    await asyncio.wait_for(asyncio.gather(lock.step(0, [], 2.0), lock.step(1, [], 5.0)), 1)
    await asyncio.wait_for(lock.step(0, [], 1.0), 1)
    assert moves == [[0, 1], [0]] and clock["t"] == 3.0 and lock.stalls == {0: 0, 1: 0}


async def test_a_waiting_player_goes_once_the_game_ends_outside_a_step(monkeypatch):
    monkeypatch.setattr(Lockstep, "RECHECK_SECONDS", 0.05)
    out = set()

    async def advance(batches, seconds):
        raise AssertionError("the game ended without a step")

    lock = Lockstep([0, 1], 30, advance, lambda: 0.0, lambda p: p in out)
    waiting = asyncio.create_task(lock.step(0, [], 1.0))
    await asyncio.sleep(0.1)
    assert not waiting.done()
    out.update({0, 1})
    await asyncio.wait_for(waiting, 1)


async def _later(step):
    await asyncio.sleep(0.05)
    await step


def test_seats_say_who_plays():
    assert [(x["slot"], x["agent"], x["computer"], x["team"]) for x in seats_of(check_scenario(
        {**WC3Env.__init__.__globals__["DEFAULT_SCENARIO"]}))] == [(0, None, None, 1), (1, None, "normal", 2)]
    base_scenario = dict(WC3Env.__init__.__globals__["DEFAULT_SCENARIO"])
    for seats, error in (([{"computer": "easy"}, {"computer": "normal"}], "same difficulty"),
                         ([{"agent": "a"}, {"agent": "a"}], "one seat"), ([{"computer": "easy"}], "agent seat"),
                         ([{"agent": "a", "race": "elf"}], "race"), ([{"agent": "a", "colour": 1}], "colour")):
        with pytest.raises(ValueError, match=error):
            check_scenario({**base_scenario, "seats": seats})


def test_a_team_wins_together_once_every_rival_is_defeated(env_vars):
    env = WC3Env()
    env.seats = seats_of(check_scenario({**env.scenario, "map": "(4)TurtleRock.w3x", "seats": [
        {"agent": "a", "team": 1}, {"agent": "b", "team": 1}, {"computer": "easy", "team": 2},
        {"computer": "easy", "team": 2}]}))
    env.obs = {0: {"game_time_seconds": 60.0}, 1: {}, 2: {"result": "defeat"}, 3: {}}
    assert [env._result_of(s) for s in range(4)] == ["", "", "defeat", ""]
    assert not env.game_over
    env.obs[3]["result"] = "defeat"
    assert [env._result_of(s) for s in range(4)] == ["victory", "victory", "defeat", "defeat"] and env.game_over


def test_a_free_for_all_plays_on_after_one_seat_is_out(env_vars):
    env = WC3Env()
    env.seats = seats_of(check_scenario({**env.scenario, "map": "(4)TurtleRock.w3x", "seats": [
        {"agent": "a"}, {"agent": "b"}, {"agent": "c"}]}))
    env.obs = {0: {"result": "defeat", "game_time_seconds": 60.0}, 1: {}, 2: {}}
    assert not env.game_over and [env._session_state(x)["done"] for x in env.seats] == [True, False, False]
    env.obs[2]["result"], env.obs[1]["result"] = "defeat", "victory"
    assert env.game_over


async def test_each_agent_gets_its_seat():
    posted, servers = [], {"a": {}, "b": {"x": {"url": "http://env:1/mcp"}}}

    class Agent(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self._reply({"mcp_servers": servers[self.path.split("/")[1]]})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            posted.append((self.path.split("/")[1], body))
            servers[self.path.split("/")[1]][body["name"]] = {"url": body["url"]}
            self._reply({"status": "added"})

        def _reply(self, doc):
            data = json.dumps(doc).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Agent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    card = {"capabilities": {"extensions": [{"uri": "urn:agentenv:mcp-config/v1",
                                             "params": {"endpoint": "/ext/mcp-config"}}]}}
    agents = [DeployedAgent(agent_name=n, api_url="", a2a_url=f"http://127.0.0.1:{server.server_port}/{n}",
                            a2a_card=card) for n in ("a", "b")]
    env = DeployedEnv(env_id="wc3", env_version=1, environment_card_url="http://env:1" + WELL_KNOWN_PATH,
                      environment_card={"name": "wc3"})
    context = TaskStepContext(deployed_envs=[env], deployed_agents=agents)
    step = RTSSeatAgentsTaskStep.from_dict({"id": "seat", "type": "rts_seat_agents", "env_id": "wc3", "agents": ["a"]})
    try:
        assert (await step.execute(context)).metadata["rts_seats"] == {"a": "http://env:1/seats/a/mcp"}
        assert await seat_agents(context, env, ["a"]) == {"a": "http://env:1/seats/a/mcp"}   # once
        with pytest.raises(RuntimeError, match="env_ids"):   # by default every deployed agent: a and b
            await RTSSeatAgentsTaskStep(id="seat", version=None, env_id="wc3").execute(context)
        with pytest.raises(RuntimeError, match="no deploy_agent"):
            await seat_agents(context, env, ["c"])
    finally:
        server.shutdown()
    assert posted == [("a", {"url": "http://env:1/seats/a/mcp", "headers": None, "name": "wc3"})]


async def test_staging_places_units_by_name_and_names_them(env_vars):
    env = WC3Env()
    try:
        await env.new_game(seats=[{"agent": "wc3", "race": "human"}, {"computer": "normal", "race": "orc"}],
                           time_limit_seconds=120)
        real, calls = env.bridge.call, []

        async def call(cmd, **args):
            if cmd != "debug":
                return await real(cmd, **args)
            calls.append((args["op"], args["args"]))
            return {"unit_ids": [100 + len(calls), 200 + len(calls)]} if args["op"] == "spawn" else {}

        env.bridge.call = call
        home = env.metrics.home(env.obs[0])
        result = await env.stage(warmup_seconds=2, ops=[
            {"op": "ai", "player": "opponent", "paused": True},
            {"op": "resources", "player": "wc3", "gold": 1500, "lumber": 800},
            {"op": "spawn", "player": "wc3", "type": "hfoo", "n": 2, "at": "home", "dx": -1150, "as": "army"},
            {"op": "level", "unit": "army", "level": 3},
            {"op": "spawn", "player": "opponent", "type": "ogru", "at": "toward:enemy_home:700", "as": "enemy"}])
        with pytest.raises(ValueError, match="no units named"):
            await env.stage(ops=[{"op": "give", "unit": "hero", "type": "phea"}])
        with pytest.raises(ValueError, match="unknown op"):
            await env.stage(ops=[{"op": "teleport"}])
    finally:
        await env.close()
    assert result["handles"] == {"army": 2, "enemy": 2} and env.stats["staged_seconds"] == 2
    assert calls[0] == ("ai", {"paused": 1, "player": 1}) and calls[1] == ("resources", {
        "gold": 1500, "lumber": 800, "player": 0})
    assert calls[2] == ("spawn", {"type_id": "hfoo", "player": 0, "n": 2, "x": home["x"] - 1150, "y": float(home["y"])})
    assert calls[3:5] == [("level", {"unit_id": 103, "level": 3}), ("level", {"unit_id": 203, "level": 3})]
    assert calls[5][1]["player"] == 1 and env.metrics.handles["enemy"]["ids"] == [106, 206]


def test_metrics_measure_a_seat_over_the_game():
    ref = render.Reference({"units": {"hpea": {"name": "Peasant", "builds": ["htow"], "gold": 75},
                                      "hfoo": {"name": "Footman", "gold": 135}, "ogru": {"name": "Grunt", "gold": 200},
                                      "htow": {"name": "Town Hall", "structure": True, "food_made": 12},
                                      "hkee": {"name": "Keep", "structure": True, "food_made": 12}}})
    m = metrics.Metrics(ref, {"creep_camps": [{"x": 3000, "y": 0}], "start_locations": [{"x": 0, "y": 0},
                                                                                         {"x": 9000, "y": 0}]}, [0])
    hall = {"unit_id": 1, "type_id": "hkee", "structure": True, "hp": 2000, "max_hp": 2000, "x": 50, "y": 0}
    idle = {"unit_id": 2, "type_id": "hpea", "hp": 220, "max_hp": 220, "x": 0, "y": 0, "order": None}
    foot = {"unit_id": 3, "type_id": "hfoo", "hp": 420, "max_hp": 420, "x": 3000, "y": 100}

    def obs(t, units, events=(), food=(10, 12), enemies=()):
        return {0: {"game_time_seconds": t, "units": units, "player": {"gold": 100, "food_used": food[0],
                                                                       "food_cap": food[1]},
                    "events": list(events), "visible_enemies": list(enemies), "score": {"total": 900}}}

    m.stage("army", 0, [3])
    m.see(obs(0.0, [hall, idle, foot], enemies=[{"unit_id": 9, "owner": 12, "hp": 50, "x": 3100, "y": 0}]))
    m.see(obs(10.0, [hall, idle, foot], food=(12, 12)))
    m.see(obs(20.0, [hall, {**idle, "order": "harvest"}, {**foot, "hp": 210}],
              events=[{"kind": "death", "unit_id": 9, "owner": 12, "type_id": "ogru"}]))
    got = m.of(0, obs(20.0, []))
    assert (got["idle_worker_seconds"], got["supply_blocked_seconds"], got["workers"]) == (20.0, 10.0, 1)
    assert got["first_time"]["hfoo"] == 0.0 and got["count"]["hkee"] == 1 and got["tier"] == 2
    assert got["camp_cleared"] is True and got["camps_cleared"] == [1] and got["army_kept_percent"] == 50
    assert got["total"] == 900 and got["hero_alive"] is False and got["units_lost"] == 0


def test_the_timeline_keeps_a_frame_per_half_second_and_every_event():
    timeline = Timeline({"game": "g"})
    for t in (0.0, 0.1, 0.2, 0.3, 0.6):
        timeline.add({"t": t, "units": [], "events": [], "result": ""})
    timeline.add({"t": 0.7, "units": [], "events": ["a fight"], "result": ""})
    timeline.add({"t": 0.8, "units": [], "events": [], "result": "victory"})
    assert [f["t"] for f in timeline.frames] == [0.0, 0.3, 0.6, 0.7, 0.8]
    assert timeline.doc(0.6)["live"] == {"t": 0.8, "result": "victory", "over": True, "frames": 5}


QUESTIONS = {"Footman 1": {"type": "choice", "instructions": "Pick.", "criteria": {
    "Attack Grunt 2": "Attack", "Wait for now": "Stay idle.", "Move back": "Move"}},
    "Footman 2": {"type": "choice", "instructions": "Pick.", "criteria": {"Attack Grunt 2": "Attack",
                                                                           "Move back": "Move"}}}


def test_choice_answers_fall_back_to_keeping_the_current_order():
    chosen, fallbacks = choices.parse_answers(
        'Sure: {"answers": {"Footman 1": "Charge!", "Footman 2": "Move back"}}', QUESTIONS)
    assert chosen == {"Footman 1": "Wait for now", "Footman 2": "Move back"} and fallbacks == 1
    assert choices.parse_answers("no json", QUESTIONS) == ({"Footman 1": "Wait for now",
                                                            "Footman 2": "Attack Grunt 2"}, 2)


def test_any_chat_model_answers_choice_questions_in_jevs_shape():
    seen = []

    class Endpoint(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append((self.path, self.headers["Authorization"],
                         json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            body = json.dumps({"choices": [{"message": {"content": json.dumps(
                {"answers": {"Footman 1": "Attack Grunt 2", "Footman 2": "Move back"}})}}],
                "usage": {"prompt_tokens": 900, "completion_tokens": 40}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        payload = {"model": "jev-latest", "state": {"format": "readable-units-v7"}, "questions": QUESTIONS}
        response, record = choices.answer(payload, base_url=f"http://127.0.0.1:{server.server_port}", api_key="k",
                                          model="anthropic/claude-haiku-4-5")
    finally:
        server.shutdown()
    path, auth, body = seen[0]
    assert path == "/v1/chat/completions" and auth == "Bearer k" and body["model"] == "anthropic/claude-haiku-4-5"
    assert "Footman 1" in body["messages"][1]["content"] and "temperature" not in body
    assert response["answers"]["Footman 1"]["choice"] == "Attack Grunt 2"
    assert response["usage"] == {"input_tokens": 900, "output_tokens": 40} and record["fallbacks"] == 0
    jev = pytest.importorskip("wc3agent.models.jev")   # wc3agent's own check of a Jev reply
    checked = {"request": payload, "status": 200, "response": response}
    jev.validate_choice_response(checked)
    assert checked["contract_valid"] is True


async def finished(rule: str) -> tuple[dict, dict]:
    """A 60-second game whose agent stopped after 5 s, settled by rts_finish's `rule`: the summary, and its grade."""
    async with deployed(WC3Env()) as record:
        await match(record)
        session = RemoteSession(base(record))
        for _ in range(5):
            await asyncio.to_thread(session.step, {0: []})
        context = await RTSFinishTaskStep(id="finish", version=None, env_id="wc3", rule=rule).execute(
            run_context(record))
        context = await RTSGradeTaskStep(id="grade", version=None, env_id="wc3").execute(context)
        summary = (await client.get_data(base(record))).parts[0].data
    return summary, context.metadata


async def test_a_game_its_agents_left_is_played_out_to_its_end(env_vars):
    summary, metadata = await finished("play_out")
    assert summary["game_over"] and summary["result"] == "time_limit" and summary["game_time_seconds"] == 60
    assert summary["harness"]["finish_seconds"] == 55 and summary["harness"]["idle_seconds"] == 0
    assert summary["finish"] == metadata["rts_finish"]["finish"] == {
        "rule": "play_out", "from_seconds": 5.0, "to_seconds": 60.0, "open_seats": [{"slot": 0, "agent": None}],
        "game_over": True}
    rows = {r["name"]: r for r in metadata["verifications"]["grade"]["results"]}
    assert rows["agent_played"]["result"] is False   # it sent no orders: finishing the game is not playing it
    assert rows["finish"]["weight"] == 0 and "stopped before the end (last move at 4.0 s)" in rows["finish"]["evidence"]


async def test_a_forfeit_loses_now_and_as_is_leaves_the_game_alone(env_vars):
    summary, _ = await finished("forfeit")
    assert summary["game_over"] and summary["result"] == "defeat" and summary["game_time_seconds"] == 5
    assert [(x["result"], x["forfeit"]) for x in summary["seats"]] == [("defeat", True), ("victory", False)]
    summary, _ = await finished("as_is")
    assert not summary["game_over"] and summary["game_time_seconds"] == 5 and summary["finish"]["rule"] == "as_is"
    with pytest.raises(ValueError, match="rule must be one of"):
        RTSFinishTaskStep(id="finish", version=None, env_id="wc3", rule="resign")
