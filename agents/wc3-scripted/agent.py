"""An A2A agent that plays its player slot as wc3agent's scripted opponents play theirs, with no model: every
SCRIPT_EVERY_SECONDS (default 5) game seconds it attack-moves its army at the centroid of the other side's army
(SCRIPT=attack, the default), or of its workers, else at its first building (SCRIPT=raid); SCRIPT=idle gives no
orders. It gives its first order SCRIPT_AFTER_SECONDS (default 0) game seconds after its first observation.

It plays through the env's urn:rts session at its player slot's address. The other side is every player on another
team, and its units come from its observations: its player slot is an omniscient one, the match's option for harness
opponents. The prompt is not read.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass

from agentenv_protocol.a2a_agent import MCP_CONFIG_V1, AgentEnvAgent, AgentIdentity, TaskRequest, TaskResult, a2a_agent

from agentenv_rts.session import RemoteSession, SessionError, player_number

SCRIPTS = ("attack", "raid", "idle")
WORKERS = {"hpea", "opeo", "uaco", "ewsp"}
MAX_ORDERS = 64
STEP_MS = 1000


def army(units: list[dict]) -> list[dict]:
    return [u for u in units if not u["structure"] and u["type_id"] not in WORKERS]


def enemies(state: dict) -> list[int]:
    """The player numbers on another team than this one's; with no player slots (one agent at the env's root), every
    other player."""
    me, slots = player_number(state), state.get("player_slots")
    if not slots:
        return [slot for slot in state["observations"] if slot != me]
    team = next(s["team"] for s in slots if s["player_id"] == str(me))
    return [int(s["player_id"]) for s in slots if s["team"] != team]


@dataclass
class Script:
    """wc3agent's Scenario.opponent_actions for any player, with the order rounds it has sent."""

    kind: str = "attack"
    after: float = 0.0
    every: float = 5.0
    started: float | None = None
    last: float = float("-inf")
    orders: int = 0
    rounds: int = 0

    @classmethod
    def of(cls, environ: dict[str, str]) -> Script:
        kind = environ.get("SCRIPT") or "attack"
        if kind not in SCRIPTS:
            raise ValueError(f"SCRIPT must be one of {', '.join(SCRIPTS)}, not {kind!r}")
        return cls(kind, float(environ.get("SCRIPT_AFTER_SECONDS") or 0),
                   float(environ.get("SCRIPT_EVERY_SECONDS") or 5))

    def actions(self, state: dict) -> list[dict]:
        me = state["observations"][player_number(state)]
        now = me["game_time_seconds"]
        self.started = now if self.started is None else self.started
        if self.kind == "idle" or now - self.started < self.after or now - self.last < self.every:
            return []
        theirs = [u for slot in enemies(state) for u in (state["observations"].get(slot) or {}).get("units", ())]
        target = army(theirs) if self.kind == "attack" else [u for u in theirs if u["type_id"] in WORKERS]
        if not target and self.kind == "raid":
            target = [u for u in theirs if u["structure"]][:1]
        if not target:
            return []
        self.last = now
        point = {"x": sum(u["x"] for u in target) / len(target), "y": sum(u["y"] for u in target) / len(target)}
        orders = [{"unit_id": u["unit_id"], "command": "attack", "arguments": dict(point)}
                  for u in army(me["units"])[:MAX_ORDERS]]
        self.orders += len(orders)
        self.rounds += bool(orders)
        return orders


def play(remote: RemoteSession, script: Script, ms: int = STEP_MS) -> dict:
    """Plays the player slot to the game's end; the last state. In realtime a step only sends and observes, so the loop
    keeps to a step a second of wall time."""
    state = remote.observe()
    while not state["done"]:
        state = remote.step({player_number(state): script.actions(state)}, ms)
        if state["scenario"].get("mode") == "realtime":
            time.sleep(ms / 1000)
    return state


@a2a_agent(
    identity=AgentIdentity(
        name="wc3-scripted",
        description="Plays a Warcraft III player slot as wc3agent's scripted opponent: attack-moves its army at the "
                    "other side's army or workers every few game seconds, or stays idle; no model.",
        version="0.1.0",
    ),
    extensions=(MCP_CONFIG_V1,),
)
class WC3Scripted(AgentEnvAgent):
    async def run(self, request: TaskRequest) -> TaskResult:
        if not request.mcp_servers:
            return TaskResult.failure("no_mcp_server", "No MCP server was configured for this agent.")
        try:
            script = Script.of(dict(os.environ))
        except ValueError as e:
            return TaskResult.failure("bad_script", str(e))
        server = next(iter(request.mcp_servers.values()))
        remote = RemoteSession(server["url"].rstrip("/").removesuffix("/mcp"), headers=server.get("headers"))
        try:
            state = await asyncio.to_thread(play, remote, script)
        except SessionError as e:
            return TaskResult.failure("session_error", str(e))
        seconds = state["observations"][player_number(state)]["game_time_seconds"]
        return (TaskResult.builder().succeeded()
                .add_text(f"{script.kind}: {script.orders} orders in {script.rounds} rounds over {seconds:.0f} s of "
                          f"game time ({state.get('result') or 'game over'}).")
                .add_data({"script": script.kind, "orders": script.orders, "rounds": script.rounds,
                           "game_seconds": seconds, "result": state.get("result")})
                .build())


if __name__ == "__main__":
    WC3Scripted().serve()
