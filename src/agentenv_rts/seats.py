"""Several players in one RTS env: each agent plays its seat at its own address, `/seats/<agent>/...` (the MCP tools
at `/seats/<agent>/mcp`, the urn:rts session under `/seats/<agent>`), which `SeatPaths` serves from the env's routes
with the seat attached to the request; `current_seat()` reads it back in a tool or an extension. In stepping mode
`Lockstep` holds one game clock for them all."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar

from mcp.server.lowlevel.server import request_ctx

SEAT_PATH = re.compile(r"^/seats/(?P<seat>[A-Za-z0-9_.-]{1,64})(?P<rest>/.*)?$")
SEAT: ContextVar[str | None] = ContextVar("rts_seat", default=None)


class SeatPaths:
    """ASGI middleware: `/seats/<name>/<rest>` is `/<rest>` with `scope["rts_seat"] = name`."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket") and (m := SEAT_PATH.match(scope.get("path") or "")):
            rest = m["rest"] or "/"
            scope = {**scope, "path": rest, "raw_path": rest.encode(), "rts_seat": m["seat"]}
            token = SEAT.set(m["seat"])
            try:
                return await self.app(scope, receive, send)
            finally:
                SEAT.reset(token)
        return await self.app(scope, receive, send)


def current_seat() -> str | None:
    """The seat the current request came in on: from the MCP request's scope in a tool (tools run in the MCP
    session's own task), else from the request's context (an extension); None at the env's root."""
    try:
        request = request_ctx.get().request
    except LookupError:
        request = None
    scope = getattr(request, "scope", None) or {}
    return scope.get("rts_seat") or SEAT.get()


class Lockstep:
    """One game clock for several players in stepping mode. A player's step waits until every player is waiting,
    then the game moves to the nearest deadline among them, with every waiting player's orders; a player whose last
    step was more than `stall_seconds` ago (wall time) doesn't hold the others up, and is counted as stalled."""

    def __init__(self, players: list[int], stall_seconds: float,
                 advance: Callable[[dict[int, list], float], Awaitable[None]], now: Callable[[], float],
                 over: Callable[[], bool]):
        self.players, self.stall_seconds = list(players), stall_seconds
        self.advance, self.now, self.over = advance, now, over
        self.waiting: dict[int, dict] = {}
        self.active = {p: time.monotonic() for p in players}
        self.stalls = {p: 0 for p in players}
        self.changed = asyncio.Condition()

    async def step(self, player: int, batch: list, seconds: float) -> None:
        """Returns once the game has played `seconds` past now for this player, or is over."""
        deadline = self.now() + seconds
        async with self.changed:
            self.waiting[player] = {"deadline": deadline, "batch": list(batch)}
            self.active[player] = time.monotonic()
            try:
                while not self.over() and self.now() < deadline - 1e-6:
                    if not await self._move():
                        try:
                            await asyncio.wait_for(self.changed.wait(), timeout=self._next_stall())
                        except TimeoutError:
                            pass
            finally:
                self.waiting.pop(player, None)
                self.active[player] = time.monotonic()

    async def _move(self) -> bool:
        """Advances the game if everyone is waiting or stalled; True if it did."""
        now = time.monotonic()
        stalled = [p for p in self.players if p not in self.waiting and now - self.active[p] >= self.stall_seconds]
        if not self.waiting or any(p not in self.waiting and p not in stalled for p in self.players):
            return False
        target = min(w["deadline"] for w in self.waiting.values())
        batches = {p: w.pop("batch", []) for p, w in self.waiting.items()}
        for p in stalled:
            self.stalls[p] += 1
            self.active[p] = now
        await self.advance(batches, max(target - self.now(), 0.0))
        self.changed.notify_all()
        return True

    def _next_stall(self) -> float:
        now = time.monotonic()
        left = [self.stall_seconds - (now - self.active[p]) for p in self.players if p not in self.waiting]
        return max(0.05, min(left, default=self.stall_seconds))
