"""The wc3-llm agent (agents/wc3-llm): a model's tool loop over the env's MCP tools on the fake game, with the model
answered by a stand-in that plays get_state and advance, stops once early, and says the result at GAME OVER."""

import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest
from test_steps import deployed

from agentenv_wc3.server import WC3Env

pytestmark = pytest.mark.anyio
AGENT = Path(__file__).resolve().parents[1] / "agents/wc3-llm/agent.py"
ENDPOINT = {"LITELLM_BASE_URL": "http://models", "LITELLM_API_KEY": "k"}


def llm():
    spec = importlib.util.spec_from_file_location("wc3_llm_agent", AGENT)
    module = sys.modules[spec.name] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def calling(name: str, n: int, **args) -> dict:
    return {"content": None, "tool_calls": [{"id": f"c{n}", "type": "function",
                                             "function": {"name": name, "arguments": json.dumps(args)}}]}


async def test_a_model_plays_the_game_through_the_tools(env_vars):
    agent, sent, offered = llm(), [], set()

    async def complete(http, base, key, model, chat, tools):
        sent.append(agent.outgoing(chat))
        offered.update(t["function"]["name"] for t in tools)
        last, n = chat[-1], len(sent)
        if last["role"] == "user" and last["content"] != agent.NUDGE:
            message = calling("get_state", n)
        elif last["role"] == "tool" and "GAME OVER" in last["content"]:
            message = {"content": "The game ended at its time limit."}
        elif n == 2:
            message = {"content": "I will wait and see."}   # stops early: the agent tells it to keep playing
        else:
            message = {**calling("advance", n, seconds=60), "content": "Plan: workers on gold, then advance."}
        return message, {"prompt_tokens": 1000, "completion_tokens": 20, "cache_read_input_tokens": 900}, 0.01

    agent.complete = complete
    env = WC3Env()
    async with deployed(env) as record:
        player = agent.Player("anthropic/claude-haiku-4-5", {"url": record.environment_url + "/mcp"}, ENDPOINT, 50)
        await player.play("Win the game.")
        over, result = await player.over()
        kept = (await env.kept(["agent_files"])).files
        transcript = json.loads(next(f.file for f in kept if f.name.endswith(agent.TRANSCRIPT)).read_text())
    assert over and result == "time_limit"
    assert [m["role"] for m in transcript["messages"]][:3] == ["user", "assistant", "tool"]
    assert transcript["messages"][0]["content"] == "Win the game." and transcript["stats"]["turns"] == len(sent)
    assert player.stats["nudges"] == 1 and player.stats["tool_calls"] == 3 and player.reply.startswith("The game ended")
    assert player.stats["cached_tokens"] == 900 * player.stats["turns"] and player.stats["cost_usd"] > 0
    assert {"get_state", "act", "advance"} <= offered
    first = sent[0]
    assert first[0]["content"][0]["cache_control"] == {"type": "ephemeral"}   # the system prompt
    assert first[-1]["content"][0]["cache_control"] == {"type": "ephemeral"}   # and the newest message


async def test_the_spectators_see_its_model_and_plans(env_vars):
    agent = llm()
    turns = iter([calling("advance", 1, seconds=60), {**calling("advance", 2, seconds=60), "content": "Plan: rush."},
                  {"content": "Over."}])

    async def complete(http, base, key, model, chat, tools):
        return next(turns), {}, 0.0

    agent.complete = complete
    env = WC3Env()
    async with deployed(env) as record:
        await agent.Player("anthropic/claude-haiku-4-5", {"url": record.environment_url + "/mcp"}, ENDPOINT,
                           10).play("Play.")
    assert [p["label"] for p in env.timeline.static["players"] if p["slot"] == 0] == ["Claude Haiku 4.5"]
    assert [n["text"] for f in env.timeline.frames for n in f.get("notes") or () if n["kind"] == "plan"] == [
        "Plan: rush."]


def test_old_tool_results_are_trimmed_all_at_once():
    agent = llm()
    chat = [{"role": "user", "content": "go"}]
    for i in range(10):
        chat += [{"role": "assistant", "content": None, "tool_calls": []},
                 {"role": "tool", "tool_call_id": str(i), "content": "x" * 10_000}]
    agent.trim(chat)
    assert [m["content"] for m in chat if m["role"] == "tool"] == ["x" * 10_000] * 10   # under TRIM_CHARS
    chat[-1]["content"] = "x" * agent.TRIM_CHARS
    agent.trim(chat)
    kept = [m["content"] for m in chat if m["role"] == "tool"]
    assert kept[:-agent.KEEP] == [agent.TRIMMED] * (10 - agent.KEEP) and agent.TRIMMED not in kept[-agent.KEEP:]


async def test_a_model_call_that_times_out_is_retried(monkeypatch):
    agent, calls, chat = llm(), [], [{"role": "user", "content": "go"}]

    def answer(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("no answer", request=request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}], "usage": {"total_tokens": 3}},
                              headers={"x-litellm-response-cost": "0.01"})

    async def no_wait(seconds):
        pass

    monkeypatch.setattr(agent.asyncio, "sleep", no_wait)
    async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as http:
        message, usage, cost = await agent.complete(http, "http://models", "k", "m", chat, [])
    assert (message["content"], usage, cost, len(calls)) == ("hi", {"total_tokens": 3}, 0.01, 2)
    calls.clear()
    monkeypatch.setattr(agent, "RETRIES", 1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as http:
        with pytest.raises(httpx.ReadTimeout):
            await agent.complete(http, "http://models", "k", "m", chat, [])
