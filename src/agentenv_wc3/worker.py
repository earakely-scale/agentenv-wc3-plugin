"""The game worker: one wc3env GameSession, driven over JSON lines on stdin/stdout (docs/protocol.md).

In the env image it runs as Windows Python under Wine, next to the game: `wine C:\\Python311\\python.exe
worker.py`. It imports only the standard library and wc3env, so the image copies this one file rather than
installing the plugin into Wine. `--fake` plays wc3env's own stand-in for the game (wc3env.fake_server), for tests
and for developing without Warcraft III.

Requests are `{"id", "cmd", "args"}`, one per line; each gets one reply, `{"id", "ok": true, "result"}` or
`{"id", "ok": false, "error": {"code", "message"}}`. The first line out is the ready line, id 0. Anything else
the game or wc3env prints goes to stderr.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

VERSION = "0.1.0"
RACES = ("human", "orc", "undead", "night_elf", "random")
CONTROLS = ("agent", "computer")


class WorkerError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class FakeGame:
    """wc3env's in-process stand-in for a game process (as its own tests build it): FakeServer behind the RPC."""

    def __init__(self, config):
        from wc3env.fake_server import FakeServer
        from wc3env.rpc import LocalTransport, RpcClient

        self.map = config.map
        self.server = FakeServer()
        self.rpc = RpcClient(LocalTransport(self.server))
        self.startup = None
        self.closed = False

    def close(self):
        if not self.closed:
            self.closed = True
            self.rpc.quit()
            self.rpc.close()


class Worker:
    def __init__(self, fake: bool = False):
        self.fake = fake
        self.session = None
        self.wall_clock: float | None = None   # the fake game in realtime: its clock follows the wall's
        self.held = False   # a realtime game standing at its start until release

    # ---- commands ----

    def start(self, map: str, players: list[dict], step_ms: int = 1000, seed: int | None = None,
              randomize_starts: bool = False, ai_difficulty: int | None = None, render: bool = False,
              visible: bool = False, window: list[int] | None = None, mode: str = "stepping",
              ai_agents: list[int] | None = None) -> dict:
        """A new game: closes the one before, launches the game with these players, and returns every player's
        first observation and the accepted setup. In `realtime` mode the game runs on its own clock from `release`
        (`held` says it waits for one: a wc3env without hold starts it at once); `render` and `visible` draw the
        game in a window on the display, for its picture, `window` [width, height] big; `ai_agents` are agent slots
        the game's own AI plays beside."""
        from wc3env.session import GameConfig, GameSession, MatchSetup, PlayerConfig

        self.close()
        # The fake game only moves when stepped, so realtime on it is stepping by the wall time since release.
        self.wall_clock = None
        self.held = mode == "realtime" and (self.fake or "hold" in {f.name for f in dataclasses.fields(GameConfig)})
        hold = {"hold": True} if self.held and not self.fake else {}
        if self.fake and mode == "realtime":
            mode = "stepping"
        try:
            config = GameConfig(
                map=map,
                players=tuple(PlayerConfig(p["slot"], p.get("race"), p.get("control", "agent")) for p in players),
                mode=mode, step_ms=step_ms, render=render, background_visible=visible, sound=False,
                ai_difficulty=ai_difficulty, ai_agents=tuple(ai_agents or ()),
                setup=MatchSetup(seed=seed, randomize_starts=randomize_starts),
                output_dir=os.environ.get("WC3_OUTPUT_DIR") or None, **hold,
            )
        except (TypeError, ValueError) as e:
            raise WorkerError("bad_config", str(e)) from e
        self.session = GameSession(config, **({"game_factory": FakeGame} if self.fake else {}))
        observations = self.session.reset()
        if visible and window and not self.fake:
            self.session.game.resize(*window)
        return {"observations": _keyed(observations), "setup": self.session.setup, "held": self.held}

    def release(self) -> dict:
        """Starts the clock of a realtime game held at its start; a game already running is left as it is."""
        session = self._session()
        if not self.held:
            return {"released": False}
        if self.fake:
            self.wall_clock = time.monotonic()
        else:
            session.release()
        self.held = False
        return {"released": True}

    def validate(self, slot: int, actions: list[dict]) -> dict:
        """Checks a batch against the slot's newest observation as step() will, without sending it."""
        from wc3env.protocol import Observation, ProtocolError, normalize_actions, validate_actions

        observation = self._session().observations.get(slot)
        if observation is None:
            raise WorkerError("unknown_slot", f"slot {slot} is not a player in this game")
        try:
            validate_actions(Observation.of(observation), normalize_actions(actions))
        except ProtocolError as e:
            raise WorkerError("bad_actions", str(e)) from e
        return {"valid": len(actions)}

    def step(self, actions: dict, ms: int) -> dict:
        """Sends every agent slot's batch (an empty one for a slot left out) and advances the game `ms`."""
        from wc3env.protocol import ProtocolError

        session = self._session()
        batches = {slot: actions.get(str(slot), []) for slot in session.config.agent_slots}
        if self.wall_clock is not None:
            now = time.monotonic()
            ms, self.wall_clock = max(25, min(60000, round((now - self.wall_clock) * 1000) // 25 * 25)), now
        try:
            observations, done, info = session.step(batches, ms)
        except ProtocolError as e:
            raise WorkerError("bad_actions", str(e)) from e
        except ValueError as e:
            raise WorkerError("bad_args", str(e)) from e
        return {"observations": _keyed(observations), "done": done,
                "rejected": _keyed(info["rejected"]), "placements": _keyed(info["placements"]),
                "elapsed_ms": info["elapsed_ms"]}

    def observe(self) -> dict:
        """The game as it is now, also for checking the next orders: staged units are in it before any step."""
        session = self._session()
        return {"observations": _keyed(session._observe_all()), "done": session.done}

    def replay(self) -> dict:
        """The episode's native replay (.w3g), as base64. Recording stops: call it once the game is over."""
        session = self._session()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                path = session.save_replay(Path(tmp) / "game.w3g")
            except Exception as e:   # the game ended without one, or (the fake game on Linux) can't write one
                raise WorkerError("no_replay", f"{type(e).__name__}: {e}") from e
            data = path.read_bytes() if path.is_file() else b""
        if not data:
            raise WorkerError("no_replay", "the game wrote no replay")
        return {"name": "game.w3g", "base64": base64.b64encode(data).decode()}

    def debug(self, op: str, args: dict | None = None) -> dict:
        """A wc3env debug op; the fake game's `end` (with a result) is how tests finish a game."""
        session = self._session()
        if self.fake and op == "end":   # as the fake's own `end` op, with a result of the caller's choosing
            server = session.game.server
            server.world.result = (args or {}).get("result", "victory")
            server.status = "ended"
            return {"ended": True}
        return session.debug(op, **(args or {}))

    def close(self) -> dict:
        if self.session is not None:
            session, self.session = self.session, None
            session.close()
        return {"closed": True}

    def _session(self):
        if self.session is None:
            raise WorkerError("no_game", "no game has started")
        return self.session

    # ---- the loop ----

    COMMANDS = ("start", "validate", "step", "observe", "replay", "debug", "release", "close")

    def handle(self, request: dict) -> dict:
        rid = request.get("id")
        try:
            cmd, args = request.get("cmd"), request.get("args") or {}
            if cmd not in self.COMMANDS:
                raise WorkerError("unknown_command", f"unknown command {cmd!r}; valid: {', '.join(self.COMMANDS)}")
            if not isinstance(args, dict):
                raise WorkerError("bad_args", "args must be an object")
            try:
                result = getattr(self, cmd)(**args)
            except TypeError as e:
                raise WorkerError("bad_args", str(e)) from e
            return {"id": rid, "ok": True, "result": result}
        except WorkerError as e:
            return {"id": rid, "ok": False, "error": {"code": e.code, "message": e.message}}
        except Exception as e:  # the game itself failed: the env decides whether to start it again
            traceback.print_exc(file=sys.stderr)
            if self.session is not None and getattr(self.session, "_faulted", False):
                try:  # a partly played step can't be retried: the next game starts from a fresh process
                    self.close()
                except Exception:
                    self.session = None
            return {"id": rid, "ok": False, "error": {"code": "game_failed", "message": f"{type(e).__name__}: {e}"}}


def _keyed(by_slot: dict) -> dict:
    """{slot: x} with string keys, as JSON has them."""
    return {str(k): v for k, v in by_slot.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fake", action="store_true", help="play wc3env's fake game instead of Warcraft III")
    args = parser.parse_args()
    out = sys.stdout
    sys.stdout = sys.stderr  # the protocol owns stdout: stray prints go to stderr
    worker = Worker(fake=args.fake)

    def send(message: dict) -> None:
        out.write(json.dumps(message, separators=(",", ":")) + "\n")
        out.flush()

    send({"id": 0, "ok": True, "result": {"ready": True, "version": VERSION, "fake": args.fake}})
    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                request = json.loads(line)
            except ValueError as e:
                send({"id": None, "ok": False, "error": {"code": "bad_request", "message": str(e)}})
                continue
            send(worker.handle(request))
    finally:
        worker.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
