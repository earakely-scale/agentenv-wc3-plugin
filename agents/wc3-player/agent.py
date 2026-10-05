"""An A2A agent that plays Warcraft III with wc3env's own agent, `wc3agent`: a macro model (System 2) plans the
economy, workers and army objectives every few game seconds, and a micro model (System 1) controls the army unit by
unit, at most once a second per group. It plays the game the task's wc3_match started in the env, through the env's
urn:rts session (remote.py), stepped or in realtime as the match says.

Models come from agent-env's model endpoint (LITELLM_BASE_URL, LITELLM_API_KEY):
- macro: the prompt_agent step's model, e.g. anthropic/claude-sonnet-5-5; WC3_MACRO_REASONING (default low).
- micro: WC3_MICRO_MODEL, default anthropic/claude-haiku-4-5, asked through the same endpoint; `jev` (or a
  `jev-...` model name) plays TypeSafe's Jev instead, with TYPESAFE_API_KEY; `off` plays without micro.
WC3_TURN_SECONDS (default 5, at least 5) is the game time between macro requests; WC3_MAX_GAME_SECONDS stops
playing that much game time from now (default: the match's time limit).

The prompt is not read: the game's setup (map, races, AI, time limit, mode) is the env's, from the task.
Spectators hear from it through the env's urn:rts:note/v1: its name (the models), each macro turn's plan, and its
running cost and decisions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from pathlib import Path

from agentenv_protocol.a2a_agent import (
    MCP_CONFIG_V1,
    TRAJECTORY_V1,
    AgentConfig,
    AgentEnvAgent,
    AgentIdentity,
    TaskRequest,
    TaskResult,
    Usage,
    a2a_agent,
)
from remote import RemoteGameSession, chat_micro

from agentenv_rts.session import RemoteSession, SessionError
from agentenv_rts.timeline import model_name

log = logging.getLogger(__name__)

DEFAULT_MICRO = "anthropic/claude-haiku-4-5"
RACES = {"human": "human", "orc": "orc", "undead": "undead", "night_elf": "nightelf"}
HALLS = {"htow": "human", "ogre": "orc", "unpl": "undead", "etol": "nightelf"}
# Dollars per million tokens for models wc3agent's rates.json names otherwise or not at all (it matches by prefix).
RATES = {
    "anthropic/claude-haiku-4-5": {"input": 1.0, "cached": 0.10, "cache_write": 1.25, "output": 5.0},
    "anthropic/claude-opus-5-5": {"input": 4.0, "cached": 0.20, "cache_write": 5.0, "output": 20.0},
    "anthropic/claude-sonnet-5-5": {"input": 2.0, "cached": 0.20, "cache_write": 2.5, "output": 10.0},
    "openai/gpt-6-sol": {"input": 2.0, "cached": 0.20, "cache_write": 2.0, "output": 10.0},
    "openai/gpt-6-luna": {"input": 0.10, "cached": 0.01, "cache_write": 0.125, "output": 0.50},
}
TRAJECTORY_LIMIT = 3000


class WC3Config(AgentConfig):
    model: str = "anthropic/claude-sonnet-5-5"


def endpoint(environ: dict[str, str]) -> tuple[str, str]:
    base, key = environ.get("LITELLM_BASE_URL", "").rstrip("/"), environ.get("LITELLM_API_KEY", "")
    if not base or not key:
        raise RuntimeError("no model endpoint: agent-env passes LITELLM_BASE_URL and LITELLM_API_KEY ([model] in "
                           ".agentenv/config.toml)")
    return (base if base.endswith("/v1") else base + "/v1"), key


def race_of(scenario_race: str | None, observation: dict) -> str:
    """wc3agent's name for the race this side plays: the scenario's, or for `random`, its town hall's."""
    if scenario_race in RACES:
        return RACES[scenario_race]
    hall = next((u["type_id"] for u in observation.get("units") or () if u["type_id"] in HALLS), None)
    return HALLS.get(hall, "human")


def trimmed(record: dict) -> dict:
    """A call record for the trajectory: what was decided and what it cost, without the full prompt."""
    request = record.get("request")
    out = {k: v for k, v in record.items() if k not in ("request", "response", "observation", "messages")}
    if isinstance(request, dict) and "questions" in request:
        out["questions"] = list(request["questions"])
    response = record.get("response")
    if isinstance(response, dict) and "answers" in response:
        out["answers"] = {q: a.get("choice") for q, a in response["answers"].items() if isinstance(a, dict)}
    return out


def plan_of(reply) -> str:
    """The plan a macro reply opens with ("Plan: ..."), without the orders after it."""
    found = re.search(r"Plan:\s*(.+?)(?:\n\s*\n|$)", reply if isinstance(reply, str) else "", re.S)
    return " ".join(found.group(1).split()) if found else ""


def player_name(macro: str, micro: str) -> str:
    """The models that play, as spectators read them: "Claude Sonnet 5.5 + Haiku 4.5"."""
    big = model_name(macro)
    if micro == "off":
        return big
    small = "Jev" if micro.startswith("jev") else model_name(micro)
    family = big.split()[0]
    return f"{big} + {small.removeprefix(family + ' ') if small.startswith(family + ' ') else small}"


def telling(run_log, remote: RemoteSession):
    """wc3agent's RunLog that also tells the env's spectators each macro plan and the running cost (notes are best
    effort: the game goes on without them)."""
    class SpectatorLog(run_log):
        def calls(self, records):
            super().calls(records)
            plans = [plan_of(r.get("reply")) for r in records if r["kind"] == "macro"]
            usage = self.ledger.by_model.values()
            notes = [("plan", plan, None) for plan in plans if plan]
            notes.append(("stats", "", {"cost_usd": self.ledger.total(),
                                        "decisions": self.summary["turns"] + self.summary["micro_calls"],
                                        "tokens": sum(r["input"] + r["cached"] + r["output"] for r in usage)}))
            for kind, text, data in notes:
                try:
                    remote.note(kind, text, data=data)
                except SessionError as e:
                    log.warning("spectator note: %s", e)
    return SpectatorLog


def play_game(config: WC3Config, servers: dict, environ: dict[str, str], out: Path) -> dict:
    """Play the env's game to its end with wc3agent; wc3agent's summary of it."""
    from wc3agent import play as runner
    from wc3agent.micro import agent as micro
    from wc3agent.models import cost
    from wc3agent.models.chat import ChatModel

    server = next(iter(servers.values()))
    remote = RemoteSession(server["url"].rstrip("/").removesuffix("/mcp"), headers=server.get("headers"))
    state = remote.observe()
    scenario, me = state["scenario"], state["observations"][0]
    base_url, key = endpoint(environ)
    cost.RATES.update(RATES)
    macro = ChatModel("openai", config.model, key, base_url, reasoning=environ.get("WC3_MACRO_REASONING", "low"))
    micro_model = environ.get("WC3_MICRO_MODEL") or DEFAULT_MICRO
    if micro_model.startswith("jev"):
        os.environ["TYPESAFE_DEFAULT_MODEL"] = "jev-latest" if micro_model == "jev" else micro_model
    else:
        # wc3agent turns micro on when it has a key; the chat-model transport ignores it and uses the endpoint's.
        os.environ["TYPESAFE_DEFAULT_MODEL"] = micro_model
        os.environ["TYPESAFE_API_KEY"] = "" if micro_model == "off" else "chat-model"
        micro.timed_call = chat_micro(base_url, key, micro_model)
    runner.GameSession = lambda game_config: RemoteGameSession(game_config, remote)
    runner.RunLog = telling(runner.RunLog, remote)
    try:
        remote.note("player", player_name(config.model, micro_model))
    except SessionError as e:
        log.warning("spectator note: %s", e)
    remaining = scenario["time_limit_seconds"] - me.get("game_time_seconds", 0.0)
    if environ.get("WC3_MAX_GAME_SECONDS"):
        remaining = min(remaining, float(environ["WC3_MAX_GAME_SECONDS"]))
    opponent = scenario.get("opponent_race")
    melee = runner.MeleeConfig(
        map=scenario["map"], race=race_of(scenario.get("race"), me),
        opponent_race=RACES.get(opponent) if opponent in RACES else None, difficulty=scenario["ai_difficulty"],
        max_game_minutes=max(remaining, 1.0) / 60, turn_interval_seconds=float(environ.get("WC3_TURN_SECONDS", 5)),
        hidden=True, realtime=scenario.get("mode") == "realtime", out=out)
    summary = runner.play(melee, model=macro)
    return {**summary, "micro_model": micro_model, "macro_model": config.model}


def usage_of(summary: dict, calls: int) -> Usage:
    rows = (summary.get("cost") or {}).get("by_model") or summary.get("cost") or {}
    rows = rows if isinstance(rows, dict) else {}
    tokens_in = sum(int(r.get("input", 0)) + int(r.get("cached", 0)) + int(r.get("cache_write", 0))
                    for r in rows.values() if isinstance(r, dict))
    tokens_out = sum(int(r.get("output", 0)) for r in rows.values() if isinstance(r, dict))
    dollars = [r.get("dollars") for r in rows.values() if isinstance(r, dict) and r.get("priced")]
    return Usage(tool_call_count=calls, input_tokens=tokens_in, output_tokens=tokens_out,
                 cost_usd=round(sum(dollars), 6) if dollars else None)


def result_of(summary: dict, out: Path) -> TaskResult:
    records = [json.loads(line) for line in (out / "calls.jsonl").read_text().splitlines() if line.strip()] \
        if (out / "calls.jsonl").is_file() else []
    turns, micro_calls = summary.get("turns", 0), summary.get("micro_calls", 0)
    usage = usage_of(summary, turns + micro_calls)
    priced = f", ${usage.cost_usd:.2f}" if usage.cost_usd is not None else ""
    text = (f"{summary.get('result', 'unknown')} after {summary.get('game_seconds', 0):.0f} s of game time: "
            f"{turns} macro turns ({summary['macro_model']}), {micro_calls} micro calls ({summary['micro_model']})"
            f"{priced}.")
    builder = TaskResult.builder()
    builder = builder.failed("wc3agent_error", summary.get("error") or "the game failed",
                             error_type="infra_error") if summary.get("result") == "error" else builder.succeeded()
    return (builder.add_text(text)
            .add_data({"summary": {k: v for k, v in summary.items() if k != "config"}})
            .usage(usage)
            .native_trajectory(format="wc3agent-calls", payload=[trimmed(r) for r in records[-TRAJECTORY_LIMIT:]])
            .build())


@a2a_agent(
    identity=AgentIdentity(
        name="wc3-macro-micro",
        description="Plays Warcraft III with wc3env's wc3agent: a macro model plans, a fast micro model (Jev or any "
                    "chat model) controls the army; through the env's urn:rts session, stepped or realtime.",
        version="0.1.0",
    ),
    config=WC3Config,
    extensions=(MCP_CONFIG_V1, TRAJECTORY_V1),
)
class WC3Player(AgentEnvAgent):
    async def run(self, request: TaskRequest[WC3Config]) -> TaskResult:
        if not request.mcp_servers:
            return TaskResult.failure("no_mcp_server", "No MCP server was configured for this agent.")
        with tempfile.TemporaryDirectory(prefix="wc3agent-") as tmp:
            out = Path(tmp) / "session"
            try:
                summary = await asyncio.to_thread(play_game, request.config, dict(request.mcp_servers),
                                                  dict(os.environ), out)
            except (SessionError, RuntimeError, ValueError) as e:
                return TaskResult.failure("wc3agent_error", str(e))
            return result_of(summary, out)


if __name__ == "__main__":
    WC3Player().serve()
