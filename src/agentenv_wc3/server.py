"""Warcraft III as an AgentEnv environment: an agent plays one side of a melee game against the game's own AI,
through raw wc3env orders (docs/tools.md).

The game runs in a worker process (worker.py): in the image, Windows Python under Wine driving wc3env, which
holds the game's clock. Time only passes when the agent calls `advance`, so the agent may think as long as it
likes between steps. Orders given with `act` wait in a queue and go to the game with the next `advance`.

A program plays the same game through the `urn:rts:*` session extensions (agentenv_rts.session): raw observations
in, raw wc3env actions out, as wc3env's own `wc3agent` plays (agents/wc3-player). In `realtime` mode the game runs on
its own clock and a step only sends orders and observes. Spectators follow the game at `/live` (agentenv_rts.live),
and `urn:rts:recording/v1` gives its recording once it is played. With `client_view` the game also draws itself in a
window on the container's display: the live page shows that picture (`/live/client`), a director points the camera
at the agent's fights and key moments (frames.Director), and the recording has the match's video
(agentenv_rts.display). Spectators read a feed of what happened (frames.Feed), and what players tell them through
`urn:rts:note/v1`: their plans (also shown in the game's picture), their names and their running costs.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import math
import os
import shlex
import sys
import textwrap
import time
from pathlib import Path
from typing import Annotated, Any, Literal

from agentenv_protocol import (
    AgentEnvEnvironment,
    DataPart,
    add_data,
    environment_card,
    extension,
    get_data,
    reset_data,
    tool,
)
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from agentenv_rts import display as rts_display
from agentenv_rts import live as rts_live
from agentenv_rts import recording as rts_recording
from agentenv_rts.session import DEBUG, NOTE, NOTE_KINDS, OBSERVE, STEP
from agentenv_rts.timeline import Timeline

from . import frames, render
from .bridge import DEAD, Bridge, WorkerError

log = logging.getLogger(__name__)

AGENT, COMPUTER = 0, 1   # the agent plays slot 0; the game's AI, slot 1
RACES = ("human", "orc", "undead", "night_elf", "random")
DIFFICULTIES = {"easy": 0, "normal": 1, "insane": 2}
COMMANDS = ("move", "stop", "attack", "smart", "harvest", "build", "train", "research", "learn", "cast", "use_item",
            "drop_item", "select", "buy", "revive")
LICENSE_FILES = ("roc.w3k", "tft.w3k")
WORKER = Path(__file__).with_name("worker.py")
# Windows Python under Wine, on this file's worker.py: Wine's drive Z: is the Linux root.
DEFAULT_WORKER_CMD = ["wine", "C:\\Python311\\python.exe", "Z:" + str(WORKER).replace("/", "\\")]
MAX_ADVANCE_SECONDS = 60
DEFAULT_SCENARIO = {"map": "(2)EchoIsles.w3x", "race": "human", "opponent_race": "orc", "ai_difficulty": "normal",
                    "seed": None, "randomize_starts": False, "time_limit_seconds": 1200, "mode": "stepping",
                    "allow_debug": False, "client_view": False, "labels": None}
MODES = ("stepping", "realtime")
# Debug ops that change only how the game is shown or how fast its clock runs: any session may send them (wc3agent
# sets the speed when it starts). Every other op stages the game, so it needs the match's allow_debug.
VIEW_DEBUG_OPS = ("speed", "camera", "overlay", "render")
DEFAULT_STEP_MS = 1000
WINDOW_NAME = "Warcraft III"
# The game's window with client_view (its frame: the picture inside is 8 by 34 smaller, 1280x720 here).
WINDOW = [int(n) for n in os.environ.get("WC3_WINDOW", "1288x754").split("x")]
CLIENT_DIR = Path(os.environ.get("WC3_CLIENT_DIR", "/tmp/wc3-client"))
PAN_SECONDS, PAN_STEPS, PAN_MAX = 0.6, 8, 4000.0   # the camera eases over this; farther than PAN_MAX it cuts
NOTE_CHARS = 400
OVERLAY = {"x": -0.30, "y": 0.60, "seconds": 9}   # where and how long a plan shows in the game's picture

NEW_GAME_EXTENSION = "urn:wc3:new-game/v1"
REPLAY_EXTENSION = "urn:wc3:replay/v1"
IDLE_EXTENSION = "urn:wc3:idle/v1"


class Action(BaseModel):
    """One order to one of your units, as wc3env takes it (docs/tools.md has every command's arguments)."""

    model_config = ConfigDict(extra="forbid")
    unit_id: Annotated[int, Field(description="The unit or structure that acts: an id from get_state or list_units.")]
    command: Annotated[Literal[COMMANDS], Field(description="What it does.")]
    arguments: Annotated[dict[str, Any], Field(
        description='The command\'s arguments, e.g. {"x": 100, "y": -200} for move, {"target_id": 1234} for '
                    'attack or harvest, {"type_id": "Footman"} for train, {"type_id": "Farm", "x": .., "y": .., '
                    '"auto_place": true} for build, {"order": "Blizzard", "x": .., "y": ..} for cast. Type names '
                    'or ids both work. Add "queued": true to append to the unit\'s orders instead of replacing '
                    "them.")] = {}


def scenario_from_env() -> dict:
    """The default game, from WC3_* environment variables (tests and `agent-env wc3 serve`)."""
    scenario = dict(DEFAULT_SCENARIO)
    for key, cast in (("map", str), ("race", str), ("opponent_race", str), ("ai_difficulty", str), ("seed", int),
                      ("time_limit_seconds", int)):
        if (value := os.environ.get(f"WC3_{key.upper()}")) not in (None, ""):
            scenario[key] = cast(value)
    return check_scenario(scenario)


def check_scenario(s: dict) -> dict:
    if unknown := sorted(set(s) - set(DEFAULT_SCENARIO)):
        raise ValueError(f"unknown scenario keys {unknown}; valid: {', '.join(DEFAULT_SCENARIO)}")
    for key in ("race", "opponent_race"):
        if s[key] not in RACES:
            raise ValueError(f"{key} must be one of {', '.join(RACES)}, got {s[key]!r}")
    if s["ai_difficulty"] not in DIFFICULTIES:
        raise ValueError(f"ai_difficulty must be one of {', '.join(DIFFICULTIES)}, got {s['ai_difficulty']!r}")
    if s["seed"] is not None and not (isinstance(s["seed"], int) and 0 <= s["seed"] <= 0x7FFFFFFF):
        raise ValueError("seed must be an integer in 0..2147483647, or null for a new one each game")
    if not isinstance(s["time_limit_seconds"], int) or not 60 <= s["time_limit_seconds"] <= 4 * 3600:
        raise ValueError("time_limit_seconds must be an integer from 60 to 14400 (game seconds)")
    if not isinstance(s["map"], str) or not s["map"].strip():
        raise ValueError("map must be a map name or path, e.g. (2)EchoIsles.w3x")
    if s["mode"] not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}, got {s['mode']!r}")
    for key in ("allow_debug", "client_view"):
        if not isinstance(s[key], bool):
            raise ValueError(f"{key} must be true or false")
    if s["labels"] is not None and not (isinstance(s["labels"], dict) and all(
            str(k).isdigit() and isinstance(v, str) for k, v in s["labels"].items())):
        raise ValueError('labels must name players by slot, e.g. {"0": "Claude Opus 5.5", "1": "Orc AI"}')
    return s


def attach_license(files: dict[str, str], store: Path, game_dir: Path) -> None:
    """Writes the activation files the task sent (base64) to a private directory and links them into the game's
    folder, as wc3env's docker/license.py does with a mounted directory: their contents never enter the image."""
    if missing := [n for n in LICENSE_FILES if not files.get(n)]:
        raise ValueError(f"license is missing {', '.join(missing)}: send both of {', '.join(LICENSE_FILES)}")
    store.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in LICENSE_FILES:
        try:
            data = base64.b64decode(files[name], validate=True)
        except ValueError as e:
            raise ValueError(f"license {name} is not base64") from e
        if not data:
            raise ValueError(f"license {name} is empty")
        source = store / name
        source.write_bytes(data)
        source.chmod(0o600)
        link_license(source, game_dir / name)


def link_license(source: Path, target: Path) -> None:
    if target.is_symlink():
        if target.resolve() == source.resolve():
            return
        target.unlink()
    elif target.exists():
        raise ValueError(f"the image has {target.name} baked in; build it without the activation files")
    target.symlink_to(source)


@environment_card(name="wc3")
class WC3Env(AgentEnvEnvironment):
    def __init__(self):
        super().__init__()
        given = os.environ.get("WC3_WORKER_CMD")   # a shell-quoted command line, e.g. for the fake game
        self.worker_cmd = shlex.split(given) if given else list(DEFAULT_WORKER_CMD)
        self.fake = "--fake" in self.worker_cmd
        self.game_dir = Path(os.environ.get("WC3_LINUX_GAME_DIR", "/opt/game"))
        self.license_store = Path(os.environ.get("WC3_LICENSE_STORE", "/tmp/wc3-license"))
        self.license_mount = Path(os.environ.get("WC3_LICENSE_DIR", "/run/wc3-license"))
        self.scenario = scenario_from_env()
        self.ref = render.Reference.load()
        self.lock = asyncio.Lock()
        self.bridge: Bridge | None = None
        self.setup: dict | None = None
        self.obs: dict[int, dict] = {}
        self.queue: list[dict] = []
        self.recent: list[str] = []
        self.names = render.Names()
        self.result = ""
        self.failed: str | None = None
        self.timeline: Timeline | None = None
        self.capture: rts_display.Capture | None = None
        self.camera: tuple[float, float] | None = None
        self.director = frames.Director(AGENT)
        self.feed: frames.Feed | None = None
        self.notes: list[dict] = []
        self.agents: dict[int, dict] = {}
        self.stats = {"games": 0, "tool_calls": 0, "invalid_calls": 0, "advances": 0, "orders_sent": 0,
                      "orders_rejected": 0, "extension_calls": 0, "idle_seconds": 0, "session_steps": 0}

    # ---- the game ----

    def _license_ready(self) -> bool:
        if self.fake:
            return True
        if not all((self.game_dir / n).exists() for n in LICENSE_FILES) and self.license_mount.is_dir():
            for name in LICENSE_FILES:   # `docker run` with the directory mounted, as wc3env's own image takes it
                if (self.license_mount / name).is_file():
                    link_license(self.license_mount / name, self.game_dir / name)
        return all((self.game_dir / n).exists() for n in LICENSE_FILES)

    async def _new_game(self, scenario: dict) -> None:
        if not self._license_ready():
            raise WorkerError("no_license", "the game needs your Warcraft III activation files (roc.w3k, tft.w3k): "
                                            "the wc3_match task step sends them from your license directory "
                                            "(agent-env wc3 setup --help)")
        await self._drop_capture()
        if self.bridge is None or not self.bridge.alive:
            self.bridge = Bridge(self.worker_cmd)
            await self.bridge.start()
        players = [{"slot": AGENT, "race": scenario["race"]},
                   {"slot": COMPUTER, "race": scenario["opponent_race"], "control": "computer"}]
        result = await self.bridge.call("start", map=scenario["map"], players=players,
                                        seed=scenario["seed"], randomize_starts=scenario["randomize_starts"],
                                        ai_difficulty=DIFFICULTIES[scenario["ai_difficulty"]],
                                        mode=scenario["mode"], render=scenario["client_view"],
                                        visible=scenario["client_view"],
                                        window=WINDOW if scenario["client_view"] else None)
        self.scenario, self.setup = scenario, result.get("setup")
        self.obs = {int(k): v for k, v in result["observations"].items()}
        self.queue, self.recent, self.result, self.failed = [], [], "", None
        self.names = render.Names()
        for o in self.obs.values():
            self.names.see(o)
        self.stats["games"] += 1
        labels = {int(k): v for k, v in (scenario["labels"] or {}).items()}
        self.timeline = Timeline(frames.static(f"g-{self.stats['games']}", scenario, self.setup, self.obs,
                                               self.ref, labels))
        self.feed = frames.Feed(self.ref, self.timeline.static)
        self.camera, self.director, self.notes, self.agents = None, frames.Director(AGENT), [], {}
        if scenario["client_view"]:
            await self._start_capture()
        self._record()
        log.warning("NEW GAME %s: %s vs the %s %s AI, seed %s", scenario["map"], scenario["race"],
                    scenario["ai_difficulty"], scenario["opponent_race"], (self.setup or {}).get("match_setup"))

    async def _ensure_game(self) -> None:
        if self.failed:
            raise WorkerError("game_failed", f"the game stopped working: {self.failed}. A new game (data/reset or "
                                             "the new-game extension) starts it again.")
        if not self.obs:
            await self._new_game(self.scenario)

    def _me(self) -> dict:
        return self.obs[AGENT]

    @property
    def game_over(self) -> bool:
        return bool(self.result)

    def _seconds(self) -> float:
        return self._me().get("game_time_seconds", 0.0)

    def _record(self) -> None:
        """A spectator frame of the game as it is now, with the feed's new events and the players' new notes."""
        if self.timeline is None or not self.obs:
            return
        notes, self.notes = self.notes, []
        wall = time.monotonic() - self.capture.started if self.capture is not None and self.capture.running else None
        frame = frames.frame(self.obs, self.result, self.feed.see(self.obs), self.ref, notes, wall)
        for slot, stats in self.agents.items():
            frame["players"].setdefault(str(slot), {})["agent"] = stats
        self.timeline.add(frame)

    def _observed(self, result: dict) -> None:
        """A step's observations become the game's state: its result (or the time limit) and a spectator frame."""
        self.obs = {int(k): v for k, v in result["observations"].items()}
        me = self._me()
        self.result = me.get("result") or ("time_limit" if self._seconds() >= self.scenario["time_limit_seconds"]
                                           else "")

    async def _start_capture(self) -> None:
        """The game's picture, from its window on the container's display, for the live page and the recording."""
        if self.fake:
            log.warning("client_view: the fake game draws nothing; the live page shows the map only")
            return
        display, region, seen = os.environ.get("DISPLAY", ":99"), None, None
        for _ in range(20):   # until the window is there and done resizing
            region = await asyncio.to_thread(rts_display.window_region, display, WINDOW_NAME)
            if region is not None and region == seen:
                break
            seen = region
            await asyncio.sleep(0.5)
        if region is None:
            log.warning("client_view: no %r window on %s, so no picture this game", WINDOW_NAME, display)
            return
        capture = rts_display.Capture.of_display(display, region, CLIENT_DIR / f"g-{self.stats['games']}.mp4")
        try:
            await asyncio.to_thread(capture.start)
        except rts_display.DisplayError as e:
            log.warning("client_view: %s", e)
            return
        self.capture = capture
        log.warning("CLIENT VIEW %dx%d at %d,%d of %s", region[2], region[3], region[0], region[1], display)

    async def _drop_capture(self) -> None:
        capture, self.capture = self.capture, None
        if capture is not None:
            await asyncio.to_thread(capture.stop)
            capture.path.unlink(missing_ok=True)

    async def _aim(self) -> None:
        """With the game's picture on, the director picks the camera's next spot (frames.Director)."""
        if self.capture is None or not self.capture.running or self.feed is None:
            return
        if (spot := self.director.choose(self._me(), self.feed.moments, time.monotonic())) is not None:
            await self._pan(spot)

    async def _pan(self, spot: tuple[float, float]) -> None:
        """Eases the camera to `spot`; a far one is a cut, since a long pan only blurs the map."""
        start = self.camera
        steps = PAN_STEPS if start is not None and math.dist(start, spot) <= PAN_MAX else 1
        for i in range(1, steps + 1):
            k = i / steps
            k = k * k * (3 - 2 * k)
            x, y = (spot if start is None or steps == 1 else
                    (start[0] + (spot[0] - start[0]) * k, start[1] + (spot[1] - start[1]) * k))
            try:
                await self.bridge.call("debug", op="camera", args={"x": round(x), "y": round(y)})
            except WorkerError as e:
                log.warning("camera: %s", e.message)
                return
            if i < steps:
                await asyncio.sleep(PAN_SECONDS / steps)
        self.camera = spot

    def _footer(self) -> str:
        return render.footer(self._me(), limit=self.scenario["time_limit_seconds"], queued=len(self.queue),
                             result=self.result)

    async def _run(self, body):
        """A tool call: one at a time, on a started game; game errors become tool errors the agent can read."""
        async with self.lock:
            self.stats["tool_calls"] += 1
            try:
                await self._ensure_game()
                return await body()
            except WorkerError as e:
                if e.code in DEAD or e.code == "game_failed":
                    self.failed = self.failed or e.message
                self.stats["invalid_calls"] += 1
                raise ToolError(f"{e.code}: {e.message}") from e
            except ValueError as e:
                self.stats["invalid_calls"] += 1
                raise ToolError(f"bad_arguments: {e}") from e

    # ---- tools ----
    # No return annotations: `-> str` makes FastMCP send every result twice (text and structuredContent).

    @tool()
    async def get_state(self):
        """Your whole situation on one page: game time and the time limit, gold, lumber and food, your units by
        type, idle workers, every structure and what it is producing, enemies in view, nearby gold mines, the
        map's start locations, and what happened during your last advance. Time is frozen until you call advance.
        Lost track? Call get_state."""
        async def body():
            return render.state(self._me(), self.ref, me=AGENT, race=self.scenario["race"],
                                map_name=self.scenario["map"],
                                limit=self.scenario["time_limit_seconds"], queued=len(self.queue),
                                result=self.result, events=self.recent)
        return await self._run(body)

    @tool()
    async def list_units(self, who: Annotated[Literal["own", "enemy", "neutral", "inside"], Field(
                             description="own (default): yours on the map; enemy: enemies in view; neutral: neutral "
                                         "units in view (gold mines, creeps, shops); inside: yours inside a mine, "
                                         "building or transport.")] = "own",
                         type: Annotated[str | None, Field(description='Only this type, e.g. "Peasant" or "hpea".')]
                         = None,
                         near: Annotated[int | None, Field(description="Sort by distance from this unit's id.")] = None,
                         radius: Annotated[float | None, Field(gt=0, description="With near: only units this close."
                                                               )] = None,
                         details: Annotated[bool, Field(description="Also what each of yours can train, build, "
                                                                    "research and cast.")] = False,
                         limit: Annotated[int, Field(ge=1, le=200)] = 40):
        """One line per unit: id, type, position, hp (and mana, level), and for yours its current order, or for a
        structure what it is producing. Ids are what act takes."""
        async def body():
            center = self._unit(near) if near is not None else None
            return render.units_list(self._me(), self.ref, who=who, type_filter=type, near=center, radius=radius,
                                     details=details, limit=limit)
        return await self._run(body)

    @tool()
    async def resources(self, near: Annotated[int | None, Field(
                            description="The unit to measure from (default: your first structure).")] = None,
                        radius: Annotated[float, Field(gt=0, le=4000)] = 1200):
        """Gold mines in view, the nearest trees (harvest a tree's id for lumber) and items on the ground."""
        async def body():
            me = self._me()
            center = self._unit(near) if near is not None else next(
                (u for u in me.get("units") or () if u.get("structure")), (me.get("units") or [None])[0])
            if center is None:
                return "You have no units left."
            return render.resources(me, self.ref, near=center, radius=radius, limit=12)
        return await self._run(body)

    @tool()
    async def lookup(self, query: Annotated[str, Field(description='A unit, building, upgrade, item or ability, by '
                                                                   'name or id: "Footman", "Barracks", "Rhde".')]):
        """The game's facts about a type: cost, build time, hp, damage, what it requires, and what it trains, builds
        or researches; for an ability, its cast order and target. No game time passes."""
        return await self._run(lambda: _now(render.lookup(query, self.ref)))

    @tool()
    async def act(self, actions: Annotated[list[Action], Field(min_length=1, max_length=200)],
                  clear: Annotated[bool, Field(description="Drop the orders already queued first.")] = False):
        """Queue orders for your units. Nothing happens until you call advance, which sends the queue and lets game
        time pass; orders are checked here (your unit, a known target, valid arguments) and the game may still
        refuse them then (not enough gold, no room to build). Commands and their arguments:
        move/attack {x, y} or attack {target_id}; smart {target_id} (right-click); harvest {target_id}: a gold
        mine or a tree; build {type_id, x, y, auto_place: true} (the game finds a free site near x,y) or
        {type_id, target_id}; train/research {type_id} on a structure; learn {ability_id} on a hero; cast {order,
        optionally x, y or target_id}; use_item {slot 0-5, optionally a target}; drop_item {slot, ...}; buy
        {shop_id, item_type_id}; revive {target_id} on an altar; stop; select."""
        async def body():
            if self.game_over:
                raise WorkerError("game_over", f"the game is over ({self.result})")
            batch = [self._resolve(a.model_dump()) for a in actions]
            await self.bridge.call("validate", slot=AGENT, actions=batch)
            self.queue = batch if clear else self.queue + batch
            return (f"Queued {len(batch)} order{'s' if len(batch) != 1 else ''}: "
                    + "; ".join(self._describe(a) for a in batch)
                    + f". {len(self.queue)} in the queue, sent with your next advance.\n" + self._footer())
        return await self._run(body)

    @tool()
    async def advance(self, seconds: Annotated[int, Field(ge=1, le=MAX_ADVANCE_SECONDS, description=(
                          "Game seconds to play, 1-60. Short steps for fights, longer ones while the economy runs."))]
                      = 5,
                      actions: Annotated[list[Action] | None, Field(
                          description="More orders to send with the queue, as act takes them.")] = None):
        """Send the queued orders (and any given here) and let the game run: the only way time passes. Returns
        what the game refused, the sites it chose for buildings, what happened, and the new state's footer."""
        async def body():
            if self.game_over:
                raise WorkerError("game_over", f"the game is over ({self.result}); get_state shows the end")
            extra = [self._resolve(a.model_dump()) for a in actions or ()]
            if extra:
                await self.bridge.call("validate", slot=AGENT, actions=extra)
            batch, dropped = await self._still_valid(self.queue + extra)
            limit = self.scenario["time_limit_seconds"]
            start = self._seconds()
            ms = max(25, min(seconds * 1000, round((limit - start) * 1000) // 25 * 25))
            result = await self.bridge.call("step", actions={str(AGENT): batch}, ms=ms)
            self.queue = []
            self._observed(result)
            self.stats["advances"] += 1
            self.stats["orders_sent"] += len(batch)
            me = self._me()
            rejected = result.get("rejected", {}).get(str(AGENT), [])
            self.stats["orders_rejected"] += len(rejected)
            events = render.events_text(me.get("events") or [], self.ref, self.names, AGENT)
            for o in self.obs.values():
                self.names.see(o)
            self.recent = events
            self._record()
            await self._aim()
            lines = [f"Advanced {render.clock(start)} → {render.clock(self._seconds())}."]
            if dropped:
                lines.append(f"Dropped {len(dropped)} queued order{'s' if len(dropped) != 1 else ''} that no longer "
                             "applied:")
                lines += [f"  {self._describe(a)}: {why}" for a, why in dropped]
            if batch:
                lines.append(f"Sent {len(batch)} order{'s' if len(batch) != 1 else ''}"
                             + (f"; the game refused {len(rejected)}:" if rejected else "."))
                lines += [f"  #{r['index'] + 1} {self._describe(batch[r['index']])}: {r['reason']}"
                          for r in rejected if 0 <= r.get("index", -1) < len(batch)]
            for p in result.get("placements", {}).get(str(AGENT), []):
                if 0 <= p.get("index", -1) < len(batch):
                    lines.append(f"  site for #{p['index'] + 1} {self._describe(batch[p['index']])}: "
                                 f"({p['x']:.0f},{p['y']:.0f})")
            if events:
                lines.append("What happened:")
                lines += [f"  {e}" for e in events]
            idle = [u["unit_id"] for u in me.get("units") or ()
                    if u["type_id"] in render.WORKERS and not u.get("order")]
            if idle:
                lines.append("IDLE WORKERS: " + ", ".join(map(str, idle)))
            if self.result:
                lines.append(f"GAME OVER: {render.RESULTS.get(self.result, self.result)}. Reply with your result.")
            lines.append(self._footer())
            return "\n".join(lines)
        return await self._run(body)

    # ---- orders ----

    async def _still_valid(self, batch: list[dict]) -> tuple[list[dict], list[tuple[dict, str]]]:
        """The batch without the orders that stopped applying since they were queued (the unit died, the target
        went out of view): wc3env refuses a whole batch for one of them. Returns it and the dropped orders."""
        if not batch:
            return batch, []
        try:
            await self.bridge.call("validate", slot=AGENT, actions=batch)
            return batch, []
        except WorkerError as e:
            if e.code != "bad_actions":
                raise
        kept, dropped = [], []
        for action in batch:
            try:
                await self.bridge.call("validate", slot=AGENT, actions=[action])
                kept.append(action)
            except WorkerError as e:
                if e.code != "bad_actions":
                    raise
                dropped.append((action, e.message))
        return kept, dropped

    def _unit(self, unit_id: int) -> dict:
        me = self._me()
        for u in [*(me.get("units") or ()), *(me.get("visible_enemies") or ()), *(me.get("inside") or ())]:
            if u["unit_id"] == unit_id:
                return u
        raise WorkerError("unknown_unit", f"no unit {unit_id} in view; list_units shows the ids")

    def _resolve(self, action: dict) -> dict:
        """The action as wc3env takes it: type, ability and order names turned into ids, `arguments` dropped when
        empty."""
        args = dict(action.get("arguments") or {})
        command = action["command"]
        if command in ("build", "train", "research") and isinstance(args.get("type_id"), str):
            args["type_id"] = self.ref.type_id(args["type_id"])
        if command == "buy" and isinstance(args.get("item_type_id"), str):
            hits = self.ref.find(args["item_type_id"], ("item",))
            args["item_type_id"] = hits[0][1] if len(hits) == 1 else args["item_type_id"]
        if command in ("cast", "learn"):
            own = next((u for u in self._me().get("units") or () if u["unit_id"] == action["unit_id"]), None)
            abilities = [a["ability_id"] for a in (own or {}).get("abilities") or ()]
            if command == "cast" and isinstance(args.get("order"), str):
                args["order"] = self.ref.cast_order(args["order"], abilities)
            if command == "learn" and isinstance(args.get("ability_id"), str) and own is not None:
                skills = (self.ref.units.get(own["type_id"]) or {}).get("potential_hero_abilities") or []
                named = [s for s in skills if self.ref.name(s).lower() == args["ability_id"].strip().lower()]
                args["ability_id"] = named[0] if named else args["ability_id"]
        return {"unit_id": action["unit_id"], "command": command, **({"arguments": args} if args else {})}

    def _describe(self, action: dict) -> str:
        args = action.get("arguments") or {}
        what = " ".join(self.ref.name(args[k]) for k in ("type_id", "item_type_id", "ability_id") if k in args)
        where = (f" at ({args['x']:.0f},{args['y']:.0f})" if isinstance(args.get("x"), int | float)
                 and isinstance(args.get("y"), int | float) else "")
        target = f" → {self.names.of(args['target_id'], self.ref)}" if args.get("target_id") else ""
        order = f" {args['order']}" if args.get("order") else ""
        unit = self.names.of(action["unit_id"], self.ref)
        return f"{unit} {action['command']}{order}{(' ' + what) if what else ''}{where}{target}"

    # ---- data plane ----

    @reset_data
    async def data_reset(self) -> None:
        async with self.lock:
            await self._new_game(self.scenario)

    @add_data
    async def data_add(self, parts: list) -> None:
        updates = [p.data["scenario"] for p in parts if isinstance(p, DataPart) and "scenario" in p.data]
        if not updates:
            raise ValueError('data/add expects a DataPart {"scenario": {...}}')
        scenario = dict(self.scenario)
        for update in updates:
            scenario = check_scenario({**scenario, **update})
        async with self.lock:
            await self._new_game(scenario)

    @get_data
    async def data_get(self) -> list[DataPart]:
        async with self.lock:
            if not self.obs and not self.failed:
                try:
                    await self._ensure_game()
                except WorkerError as e:
                    self.failed = e.message
            me, ai = self.obs.get(AGENT) or {}, self.obs.get(COMPUTER) or {}
            units = me.get("units") or []
            summary = {
                "game_time_seconds": me.get("game_time_seconds", 0.0),
                "time_limit_seconds": self.scenario["time_limit_seconds"],
                "game_over": self.game_over, "result": self.result,
                "map": self.scenario["map"], "race": self.scenario["race"],
                "opponent_race": self.scenario["opponent_race"], "ai_difficulty": self.scenario["ai_difficulty"],
                "seed": ((self.setup or {}).get("match_setup") or {}).get("seed", self.scenario["seed"]),
                "score": me.get("score") or {}, "opponent_score": ai.get("score") or {},
                "resources": me.get("player") or {},
                "units": sum(not u.get("structure") for u in units), "structures": sum(bool(u.get("structure"))
                                                                                    for u in units),
                "opponent_units": sum(not u.get("structure") for u in ai.get("units") or ()),
                "opponent_structures": sum(bool(u.get("structure")) for u in ai.get("units") or ()),
                "mode": self.scenario["mode"], "client_view": self.scenario["client_view"],
                "harness": dict(self.stats), "engine_failed": self.failed is not None, "error": self.failed,
            }
        return [DataPart(data=summary)]

    # ---- extensions (the harness's; each counts in data/get's harness) ----

    @extension(NEW_GAME_EXTENSION,
               description="Start a new game against the game's own AI; omitted args keep the current scenario. "
                           "map: a stock map, e.g. (2)EchoIsles.w3x; race and opponent_race: human, orc, undead, "
                           "night_elf or random; ai_difficulty: easy, normal or insane; seed (null: a new one); "
                           "time_limit_seconds: game seconds before the game ends undecided; mode: stepping "
                           "(time passes when the agent steps) or realtime (the game runs on its own clock); "
                           "allow_debug: the session's debug extension may stage the game; client_view: draw the "
                           "game for spectators, its picture live and in the recording (stepping is slower); "
                           "labels: players' names by slot for spectators; "
                           "license: the activation files {\"roc.w3k\": base64, \"tft.w3k\": base64}, needed once "
                           "per env.")
    async def new_game(self, map: str | None = None, race: str | None = None, opponent_race: str | None = None,
                       ai_difficulty: str | None = None, seed: int | None = None, randomize_starts: bool | None = None,
                       time_limit_seconds: int | None = None, mode: str | None = None,
                       allow_debug: bool | None = None, client_view: bool | None = None,
                       labels: dict | None = None, license: dict | None = None) -> dict:
        self.stats["extension_calls"] += 1
        given = {"map": map, "race": race, "opponent_race": opponent_race, "ai_difficulty": ai_difficulty,
                 "seed": seed, "randomize_starts": randomize_starts, "time_limit_seconds": time_limit_seconds,
                 "mode": mode, "allow_debug": allow_debug, "client_view": client_view, "labels": labels}
        scenario = check_scenario({**self.scenario, **{k: v for k, v in given.items() if v is not None}})
        async with self.lock:
            if license is not None and not self.fake:
                attach_license(license, self.license_store, self.game_dir)
            try:
                await self._new_game(scenario)
            except WorkerError as e:
                raise RuntimeError(f"{e.code}: {e.message}") from e
            return {"scenario": self.scenario, "setup": self.setup}

    @extension(IDLE_EXTENSION, description="Let the game run with no orders from the agent's side, for checking a "
                                           "setup without a model; every second counts in data/get's harness.")
    async def idle(self, seconds: int) -> dict:
        self.stats["extension_calls"] += 1
        if not 1 <= seconds <= 4 * 3600:
            raise ValueError("seconds must be 1 to 14400")
        async with self.lock:
            try:
                await self._ensure_game()
                start = self._seconds()
                limit = self.scenario["time_limit_seconds"]
                while not self.game_over and self._seconds() - start < seconds:
                    step = min(MAX_ADVANCE_SECONDS, seconds - (self._seconds() - start), limit - self._seconds())
                    result = await self.bridge.call("step", actions={}, ms=max(25, round(step * 1000) // 25 * 25))
                    self._observed(result)
                    self._record()
                    await self._aim()
                    if self.scenario["mode"] == "realtime":   # the game runs on its own: just look in now and then
                        await asyncio.sleep(1)
                played = self._seconds() - start
            except WorkerError as e:
                self.failed = self.failed or (e.message if e.code in DEAD or e.code == "game_failed" else None)
                raise RuntimeError(f"{e.code}: {e.message}") from e
            self.stats["idle_seconds"] += round(played)
            return {"played_seconds": played, "game_time_seconds": self._seconds(), "result": self.result}

    @extension(REPLAY_EXTENSION, description="The finished game's native Warcraft III replay (.w3g), as base64 "
                                             "files; recording stops, so call it once the game is over.")
    async def replay(self) -> dict:
        self.stats["extension_calls"] += 1
        async with self.lock:
            if not self.obs or self.bridge is None:
                raise RuntimeError("no game has been played")
            if not self.game_over:
                raise RuntimeError("the game is not over: a replay ends the recording")
            try:
                f = await self.bridge.call("replay")
            except WorkerError as e:
                if e.code in DEAD:
                    raise RuntimeError(f"{e.code}: {e.message}") from e
                return {"files": [], "notes": [f"no replay: {e.message}"]}   # e.g. the fake game records none
        return {"files": [{"name": f["name"], "content_type": "application/octet-stream", "base64": f["base64"]}]}

    # ---- the urn:rts:* session (agentenv_rts.session): a program plays through raw observations and actions ----

    @extension(OBSERVE, description="The game as a program plays it: every player's raw wc3env observation by slot, "
                                    "whether it is over, and the scenario and setup it was started with.")
    async def session_observe(self) -> dict:
        self.stats["extension_calls"] += 1
        async with self.lock:
            await self._session_game()
            return self._session_state()

    @extension(STEP, description="Send raw wc3env actions ({slot: [action]}) and step the game `ms` milliseconds "
                                 "(default 1000; in realtime the game runs on its own clock and this only sends and "
                                 "observes). Orders that no longer apply are dropped and reported as rejected, like "
                                 "the game's own refusals, by their index in the batch.")
    async def session_step(self, actions: dict | None = None, ms: int | None = None) -> dict:
        self.stats["extension_calls"] += 1
        async with self.lock:
            await self._session_game()
            if self.game_over:
                return {**self._session_state(), "rejected": {}, "placements": {}, "elapsed_ms": 0}
            limit = self.scenario["time_limit_seconds"]
            ms = DEFAULT_STEP_MS if ms is None else int(ms)
            if not 25 <= ms <= MAX_ADVANCE_SECONDS * 1000:
                raise ValueError(f"ms must be 25 to {MAX_ADVANCE_SECONDS * 1000}")
            ms = max(25, min(ms, round((limit - self._seconds()) * 1000) // 25 * 25))
            sent, dropped = {}, {}
            for slot, batch in (actions or {}).items():
                if int(slot) not in self.obs:
                    raise ValueError(f"slot {slot} is not a player in this game")
                kept, gone = await self._still_valid_indexed(int(slot), list(batch or ()))
                sent[str(slot)], dropped[str(slot)] = kept, gone
            try:
                result = await self.bridge.call("step", actions={k: [a for _, a in v] for k, v in sent.items()},
                                                ms=ms)
            except WorkerError as e:
                if e.code in DEAD or e.code == "game_failed":
                    self.failed = self.failed or e.message
                raise RuntimeError(f"{e.code}: {e.message}") from e
            self._observed(result)
            rejected, placements = {}, {}
            for slot, kept in sent.items():
                index = [i for i, _ in kept]
                rejected[slot] = sorted(
                    [{**r, "index": index[r["index"]]} for r in (result.get("rejected") or {}).get(slot, [])
                     if 0 <= r.get("index", -1) < len(index)] + dropped[slot], key=lambda r: r["index"])
                placements[slot] = [{**p, "index": index[p["index"]]}
                                    for p in (result.get("placements") or {}).get(slot, [])
                                    if 0 <= p.get("index", -1) < len(index)]
                self.stats["orders_sent"] += len(kept)
                self.stats["orders_rejected"] += len(rejected[slot])
            self.stats["session_steps"] += 1
            me = self._me()
            for o in self.obs.values():
                self.names.see(o)
            self.recent = render.events_text(me.get("events") or [], self.ref, self.names, AGENT)
            self._record()
            await self._aim()
            return {**self._session_state(), "rejected": rejected, "placements": placements,
                    "elapsed_ms": result.get("elapsed_ms")}

    @extension(DEBUG, description="A wc3env debug op ({op, args}). speed, camera, overlay and render are always "
                                  "allowed; ops that stage the game (resources, spawn, ai, ...) only in a match "
                                  "started with allow_debug.")
    async def session_debug(self, op: str, args: dict | None = None) -> dict:
        self.stats["extension_calls"] += 1
        async with self.lock:
            await self._session_game()
            if op not in VIEW_DEBUG_OPS and not self.scenario["allow_debug"]:
                raise ValueError(f"debug op {op!r} stages the game: the match must allow it (allow_debug)")
            if self.fake and op in VIEW_DEBUG_OPS:
                return {"ignored": True, "reason": "the fake game has no display or clock to change"}
            try:
                return await self.bridge.call("debug", op=op, args=args or {})
            except WorkerError as e:
                raise RuntimeError(f"{e.code}: {e.message}") from e

    @extension(NOTE, description="What a player tells the spectators: `plan`, its current plan in a sentence or two "
                                 "(shown on the live page and in the game's picture); `player`, its name, e.g. the "
                                 "models that play it; `stats`, data {cost_usd, decisions, tokens} so far.")
    async def session_note(self, kind: str, text: str = "", slot: int = AGENT, data: dict | None = None) -> dict:
        self.stats["extension_calls"] += 1
        if kind not in NOTE_KINDS:
            raise ValueError(f"kind must be one of {', '.join(NOTE_KINDS)}")
        text = " ".join(str(text or "").split())[:NOTE_CHARS]
        async with self.lock:
            if self.timeline is None or self.feed is None:
                raise RuntimeError("no game has started")
            if kind == "player" and text:
                for player in self.timeline.static["players"]:
                    if player["slot"] == slot:
                        player["label"] = text
                self.feed.labels[slot] = text
            elif kind == "stats":
                self.agents[slot] = {k: (data or {})[k] for k in ("cost_usd", "decisions", "tokens")
                                     if isinstance((data or {}).get(k), int | float)}
            elif kind == "plan" and text:
                self.notes.append({"slot": slot, "kind": kind, "text": text})
                if self.capture is not None and self.capture.running:
                    try:
                        await self.bridge.call("debug", op="overlay", args={"panel": wrapped(text), **OVERLAY})
                    except WorkerError as e:
                        log.warning("overlay: %s", e.message)
        return {}

    async def _session_game(self) -> None:
        try:
            await self._ensure_game()
        except WorkerError as e:
            raise RuntimeError(f"{e.code}: {e.message}") from e

    def _session_state(self) -> dict:
        return {"observations": {str(k): v for k, v in self.obs.items()}, "done": self.game_over,
                "result": self.result, "scenario": self.scenario, "setup": self.setup,
                "time_limit_seconds": self.scenario["time_limit_seconds"]}

    async def _still_valid_indexed(self, slot: int, batch: list[dict]) -> tuple[list[tuple[int, dict]], list[dict]]:
        """The batch's orders that still apply, with their indexes, and the others as rejections (as `step` reports
        the game's): wc3env refuses a whole batch for one bad order."""
        if not batch:
            return [], []
        try:
            await self.bridge.call("validate", slot=slot, actions=batch)
            return list(enumerate(batch)), []
        except WorkerError as e:
            if e.code != "bad_actions":
                raise RuntimeError(f"{e.code}: {e.message}") from e
        kept, dropped = [], []
        for i, action in enumerate(batch):
            try:
                await self.bridge.call("validate", slot=slot, actions=[action])
                kept.append((i, action))
            except WorkerError as e:
                if e.code != "bad_actions":
                    raise RuntimeError(f"{e.code}: {e.message}") from e
                dropped.append({"index": i, "reason": f"dropped: {e.message}"})
        return kept, dropped

    # ---- spectators: the live view and the recording (agentenv_rts) ----

    @extension(rts_recording.RECORDING, description="The game played so far as spectators see it: an MP4 of the "
                                                    "map (mp4), a self-contained HTML replay (html) and, for a match "
                                                    "with client_view, the game's own video (client; that ends it), "
                                                    "as base64 files.")
    async def recording(self, formats: list[str] | None = None) -> dict:
        self.stats["extension_calls"] += 1
        formats = tuple(formats or ("mp4", "html"))
        async with self.lock:
            timeline = self.timeline
            if timeline is None or not timeline.frames:
                return {"files": [], "notes": ["no game has been played"]}
            stem = f"wc3-{Path(self.scenario['map']).stem}-{timeline.static['game']}".replace(" ", "")
            client = (await asyncio.to_thread(self.capture.stop)
                      if "client" in formats and self.capture is not None else None)
        files, notes = await asyncio.to_thread(rts_recording.files, timeline, stem, formats, client)
        return {"files": files, "notes": notes}

    def create_app(self):
        app = super().create_app()
        app.custom_route("/live", methods=["GET"])(self._live_page)
        app.custom_route("/live/data.json", methods=["GET"])(self._live_data)
        app.custom_route("/live/client", methods=["GET"])(self._live_client)
        app.custom_route("/live/client.jpg", methods=["GET"])(self._live_client_frame)
        app.custom_route("/live/state.json", methods=["GET"])(self._live_state)
        app.custom_route("/live/casting.json", methods=["GET"])(self._live_casting)
        return app

    async def _live_page(self, request: Request) -> Response:
        return HTMLResponse(rts_live.page(), headers={"Cache-Control": "no-cache"})

    async def _live_data(self, request: Request) -> Response:
        return JSONResponse(rts_live.data(self.timeline, request.query_params.get("since"), self.capture is not None),
                            headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"})

    async def _live_state(self, request: Request) -> Response:
        """Where the game stands, for a streamer deciding when to stop."""
        last = self.timeline.last if self.timeline is not None else None
        return JSONResponse({"game_over": self.game_over, "result": self.result, "t": (last or {}).get("t"),
                             "client": self.capture is not None}, headers={"Cache-Control": "no-store"})

    async def _live_casting(self, request: Request) -> Response:
        return JSONResponse(rts_live.casting(self.timeline, request.query_params.get("since"), self.desk()),
                            headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"})

    def desk(self) -> str:
        """The casters' brief: what game this is, who plays, and how it is won."""
        s, names = self.scenario, {p["slot"]: p["label"] for p in (self.timeline.static["players"]
                                                                   if self.timeline is not None else ())}
        race = s["race"].replace("_", " ")
        return (f"Warcraft III: The Frozen Throne, a real-time strategy game. Each side mines gold and cuts lumber, "
                f"builds a base, trains workers, an army and heroes (heroes level up by killing creeps and enemies), "
                f"and wins by destroying every building of the other side. Neutral creep camps guard the map and give "
                f"heroes experience and items. Here {names.get(AGENT, 'an AI agent')} plays {race} against the game's "
                f"own {s['ai_difficulty']} {s['opponent_race'].replace('_', ' ')} AI on {Path(s['map']).stem}, in "
                f"{'real time' if s['mode'] == 'realtime' else 'stepped time (the game waits while the agent thinks)'}"
                f". There is a {s['time_limit_seconds'] // 60}-minute limit: if time runs out, the game is undecided "
                f"and the score says who was ahead. Score counts units, buildings, heroes and resources gathered.")

    def _client_frame(self) -> bytes | None:
        return self.capture.latest() if self.capture is not None else None

    async def _live_client(self, request: Request) -> Response:
        return StreamingResponse(rts_display.mjpeg(self._client_frame),
                                 media_type=f"multipart/x-mixed-replace; boundary={rts_display.BOUNDARY}",
                                 headers={"Cache-Control": "no-store"})

    async def _live_client_frame(self, request: Request) -> Response:
        if (frame := self._client_frame()) is None:
            return Response(status_code=404)
        return Response(frame, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    async def close(self) -> None:
        await self._drop_capture()
        if self.bridge is not None:
            await self.bridge.close()


async def _now(value):
    return value


def wrapped(text: str, width: int = 64, lines: int = 3) -> str:
    """A plan as the game's overlay shows it: a few short lines."""
    out = textwrap.wrap(text, width)
    return "\n".join(out[:lines - 1] + [textwrap.shorten(" ".join(out[lines - 1:]), width)] if len(out) > lines
                     else out)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stderr)
    WC3Env().serve()


if __name__ == "__main__":
    main()
