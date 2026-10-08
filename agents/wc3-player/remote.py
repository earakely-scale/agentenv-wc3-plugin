"""What lets wc3env's own agent, `wc3agent`, play an AgentEnv env unchanged: wc3env's GameSession as wc3agent's
runner uses it (reset, step, observations, debug, save_replay, config, close), over the env's urn:rts session
(agentenv_rts.session), and its micro transport (Jev's `timed_call`) answered by any chat model.

The env already holds the game the task's close_lobby created: `reset` only observes it, and the replay is the task's
to save (save_wc3_replay), so `save_replay` declines. wc3agent plays player 0; at a player slot (the env's replies
give its player_id) its player number and 0 trade places both ways, so player 0 is always its own.
"""

from __future__ import annotations

import json
import time

from agentenv_rts import choices
from agentenv_rts.session import RemoteSession, player_number


class RemoteGameSession:
    name = "agentenv"

    def __init__(self, config, remote: RemoteSession):
        self.config = config   # the wc3env GameConfig the runner built: its agent slots and step length apply
        self.remote = remote
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
        raise RuntimeError("the task saves this game's replay (save_wc3_replay)")

    def close(self) -> None:
        pass


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
