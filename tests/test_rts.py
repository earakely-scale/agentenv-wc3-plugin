"""The RTS pieces (agentenv_rts) on the WC3 env served over HTTP, on wc3env's fake game: a program playing through
the urn:rts session, the spectator's live data and page, the recording and its task step, and the choice-question
transport any chat model answers in Jev's shape."""

import asyncio
import base64
import http.server
import json
import shutil
import threading
import urllib.request

import pytest
from agent_env.artifact import FileArtifact
from agentenv_protocol import client
from test_steps import deployed, run_context

from agentenv_rts import choices
from agentenv_rts.session import RemoteSession, SessionError
from agentenv_rts.steps import SaveRTSRecordingTaskStep
from agentenv_rts.timeline import Timeline
from agentenv_wc3.server import WC3Env
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
    assert sorted(f["name"].rpartition(".")[2] for f in saved) == ["html", "mp4"]
    load = {f["name"].rpartition(".")[2]: FileArtifact.get(f["artifact_id"], f["version"]).load() for f in saved}
    assert load["mp4"][4:8] == b"ftyp"
    html = load["html"]
    assert b"window.RTS_DATA = {" in html and b"</script>" in html


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
