"""What lets wc3env's own agent, `wc3agent`, play an AgentEnv env unchanged: wc3env's GameSession as wc3agent's
runner uses it (reset, step, observations, debug, save_replay, config, close), over the env's urn:rts session
(agentenv_rts.session), its micro transport (Jev's `timed_call`) answered by any chat model, and its macro model over
LiteLLM, priced as LiteLLM prices it and with Claude's prompt cache marked.

The env already holds the game the task's close_lobby created: `reset` only observes it, and the replay is the task's
to save (save_match_files), so `save_replay` declines. wc3agent plays player 0; at a player slot (the env's replies
give its player_id) its player number and 0 trade places both ways, so player 0 is always its own.
"""

from __future__ import annotations

import http.client
import json
import time
from collections.abc import Callable
from urllib.parse import urlparse

from agentenv_rts import choices
from agentenv_rts.session import RemoteSession, player_number

COST_CAP = "cost_cap"   # the result wc3agent's run ends with once the game cost its cap: the env's game goes on
RETRIES = 3


class RemoteGameSession:
    name = "agentenv"

    def __init__(self, config, remote: RemoteSession, capped: Callable[[], bool] = lambda: False):
        self.config = config   # the wc3env GameConfig the runner built: its agent slots and step length apply
        self.remote = remote
        self.capped = capped
        self.observations: dict[int, dict] = {}
        self.done = False
        self.you = 0

    def reset(self) -> dict[int, dict]:
        state = self.remote.observe()
        self.you = player_number(state)
        self.observations, self.done = self.player(state["observations"]), state["done"]
        return self.observations

    def step(self, actions: dict[int, list], ms: int | None = None):
        result = self.remote.step(self.player(actions), ms if ms is not None else self.config.step_ms)
        self.observations, self.done = self.player(result["observations"]), result["done"]
        # wc3agent stops when its side's observation has a result; an env that decides the game (a duel's strength
        # ratio) says so in its reply, while the game's own observation has none and the clock stops
        ended = COST_CAP if self.capped() else (result.get("result") or "game_over") if self.done else None
        if ended and not self.observations[0].get("result"):
            self.observations[0] = {**self.observations[0], "result": ended}
        rejected, placements = self.player(result["rejected"]), self.player(result["placements"])
        for slot in actions:
            rejected.setdefault(slot, [])
            placements.setdefault(slot, [])
        info = {"rejected": rejected, "placements": placements, "elapsed_ms": result.get("elapsed_ms"),
                "step_reason": "game_over" if self.done else "target"}
        return self.observations, self.done, info

    def player(self, by_slot: dict[int, object]) -> dict[int, object]:
        return {0 if slot == self.you else self.you if slot == 0 else slot: v for slot, v in by_slot.items()}

    def debug(self, op: str, **args) -> dict:
        return self.remote.debug(op, **args)

    def save_replay(self, path):
        raise RuntimeError("the task saves this game's replay (save_match_files)")

    def close(self) -> None:
        pass


def litellm_chat(chat_model, model: str, api_key: str, base_url: str, reasoning: str):
    """wc3agent's macro ChatModel (`chat_model`, its class) on LiteLLM's OpenAI-compatible endpoint: a Claude model's
    system prompt and newest message are marked for prompt caching, as wc3agent marks them on Anthropic's own API,
    and each call's usage carries `litellm_cost`, its dollars as LiteLLM reports them."""

    class LiteLLMChat(chat_model):
        def complete(self, system, messages):
            cached = "claude" in self.model
            *earlier, last = messages
            body = {
                "model": self.model,
                "max_completion_tokens": self.max_tokens,
                "reasoning_effort": self.reasoning,
                "messages": [
                    {"role": "system", "content": [mark(system)] if cached else system},
                    *earlier,
                    {**last, "content": [mark(last["content"])]} if cached else last,
                ],
            }
            url = urlparse(self.base_url.rstrip("/"))
            connect = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
            started, waited = time.perf_counter(), 0.0
            for attempt in range(RETRIES + 1):
                connection = connect(url.netloc, timeout=self.timeout)
                try:
                    connection.request("POST", url.path + "/chat/completions", json.dumps(body).encode(),
                                       {"Content-Type": "application/json",
                                        "Authorization": f"Bearer {self.api_key}"})
                    response = connection.getresponse()
                    raw = response.read().decode("utf-8").replace(self.api_key, "[REDACTED]")
                    cost = response.getheader("x-litellm-response-cost")
                except (OSError, http.client.HTTPException):   # a dropped or garbled connection: ask again
                    if attempt == RETRIES:
                        raise
                    waited += 2.0 * (attempt + 1)
                    time.sleep(2.0 * (attempt + 1))
                    continue
                finally:
                    connection.close()
                if response.status not in (429, 500, 502, 503, 529) or attempt == RETRIES:
                    break
                pause = float(response.getheader("retry-after") or 0) or 2.0 * (attempt + 1)
                waited += pause
                time.sleep(pause)
            record = {"request": body, "status": response.status, "seconds": round(time.perf_counter() - started, 3),
                      "attempts": attempt + 1, "waited": round(waited, 3)}
            if response.status != 200:
                raise RuntimeError(f"litellm HTTP {response.status}: {raw[:500]}")
            data = json.loads(raw)
            record["response"] = data
            record["usage"] = {**(data.get("usage") or {}), **({"litellm_cost": float(cost)} if cost else {})}
            return data["choices"][0]["message"]["content"] or "", record

    return LiteLLMChat("openai", model, api_key, base_url, reasoning=reasoning)


def mark(text: str) -> dict:
    return {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}


def litellm_priced(cost):
    """wc3agent's cost(model, usage), preferring the dollars LiteLLM reported for the call over the rates table, so a
    model the table doesn't name is priced too."""

    def priced(model, usage):
        reported = (usage or {}).get("litellm_cost")
        return cost(model, usage) if reported is None else round(float(reported), 6)

    return priced


def chat_micro(base_url: str, api_key: str, model: str):
    """A stand-in for wc3agent.models.jev.timed_call that asks `model` on an OpenAI-compatible endpoint, filling
    the same record (request, status, response, latency) so wc3agent's records and reports work as they do on Jev."""

    def timed_call(payload, key, record):
        record["request"] = payload
        record["request_bytes"] = len(json.dumps(payload).encode())
        started = time.perf_counter()
        try:
            response, call = choices.answer(payload, base_url=base_url, api_key=api_key, model=model)
            record.update(call, response=response, probability_warnings=[], contract_valid=True)
        finally:
            record["latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
        return response

    return timed_call
