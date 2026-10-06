"""The wc3-macro-micro agent (agents/wc3-player): wc3env's wc3agent plays the env on the fake game through the
urn:rts session, and a seat of a stand-in env, its macro and micro models answered by a stand-in OpenAI-compatible
endpoint."""

import asyncio
import http.server
import importlib.util
import json
import re
import sys
import threading
from pathlib import Path

import pytest
from agentenv_protocol import client
from agentenv_protocol.a2a_agent import TaskOutcome, TaskRequest, TextPart
from test_steps import deployed, run_context, seat_and_start

from agentenv_rts import choices
from agentenv_rts.session import DEBUG, NOTE, OBSERVE, STEP
from agentenv_wc3.server import WC3Env
from agentenv_wc3.steps import WC3MatchTaskStep

pytest.importorskip("wc3agent")
fake_server = pytest.importorskip("wc3env.fake_server")
pytestmark = pytest.mark.anyio
PLAYER = Path(__file__).resolve().parents[1] / "agents/wc3-player"


def player():
    sys.path.insert(0, str(PLAYER))
    spec = importlib.util.spec_from_file_location("wc3_player_agent", PLAYER / "agent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Models:
    """An OpenAI-compatible endpoint: the macro model replies `plan` (by default prose, no orders), the micro model
    picks each unit's first choice."""

    def __init__(self, plan="Plan: keep the workers on gold and scout the enemy start."):
        self.calls, self.macro = [], []
        models = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                system = body["messages"][0]["content"]
                if system == choices.SYSTEM:
                    asked = json.loads(body["messages"][1]["content"])["questions"]
                    text = json.dumps({"answers": {q: next(iter(v["choices"])) for q, v in asked.items()}})
                    models.calls.append(("micro", body["model"]))
                else:
                    text = plan
                    models.calls.append(("macro", body["model"], body.get("reasoning_effort")))
                    models.macro.append(json.dumps(body["messages"]))
                reply = json.dumps({"choices": [{"message": {"content": text}}],
                                    "usage": {"prompt_tokens": 1000, "completion_tokens": 50}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"


@pytest.mark.parametrize("mode", ["stepping", "realtime"])
async def test_wc3agent_plays_the_game_through_the_rts_session(env_vars, tmp_path, monkeypatch, mode):
    agent = player()
    models = Models()
    environ = {"LITELLM_BASE_URL": models.url, "LITELLM_API_KEY": "sk-test",
               "WC3_MICRO_MODEL": "anthropic/claude-haiku-4-5",
               # realtime plays in wall time: a few seconds of it; stepping plays the match to its time limit
               **({"WC3_MAX_GAME_SECONDS": "4"} if mode == "realtime" else {})}
    monkeypatch.setattr(agent.os, "environ", dict(agent.os.environ))
    async with deployed(WC3Env()) as record:
        await seat_and_start(await WC3MatchTaskStep(id="match", version=None, env_id="wc3", time_limit_seconds=60,
                                                    mode=mode).execute(run_context(record)))
        config = agent.WC3Config(model="anthropic/claude-sonnet-5-5")
        servers = {"wc3": {"url": record.mcp_url}}
        summary = await asyncio.to_thread(agent.play_game, config, servers, environ, tmp_path / "session")
        data = (await client.get_data(record.mcp_url.removesuffix("/mcp"))).parts[0].data
    assert summary["result"] == "time_limit" and summary["turns"] >= 1
    assert ("macro", "anthropic/claude-sonnet-5-5", "low") in models.calls
    assert data["harness"]["session_steps"] > 0 and data["mode"] == mode
    assert data["game_over"] is (mode == "stepping")
    cost = summary["cost"]["by_model"]["anthropic/claude-sonnet-5-5"]
    assert cost["calls"] == summary["turns"] and cost["input"] == 1000 * summary["turns"]
    result = agent.result_of(summary, tmp_path / "session")
    text = " ".join(getattr(p, "text", "") for p in result.parts)
    assert result.outcome is TaskOutcome.SUCCEEDED and "macro turns (anthropic/claude-sonnet-5-5)" in text
    assert result.usage.tool_call_count == summary["turns"] + summary["micro_calls"]
    assert result.native_trajectory is not None


class Seat:
    """A stand-in env serving one seat as the seat contract has it: every request comes in under /players/wc3, the seat
    is slot 1 (wc3env's fake world, with a peon beside the orc hall) and sees only its own observation, and three
    seconds in one of the other side's peasants dies."""

    ENDPOINTS = {OBSERVE: "/rts/observe", STEP: "/rts/step", DEBUG: "/rts/debug", NOTE: "/rts/note"}

    def __init__(self, seconds=30):
        self.world, self.seconds = fake_server.SyntheticWorld.echo_isles_start(), seconds
        self.world.units[2001] = {**self.world.units[1001], "unit_id": 2001, "type_id": "opeo", "owner": 1,
                                  "x": 5000.0, "y": -2800.0}
        self.paths, self.steps, self.notes = [], [], []
        seat = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seat.paths.append(self.path)
                self.reply({"capabilities": {"extensions": [{"uri": uri, "params": {"endpoint": endpoint}}
                                                            for uri, endpoint in Seat.ENDPOINTS.items()]}})

            def do_POST(self):
                seat.paths.append(self.path)
                self.reply(seat.answer(self.path.rsplit("/", 1)[-1],
                                       json.loads(self.rfile.read(int(self.headers["Content-Length"])))))

            def reply(self, value):
                data = json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/players/wc3/mcp"

    def answer(self, name: str, body: dict) -> dict:
        if name == "note":
            self.notes.append(body)
            return {}
        if name == "debug":
            return {"ignored": True}
        if name == "observe":
            return self.state()
        self.steps.append(body["actions"])
        rejected = self.world.act(1, body["actions"].get("1", []))
        self.world.advance(body["ms"])
        return {**self.state(), "rejected": {"1": rejected}, "placements": {"1": []}, "elapsed_ms": body["ms"]}

    def state(self) -> dict:
        dying = self.world.game_time_ms >= 3000 and self.world.units.pop(1001, None)
        obs = {**self.world.observe(1), "game_time_seconds": self.world.game_time_ms / 1000}
        if dying:
            obs["events"] = [{"kind": "death", "unit_id": 1001, "type_id": "hpea", "owner": 0}]
        done = obs["game_time_seconds"] >= self.seconds
        return {"observations": {"1": obs}, "you": 1, "done": done, "result": "time_limit" if done else None,
                "seats": [{"slot": 0, "agent": "other", "computer": None, "race": "human", "team": 1},
                          {"slot": 1, "agent": "wc3", "computer": None, "race": "orc", "team": 2}],
                "scenario": {"map": "(2)EchoIsles.w3x", "race": "human", "opponent_race": "orc",
                             "ai_difficulty": "easy", "time_limit_seconds": self.seconds, "mode": "stepping"},
                "setup": {}, "time_limit_seconds": self.seconds}


async def test_wc3agent_plays_its_seat_with_the_prompt_as_its_goal(monkeypatch):
    agent = player()
    models, seat = Models(plan="Plan: scout the enemy start.\n\nmove peon1 at 0 0"), Seat()
    goal = "Scout the enemy base with your peon."
    monkeypatch.setattr(agent.os, "environ", {**agent.os.environ, "LITELLM_BASE_URL": models.url,
                                              "LITELLM_API_KEY": "sk-test", "WC3_MICRO_MODEL": "off",
                                              "WC3_GOAL": "prompt"})
    request = TaskRequest(task_id="t", context_id="c", parts=(TextPart(text=goal),),
                          config=agent.WC3Config(model="anthropic/claude-sonnet-5-5"),
                          mcp_servers={"wc3": {"url": seat.url}})
    result = await agent.WC3Player().run(request)
    assert result.outcome is TaskOutcome.SUCCEEDED, result.error
    assert seat.paths and all(p.startswith("/players/wc3/") for p in seat.paths)
    assert seat.steps and all(set(actions) == {"1"} for actions in seat.steps)
    moved = [a for actions in seat.steps for a in actions["1"] if a["command"] == "move"]
    assert moved and moved[0]["unit_id"] == 2001 and moved[0]["arguments"]["x"] == 0
    assert seat.notes and {n["slot"] for n in seat.notes} == {1}
    assert all(f"THIS GAME IS A TEST OF ONE THING. {goal}" in m for m in models.macro)
    assert any(re.search(r"enemy peasant\d+ died", m) for m in models.macro)
    assert not any(re.search(r"your peasant\d+ died", m) for m in models.macro)


def test_the_micro_transport_fills_wc3agents_record():
    sys.path.insert(0, str(PLAYER))
    from remote import chat_micro

    models = Models()
    questions = {"Footman 1": {"type": "choice", "instructions": "Pick.", "criteria": {"Wait for now": "Stay idle.",
                                                                                     "Attack": "Attack"}}}
    record = {}
    response = chat_micro(models.url, "sk-test", "anthropic/claude-haiku-4-5")(
        {"model": "anthropic/claude-haiku-4-5", "state": {}, "questions": questions}, "ignored", record)
    assert response["answers"]["Footman 1"]["choice"] == "Wait for now"
    assert record["status"] == 200 and record["contract_valid"] and record["latency_ms"] > 0
    assert record["request"]["questions"] == questions and models.calls == [("micro", "anthropic/claude-haiku-4-5")]
