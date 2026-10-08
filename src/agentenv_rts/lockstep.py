"""Several players in one RTS env, stepped: `Lockstep` holds one game clock for them all. Each plays its own slot at
its own address (agentenv_game's player routing)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable


class Lockstep:
    """One game clock for several players in stepping mode. A player's step waits until every player is waiting,
    then the game moves to the nearest deadline among them, with every waiting player's orders. A player whose last
    step was more than `stall_seconds` ago (wall time) has stalled: it is counted once, and the game goes on without
    it until it steps again (an agent that crashed or stopped early holds the others up once, not every step). A
    player whose game is over (`over(player)`) neither waits nor holds anyone up; a waiting player looks again every
    `RECHECK_SECONDS`, so a game that ends outside a step (staged, or stepped by the harness) lets it go."""

    RECHECK_SECONDS = 1.0

    def __init__(self, players: list[int], stall_seconds: float,
                 advance: Callable[[dict[int, list], float], Awaitable[None]], now: Callable[[], float],
                 over: Callable[[int], bool]):
        self.players, self.stall_seconds = list(players), stall_seconds
        self.advance, self.now, self.over = advance, now, over
        self.waiting: dict[int, dict] = {}
        self.active = {p: time.monotonic() for p in players}
        self.stalls = {p: 0 for p in players}
        self.absent: set[int] = set()
        self.changed = asyncio.Condition()

    async def step(self, player: int, batch: list, seconds: float) -> None:
        """Returns once the game has played `seconds` past now for this player, or is over."""
        deadline = self.now() + seconds
        async with self.changed:
            self.waiting[player] = {"deadline": deadline, "batch": list(batch)}
            self.active[player] = time.monotonic()
            self.absent.discard(player)
            try:
                while not self.over(player) and self.now() < deadline - 1e-6:
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
        playing = [p for p in self.players if not self.over(p)]
        waiting = {p: w for p, w in self.waiting.items() if p in playing}
        stalled = [p for p in playing if p not in waiting
                   and (p in self.absent or now - self.active[p] >= self.stall_seconds)]
        if not waiting or any(p not in waiting and p not in stalled for p in playing):
            return False
        target = min(w["deadline"] for w in waiting.values())
        batches = {p: w.pop("batch", []) for p, w in waiting.items()}
        for p in stalled:
            if p not in self.absent:
                self.stalls[p] += 1
                self.absent.add(p)
        await self.advance(batches, max(target - self.now(), 0.0))
        self.changed.notify_all()
        return True

    def _next_stall(self) -> float:
        now = time.monotonic()
        left = [self.stall_seconds - (now - self.active[p]) for p in self.players
                if p not in self.waiting and p not in self.absent and not self.over(p)]
        return max(0.05, min([*left, self.RECHECK_SECONDS]))
