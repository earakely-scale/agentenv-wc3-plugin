"""An A2A agent in which any chat model plays Warcraft III through the env's MCP tools (get_state, act, advance and
the rest), as the vs-ai tasks ask: the prompt_agent step's prompt is the first message and its model plays, through
agent-env's model endpoint (LITELLM_BASE_URL, LITELLM_API_KEY; any model it serves that calls tools).

A plain tool loop: the model calls tools until it answers without one, and is told to keep playing (up to NUDGES
times) if the game is not over then. A long game fits: once the tool results pass TRIM_CHARS, all but the last KEEP
are trimmed, and the system prompt and the newest message are marked for prompt caching, so each turn pays for its
history once (Anthropic models through LiteLLM; other providers ignore the marks). Spectators see its model as its
name, what it writes between tool calls as its plan, and its running cost (urn:rts:note/v1). With
WC3_MAX_COST_USD it stops playing once a game has cost that much (the task's finish_match then plays the game out).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import httpx
from agentenv_protocol.a2a_agent import (
    MCP_CONFIG_V1,
    TRAJECTORY_V1,
    AgentConfig,
    AgentEnvAgent,
    AgentIdentity,
    TaskRequest,
    TaskResult,
    TextPart,
    Usage,
    a2a_agent,
)
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from agentenv_rts.session import RemoteSession, SessionError
from agentenv_rts.timeline import model_name

log = logging.getLogger(__name__)

SYSTEM = ("You play a real-time strategy game through the tools you are given; they are the only way to act. Nobody "
          "will answer questions, so never ask or wait for confirmation. Keep calling tools until the game is over, "
          "then reply with the result in a sentence.")
NUDGE = "The game is not over yet: keep playing through the tools until it is."
TRIMMED = "[An earlier result, trimmed to save room: get_state shows the game as it is now.]"
NUDGES, KEEP, TRIM_CHARS, MAX_TOKENS, RETRIES = 3, 6, 120_000, 4096, 4
MODEL_SECONDS = 180   # one model turn; a call that hangs longer is retried
STEP_WAIT_SECONDS = 900   # a tool call; longer than lockstep's stall_seconds (600), which an advance can wait out
TRAJECTORY_LIMIT = 2000
TRANSCRIPT = "wc3-llm-transcript.json"   # every message untrimmed, left with the match's agent_files


class LLMConfig(AgentConfig):
    model: str = "anthropic/claude-haiku-4-5"
    max_turns: int = 400


def endpoint(environ: dict[str, str]) -> tuple[str, str]:
    base, key = environ.get("LITELLM_BASE_URL", "").rstrip("/"), environ.get("LITELLM_API_KEY", "")
    if not base or not key:
        raise RuntimeError("no model endpoint: agent-env passes LITELLM_BASE_URL and LITELLM_API_KEY ([model] in "
                           ".agentenv/config.toml)")
    return (base if base.endswith("/v1") else base + "/v1"), key


def tools_of(listed) -> list[dict]:
    return [{"type": "function", "function": {"name": t.name, "description": (t.description or "")[:1024],
                                              "parameters": t.inputSchema or {"type": "object", "properties": {}}}}
            for t in listed]


def cached(text: str) -> list[dict]:
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def outgoing(chat: list[dict]) -> list[dict]:
    """The messages a turn sends: the system prompt and the newest message marked for caching."""
    last = chat[-1]
    if last["role"] in ("user", "tool") and isinstance(last["content"], str):
        chat = [*chat[:-1], {**last, "content": cached(last["content"])}]
    return [{"role": "system", "content": cached(SYSTEM)}, *chat]


def trim(chat: list[dict]) -> None:
    """Once the tool results pass TRIM_CHARS, all but the last KEEP become a short note, at once, so the cached
    prefix stays the same between trims."""
    results = [m for m in chat if m["role"] == "tool"]
    if sum(len(m["content"]) for m in results) > TRIM_CHARS:
        for m in results[:-KEEP]:
            m["content"] = TRIMMED


async def complete(http: httpx.AsyncClient, base: str, key: str, model: str, chat: list[dict],
                   tools: list[dict]) -> tuple[dict, dict, float]:
    """One model turn: its message, its token usage and its cost (LiteLLM's response-cost header). A call that times
    out or meets a server error is retried a few times."""
    for attempt in range(RETRIES):
        try:
            r = await http.post(f"{base}/chat/completions", headers={"Authorization": f"Bearer {key}"},
                                json={"model": model, "messages": outgoing(chat), "tools": tools,
                                      "max_tokens": MAX_TOKENS})
        except httpx.TimeoutException:
            if attempt == RETRIES - 1:
                raise
        else:
            if r.status_code not in (429, 500, 502, 503, 504) or attempt == RETRIES - 1:
                break
        await asyncio.sleep(5 * 2 ** attempt)
    r.raise_for_status()
    body = r.json()
    return body["choices"][0]["message"], body.get("usage") or {}, float(r.headers.get("x-litellm-response-cost") or 0)


def result_text(result) -> str:
    text = "\n".join(c.text for c in result.content if getattr(c, "text", None))
    return f"Error: {text}" if result.isError else text


class Player:
    """One game: the model's turns, its tool calls, and what it told the spectators."""

    def __init__(self, model: str, server: dict, environ: dict[str, str], max_turns: int):
        self.model, self.server, self.max_turns = model, server, max_turns
        self.max_cost = float(environ.get("WC3_MAX_COST_USD") or "inf")
        self.base, self.key = endpoint(environ)
        self.session = RemoteSession(server["url"].rstrip("/").removesuffix("/mcp"), headers=server.get("headers"))
        self.stats = {"turns": 0, "tool_calls": 0, "nudges": 0, "input_tokens": 0, "output_tokens": 0,
                      "cached_tokens": 0, "cost_usd": 0.0, "stopped_at_cost_cap": False}
        self.chat: list[dict] = []
        self.transcript: list[dict] = []   # copies: trim shortens the chat's own messages
        self.reply, self.named = "", False

    def say(self, message: dict) -> None:
        self.chat.append(message)
        self.transcript.append(dict(message))

    async def note(self, kind: str, text: str = "", data: dict | None = None) -> None:
        try:
            await asyncio.to_thread(self.session.note, kind, text, data=data)
        except SessionError as e:
            log.info("note: %s", e)

    async def over(self) -> tuple[bool, str]:
        try:
            state = await asyncio.to_thread(self.session.observe)
        except SessionError:
            return True, ""
        return bool(state["done"]), state.get("result") or ""

    async def play(self, prompt: str) -> None:
        try:
            await self._play(prompt)
        finally:
            await self.keep()

    async def keep(self) -> None:
        """The whole conversation, untrimmed, left with the match, so a reader sees what the model read and did."""
        body = json.dumps({"model": self.model, "stats": self.stats, "messages": self.transcript}).encode()
        try:
            await asyncio.to_thread(self.session.file, TRANSCRIPT, body, "application/json")
        except SessionError as e:
            log.info("transcript not kept: %s", e)

    async def _play(self, prompt: str) -> None:
        self.chat, self.transcript = [], []
        self.say({"role": "user", "content": prompt})
        headers = self.server.get("headers") or {}
        model_timeout = httpx.Timeout(MODEL_SECONDS, connect=30)
        tool_timeout = httpx.Timeout(STEP_WAIT_SECONDS, connect=30)
        async with httpx.AsyncClient(headers=headers, timeout=tool_timeout) as mcp_http, \
                streamable_http_client(self.server["url"], http_client=mcp_http) as (read, write, _), \
                ClientSession(read, write) as mcp, httpx.AsyncClient(timeout=model_timeout) as http:
            await mcp.initialize()
            tools = tools_of((await mcp.list_tools()).tools)
            while self.stats["turns"] < self.max_turns:
                if self.stats["cost_usd"] >= self.max_cost:
                    self.stats["stopped_at_cost_cap"] = True
                    break
                message, usage, cost = await complete(http, self.base, self.key, self.model, self.chat, tools)
                self.count(usage, cost)
                calls = message.get("tool_calls") or []
                text = (message.get("content") or "").strip()
                self.say({"role": "assistant", "content": message.get("content"), **({"tool_calls": [
                    {"id": c["id"], "type": "function", "function": {"name": c["function"]["name"],
                                                                     "arguments": c["function"]["arguments"]}}
                    for c in calls]} if calls else {})})
                if text:
                    self.reply = text
                    if calls:
                        await self.note("plan", text)
                if not calls:
                    if (await self.over())[0] or self.stats["nudges"] >= NUDGES:
                        break
                    self.stats["nudges"] += 1
                    self.say({"role": "user", "content": NUDGE})
                    continue
                for call in calls:
                    self.say({"role": "tool", "tool_call_id": call["id"],
                              "content": await self.call(mcp, call["function"])})
                trim(self.chat)
                if not self.named:   # after its first tool calls, which start the game if none has
                    self.named = True
                    await self.note("player", model_name(self.model))
                await self.report()
            await self.report()

    async def report(self) -> None:
        """Its spend so far, for spectators and the env's summary."""
        await self.note("stats", data={"cost_usd": round(self.stats["cost_usd"], 4), "decisions": self.stats["turns"],
                                       "tokens": self.stats["input_tokens"] + self.stats["output_tokens"]})

    async def call(self, mcp: ClientSession, function: dict) -> str:
        self.stats["tool_calls"] += 1
        try:
            args = json.loads(function.get("arguments") or "{}")
        except ValueError as e:
            return f"Error: the arguments are not JSON ({e})"
        return result_text(await mcp.call_tool(function["name"], args))

    def count(self, usage: dict, cost: float) -> None:
        self.stats["turns"] += 1
        self.stats["input_tokens"] += int(usage.get("prompt_tokens") or 0)
        self.stats["output_tokens"] += int(usage.get("completion_tokens") or 0)
        self.stats["cached_tokens"] += int(usage.get("cache_read_input_tokens") or 0)
        self.stats["cost_usd"] += cost


@a2a_agent(
    identity=AgentIdentity(
        name="wc3-llm",
        description="Any chat model plays Warcraft III through the env's MCP tools (get_state, act, advance), in a "
                    "plain tool loop with trimmed history and prompt caching.",
        version="0.1.0",
    ),
    config=LLMConfig,
    extensions=(MCP_CONFIG_V1, TRAJECTORY_V1),
)
class WC3LLM(AgentEnvAgent):
    async def run(self, request: TaskRequest[LLMConfig]) -> TaskResult:
        if not request.mcp_servers:
            return TaskResult.failure("no_mcp_server", "No MCP server was configured for this agent.")
        prompt = "\n".join(p.text for p in request.parts if isinstance(p, TextPart)).strip()
        try:
            player = Player(request.config.model, next(iter(request.mcp_servers.values())), dict(os.environ),
                            request.config.max_turns)
            await player.play(prompt)
        except (RuntimeError, httpx.HTTPError) as e:
            return TaskResult.failure("wc3_llm_error", f"{type(e).__name__}: {e}")
        over, result = await player.over()
        s = player.stats
        summary = {"model": player.model, **s, "cost_usd": round(s["cost_usd"], 4), "game_over": over,
                   "result": result}
        text = (f"{result or ('game over' if over else 'stopped before the game ended')}: {s['turns']} turns, "
                f"{s['tool_calls']} tool calls, ${s['cost_usd']:.2f} ({model_name(player.model)}). {player.reply}")
        return (TaskResult.builder().succeeded().add_text(text.strip()).add_data({"summary": summary})
                .usage(Usage(tool_call_count=s["tool_calls"], input_tokens=s["input_tokens"],
                             output_tokens=s["output_tokens"], cost_usd=round(s["cost_usd"], 6)))
                .native_trajectory(format="openai-chat", payload=player.chat[-TRAJECTORY_LIMIT:])
                .build())


if __name__ == "__main__":
    WC3LLM().serve()
