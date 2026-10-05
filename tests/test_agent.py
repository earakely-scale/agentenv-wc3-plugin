"""The wc3-macro-micro agent (agents/wc3-player): wc3env's wc3agent plays the env on the fake game through the
urn:rts session, its macro and micro models answered by a stand-in OpenAI-compatible endpoint."""

import asyncio
import http.server
import importlib.util
import json
import sys
import threading
from pathlib import Path

import pytest
from agentenv_protocol import client
from test_steps import deployed, run_context

from agentenv_rts import choices
from agentenv_wc3.server import WC3Env
from agentenv_wc3.steps import WC3MatchTaskStep

pytest.importorskip("wc3agent")
pytestmark = pytest.mark.anyio
PLAYER = Path(__file__).resolve().parents[1] / "agents/wc3-player"


def player():
    sys.path.insert(0, str(PLAYER))
    spec = importlib.util.spec_from_file_location("wc3_player_agent", PLAYER / "agent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Models:
    """An OpenAI-compatible endpoint: the macro model plans in prose (no orders), the micro model picks each unit's
    first choice."""

    def __init__(self):
        self.calls = []
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
                    text = "Plan: keep the workers on gold and scout the enemy start."
                    models.calls.append(("macro", body["model"], body.get("reasoning_effort")))
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
        await WC3MatchTaskStep(id="match", version=None, env_id="wc3", time_limit_seconds=60,
                               mode=mode).execute(run_context(record))
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
    assert result.success and "macro turns (anthropic/claude-sonnet-5-5)" in text
    assert result.usage.tool_call_count == summary["turns"] + summary["micro_calls"]
    assert result.native_trajectory is not None


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
