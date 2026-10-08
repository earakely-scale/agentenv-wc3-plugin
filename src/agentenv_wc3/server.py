"""Warcraft III as an AgentEnv environment: an agent plays one side of a melee game against the game's own AI,
through raw wc3env orders (README: How it works).

The game runs in a worker process (worker.py): in the image, Windows Python under Wine driving wc3env, which
holds the game's clock. Time only passes when the agent calls `advance`, so the agent may think as long as it
likes between steps. Orders given with `act` wait in a queue and go to the game with the next `advance`.

A program plays the same game through the `urn:rts:*` session extensions (agentenv_rts.session): raw observations
in, raw wc3env actions out, as wc3env's own `wc3agent` plays (agents/wc3-player). In `realtime` mode the game runs on
its own clock and a step only sends orders and observes. Spectators follow the game at `/live` (agentenv_rts.live);
its spectator view, which a broadcast frames, is `/live?view`, and a finished match keeps its files (agentenv_game's
`@match_files`: the map's video, the HTML replay, the timeline, the native replay). With `client_view` the game
also draws itself in a window on the container's display: the live page shows that picture (`/live/client`), a
director points the camera at the agent's fights and key moments (frames.Director), and the match keeps its video
(agentenv_rts.display). Spectators read a feed of what happened (frames.Feed), and what players tell them through
`urn:rts:note/v1`: their plans (also shown in the game's picture), their names and their running costs.

Who plays comes from the env's lobby (agentenv_game, `urn:game:lobby/v1`): open_lobby opens it with the match's
settings, add_player_slot fills a player slot per player (an agent, which plays at `/players/<player_id>/mcp`, or the
game's AI), and close_lobby closes it, which creates the game and its match (`urn:game:match/v1`). The match starts
at its players' first moves, and finish_match plays it out once they have stopped. An env whose lobby was never
opened plays its default game: an agent at the env's own address against the game's AI.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import math
import os
import re
import shlex
import shutil
import sys
import tempfile
import textwrap
import time
from functools import partial
from pathlib import Path
from typing import Annotated, Any, Literal

from agentenv_game import (
    AgentEnvGameEnv,
    Counter,
    GameError,
    LicenseItem,
    LicenseParts,
    Lobby,
    LobbyStatus,
    MatchFile,
    MatchFiles,
    MatchReport,
    PlayerKind,
    PlayerSlot,
    PlayerSlotLimits,
    PlayerStatus,
    PlayerTeam,
    Score,
    begin_game,
    check_player_slot,
    create_game,
    install_license,
    license_needs,
    match_files,
    match_report,
    play_out,
    player_slot_card,
    player_slot_limits,
    player_teams,
    spectator_card,
)
from agentenv_game.match import FINAL
from agentenv_protocol import (
    AgentEnvFastMCPApplication,
    DataPart,
    environment_card,
    extension,
    get_data,
    reset_data,
    tool,
)
from agentenv_protocol.types import (
    MCP_PATH,
    MCP_TRANSPORT,
    EnvironmentCapabilities,
    EnvironmentCard,
    EnvironmentInterface,
)
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from agentenv_rts import display as rts_display
from agentenv_rts import live as rts_live
from agentenv_rts import recording as rts_recording
from agentenv_rts.lockstep import Lockstep
from agentenv_rts.session import DEBUG, NOTE, NOTE_KINDS, OBSERVE, STEP
from agentenv_rts.timeline import Timeline

from . import frames, metrics, render
from .bridge import DEAD, Bridge, WorkerError

log = logging.getLogger(__name__)

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
                    "allow_debug": False, "client_view": False, "labels": None, "players": None, "step_ms": 1000,
                    "lockstep": {"stall_seconds": 600}}
PLAYER_KEYS = ("agent", "computer", "race", "team", "slot", "ai_assist", "omniscient")
OUTCOMES = {"victory": "won", "defeat": "lost", "draw": "drawn"}
AGENT_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
MODES = ("stepping", "realtime")
ALLIANCE = (0, 1, 2, 3, 4, 5)   # passive, help request and response, shared experience, spells and vision
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
OVERLAY = {"x": -1.0, "y": 1.0, "seconds": 9}   # a plan shows top left in the game's picture, under its resources

FILE_KINDS = (*rts_recording.KINDS, "replay")
"""What a finished match keeps (README: Reference): the recording's kinds, and the game's native replay (.w3g)."""
DEFAULT_FILE_KINDS = ("map_video", "html_replay", "timeline", "replay")
STAGE_EXTENSION = "urn:wc3:stage/v1"
STAGE_OPS = ("spawn", "level", "give", "item", "hp", "mana", "kill", "remove", "resources", "ai", "research",
             "invulnerable", "alliance", "destructable")


class Action(BaseModel):
    """One order to one of your units, as wc3env takes it (wc3env's docs have every command's arguments)."""

    model_config = ConfigDict(extra="forbid")
    unit_id: Annotated[int, Field(description="The unit or structure that acts: an id from get_state or list_units.")]
    command: Annotated[Literal[COMMANDS], Field(description="What it does.")]
    arguments: Annotated[dict[str, Any], Field(
        description='The command\'s arguments, e.g. {"x": 100, "y": -200} for move, {"target_id": 1234} for '
                    'attack or harvest, {"type_id": "Footman"} for train, {"type_id": "Farm", "x": .., "y": .., '
                    '"auto_place": true} for build, {"order": "Blizzard", "x": .., "y": ..} for cast. Type names '
                    'or ids both work. Add "queued": true to append to the unit\'s orders instead of replacing '
                    "them.")] = {}


class LockstepSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", use_attribute_docstrings=True)
    stall_seconds: int | float = Field(600, gt=0)
    """How long the game waits for a silent agent: at the start, before it starts without it, and, with several
    agents stepping, at each turn."""


class MatchSettings(BaseModel):
    """A match's settings, the lobby's game_settings; its players are the lobby's player slots."""

    model_config = ConfigDict(extra="forbid", use_attribute_docstrings=True)
    map: str = Field(DEFAULT_SCENARIO["map"], min_length=1)
    """A map in the game's Maps folder, or a path to one."""
    seed: int | None = Field(None, ge=0, le=0x7FFFFFFF)
    """The game's random seed; null for a new one each game."""
    randomize_starts: bool = False
    """Shuffle the start locations."""
    time_limit_seconds: int = Field(DEFAULT_SCENARIO["time_limit_seconds"], ge=60, le=4 * 3600)
    """Game seconds; at the limit the match ends, and the higher score is ahead."""
    mode: Literal[MODES] = "stepping"
    """stepping: the game waits while the agents think; realtime: it runs on its own clock."""
    allow_debug: bool = False
    """Allow debug ops that stage the game (urn:rts:debug/v1)."""
    client_view: bool = False
    """Show and record the game's own picture."""
    step_ms: int = Field(DEFAULT_STEP_MS, ge=25, le=MAX_ADVANCE_SECONDS * 1000)
    """The game time one step of the engine plays."""
    lockstep: LockstepSettings = LockstepSettings()


class SlotSettings(BaseModel):
    """A player slot's settings: faction, team and label for every player; ai_assist and omniscient for an agent;
    ai_level for the game's AI."""

    model_config = ConfigDict(extra="forbid", use_attribute_docstrings=True)
    faction: Literal[RACES] = "random"
    team: int | None = Field(None, ge=1, le=12)
    """Player slots on one team are allies; without one, a player slot is a team of its own."""
    label: str | None = Field(None, min_length=1, max_length=60)
    """Its name on the live page and the broadcast."""
    ai_assist: bool = False
    """An agent: the game's AI plays its units beside it."""
    omniscient: bool = False
    """An agent: it sees every player's observation."""
    ai_level: Literal[tuple(DIFFICULTIES)] | None = None
    """The game's AI: easy, normal (the default) or insane, one level for every AI player slot."""


def team_of(slot: PlayerSlot) -> int:
    return slot.game_settings["team"] or int(slot.player_id) + 1


def player_slot(player: dict) -> dict:
    """A game's player as the lobby names it: its player_id, kind and name, faction and team (and an AI's level)."""
    return {"player_id": str(player["slot"]), "player_kind": "ai" if player["computer"] else "agent",
            "player_name": player["agent"], "faction": player["race"], "team": player["team"],
            **({"ai_level": player["computer"]} if player["computer"] else {})}


MAKES = {"build": ("builds",), "train": ("trains", "upgrades_to", "sells_units"), "research": ("researches",)}


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
    if not isinstance(s["step_ms"], int) or not 25 <= s["step_ms"] <= MAX_ADVANCE_SECONDS * 1000:
        raise ValueError(f"step_ms must be an integer from 25 to {MAX_ADVANCE_SECONDS * 1000}")
    stall = (s["lockstep"] or {}).get("stall_seconds") if isinstance(s["lockstep"], dict) else None
    if not isinstance(stall, int | float) or stall <= 0:
        raise ValueError('lockstep must be {"stall_seconds": <seconds>} with a positive number')
    players_of(s)
    return s


def players_of(s: dict) -> list[dict]:
    """The match's players, each {slot, agent, computer, race, team, ai_assist, omniscient}: `players` as given, or the
    shorthand's two (an agent with no name, played at the env's root, and the game's AI)."""
    given = s["players"] if s["players"] is not None else [
        {"race": s["race"], "team": 1, "agent": None}, {"computer": s["ai_difficulty"], "race": s["opponent_race"],
                                                        "team": 2}]
    if not isinstance(given, list) or not 1 <= len(given) <= 12:
        raise ValueError("players must be a list of 1 to 12 players")
    players = []
    for i, player in enumerate(given):
        if not isinstance(player, dict) or (unknown := sorted(set(player) - set(PLAYER_KEYS))):
            raise ValueError(f"player {i}: a player is an object with {', '.join(PLAYER_KEYS)}"
                             + (f", not {unknown}" if isinstance(player, dict) else ""))
        agent, computer = player.get("agent"), player.get("computer")
        if (computer is None) == ("agent" not in player) or (agent is not None and not AGENT_NAME.match(str(agent))):
            raise ValueError(f"player {i}: give one of agent (the deploy_agent's agent_name) or computer "
                             f"({', '.join(DIFFICULTIES)})")
        if computer is not None and computer not in DIFFICULTIES:
            raise ValueError(f"player {i}: computer must be one of {', '.join(DIFFICULTIES)}, got {computer!r}")
        race = player.get("race", "random")
        if race not in RACES:
            raise ValueError(f"player {i}: race must be one of {', '.join(RACES)}, got {race!r}")
        players.append({"slot": player.get("slot", i), "agent": agent, "computer": computer, "race": race,
                      "team": player.get("team", i + 1), "ai_assist": bool(player.get("ai_assist")),
                      "omniscient": bool(player.get("omniscient"))})
    slots, names = [x["slot"] for x in players], [x["agent"] for x in players if x["agent"]]
    if len(set(slots)) != len(slots) or not all(isinstance(x, int) and 0 <= x <= 11 for x in slots):
        raise ValueError("players need distinct slots from 0 to 11")
    if len(set(names)) != len(names):
        raise ValueError("an agent plays one player")
    if len({x["computer"] for x in players if x["computer"]}) > 1:
        raise ValueError("every computer player has the same difficulty: the game has one AI level")
    if not any(x["computer"] is None for x in players):
        raise ValueError("a match needs an agent")
    return players


def scenario_of(lobby: Lobby) -> dict:
    """The game a closed lobby describes, as the env plays it: the lobby's settings, a player per player slot (its
    player_id is the game's player number), and the matchup the game's own setup and agents read (the first agent's
    race, the first AI's race and level)."""
    players = []
    for s in lobby.player_slots:
        given = s.game_settings
        player = {"slot": int(s.player_id), "race": given["faction"], "team": team_of(s)}
        if s.player_kind is PlayerKind.AGENT:
            player.update(agent=s.player_name, ai_assist=given["ai_assist"], omniscient=given["omniscient"])
        else:
            player["computer"] = given["ai_level"] or DEFAULT_SCENARIO["ai_difficulty"]
        players.append(player)
    agents, computers = [x for x in players if "agent" in x], [x for x in players if "computer" in x]
    labels = {s.player_id: s.game_settings["label"] for s in lobby.player_slots if s.game_settings["label"]}
    level = computers[0]["computer"] if computers else DEFAULT_SCENARIO["ai_difficulty"]
    return check_scenario({**DEFAULT_SCENARIO, **lobby.game_settings, "players": players, "labels": labels or None,
                           "race": agents[0]["race"] if agents else "random",
                           "opponent_race": computers[0]["race"] if computers else "random", "ai_difficulty": level})


def map_players(map_file: str) -> int:
    """How many players a map takes: its start locations in the prepared map data, else the (N) of its name."""
    if starts := render.map_info(map_file).get("start_locations"):
        return len(starts)
    named = re.match(r"\((\d+)\)", Path(map_file).name)
    return min(int(named[1]), 12) if named else 12


def attach_license(files: dict[str, bytes], store: Path, game_dir: Path) -> None:
    """Writes activation files to a private directory and links them into the game's folder, as wc3env's
    docker/license.py does with a mounted directory: their contents never enter the image."""
    store.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name, data in files.items():
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
class WC3Env(AgentEnvGameEnv):
    GameSettings = MatchSettings
    PlayerSlotSettings = SlotSettings

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
        self.players = players_of(self.scenario)
        self.lead = self.players[0]["slot"]
        self.queue: dict[int, list[dict]] = {}
        self.recent: dict[int, list[str]] = {}
        self.step_info: dict[int, dict] = {}
        self.orders_sent: dict[int, int] = {}
        self.lockstep: Lockstep | None = None
        self.started = asyncio.Event()
        self.created, self.begun = time.monotonic(), False
        self.last_move: dict[int, float] = {}
        self.played_out_from: float | None = None
        self.metrics: metrics.Metrics | None = None
        self.done = False
        self.names = render.Names()
        self.result = ""
        self.failed: str | None = None
        self.timeline: Timeline | None = None
        self.capture: rts_display.Capture | None = None
        self.recordings: Path | None = None   # the latest match's files, which agentenv_game serves
        self.camera: tuple[float, float] | None = None
        self.director = frames.Director(self.lead)
        self.feed: frames.Feed | None = None
        self.notes: list[dict] = []
        self.agents: dict[int, dict] = {}
        self.stats = {"games": 0, "tool_calls": 0, "invalid_calls": 0, "advances": 0, "orders_sent": 0,
                      "orders_rejected": 0, "extension_calls": 0, "session_steps": 0, "staged_seconds": 0,
                      "finish_seconds": 0}

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
                                            "the add_license task step gives them from agent-env's secret store "
                                            "(agent-env wc3 setup --help)")
        await self._drop_capture()
        if self.bridge is None or not self.bridge.alive:
            self.bridge = Bridge(self.worker_cmd)
            await self.bridge.start()
        players = players_of(scenario)
        starting = [{"slot": x["slot"], "race": x["race"], "control": "computer" if x["computer"] else "agent"}
                    for x in players]
        level = next((x["computer"] for x in players if x["computer"]), scenario["ai_difficulty"])
        result = await self.bridge.call("start", map=scenario["map"], players=starting, step_ms=scenario["step_ms"],
                                        seed=scenario["seed"], randomize_starts=scenario["randomize_starts"],
                                        ai_difficulty=DIFFICULTIES[level],
                                        ai_agents=[x["slot"] for x in players if x["ai_assist"]],
                                        mode=scenario["mode"], render=scenario["client_view"],
                                        visible=scenario["client_view"],
                                        window=WINDOW if scenario["client_view"] else None)
        self.scenario, self.setup, self.players = scenario, result.get("setup"), players
        self.started, self.created, self.begun = asyncio.Event(), time.monotonic(), False
        self.last_move, self.played_out_from = {}, None
        self.lead = next(x["slot"] for x in players if x["computer"] is None)
        self.obs = {int(k): v for k, v in result["observations"].items()}
        agents = [x["slot"] for x in players if x["computer"] is None]
        self.queue, self.recent = {x: [] for x in agents}, {x: [] for x in agents}
        self.orders_sent, self.step_info = {x: 0 for x in agents}, {}
        self.result, self.failed, self.done = "", None, False
        self.lockstep = (Lockstep(agents, scenario["lockstep"]["stall_seconds"], self._chunk, self._seconds,
                                  self._out)
                         if scenario["mode"] == "stepping" and len(agents) > 1 else None)
        await self._ally(players)
        self.names = render.Names()
        for o in self.obs.values():
            self.names.see(o)
        self.stats["games"] += 1
        labels = {**{x["slot"]: x["agent"] for x in players if x["agent"]},
                  **{int(k): v for k, v in (scenario["labels"] or {}).items()}}
        self.timeline = Timeline(frames.static(f"g-{self.stats['games']}", scenario, self.setup, self.obs,
                                               self.ref, labels))
        for player in self.timeline.static["players"]:
            player["team"] = next((x["team"] for x in players if x["slot"] == player["slot"]), None)
        self.feed = frames.Feed(self.ref, self.timeline.static)
        self.metrics = metrics.Metrics(self.ref, render.map_info(scenario["map"]), [x["slot"] for x in players])
        self.metrics.see(self.obs)
        self.camera, self.director, self.notes, self.agents = None, frames.Director(self.lead), [], {}
        if scenario["client_view"]:
            await self._start_capture()
        self._record()
        log.warning("NEW GAME %s: %s, seed %s", scenario["map"], "; ".join(
            f"slot {x['slot']} {x['race']} {x['agent'] or ('AI ' + x['computer'] if x['computer'] else 'agent')} "
            f"team {x['team']}" for x in players), (self.setup or {}).get("match_setup"))

    async def _ally(self, players: list[dict]) -> None:
        """Players on one team are allies (passive, helping, sharing vision): the game's melee teams."""
        for a in players:
            for b in players:
                if a is not b and a["team"] == b["team"]:
                    for kind in ALLIANCE:
                        await self.bridge.call("debug", op="alliance", args={"player": a["slot"], "other": b["slot"],
                                                                             "kind": kind, "enabled": 1})

    async def _ensure_game(self) -> None:
        """Under the lock: the game is there. Until a lobby is opened that is the default game, one agent at the
        root against the AI; after, only the game a closed lobby created."""
        if self.failed:
            raise WorkerError("game_failed", f"the game stopped working: {self.failed}. A new game (data/reset, or a "
                                             "new lobby) starts it again.")
        status = self.lobby.status
        if status not in (LobbyStatus.NOT_OPENED, LobbyStatus.CLOSED):
            raise WorkerError("no_game", f"there is no game: the lobby is {status}, and "
                              + ("closing it (close_lobby) creates the game" if status is LobbyStatus.OPEN
                                 else "a new one (open_lobby) is for the next game"))
        if not self.obs:
            await self._new_game(self.scenario)

    def _playing(self) -> dict | None:
        """The player the current request plays: its player slot's, at `/players/<player_id>`; None at the root."""
        try:
            slot = self.player()
        except GameError as e:
            raise WorkerError(e.code, e.message) from e
        if slot is None:
            return None
        player = next((x for x in self.players if x["slot"] == int(slot.player_id)), None)
        if player is None:
            raise WorkerError("unknown_player", f"player slot {slot.player_id} is not in this game")
        return player

    def _slot(self) -> int:
        """The player number the current request plays; at the root, the first agent's."""
        player = self._playing()
        return player["slot"] if player else self.lead

    def _me(self, slot: int | None = None) -> dict:
        return self.obs[self._slot() if slot is None else slot]

    @property
    def game_over(self) -> bool:
        """Over once every agent has a result, so in a free-for-all the last players play on after one is out."""
        return self.done or all(self._result_of(x["slot"]) for x in self.players if x["computer"] is None)

    def _ended(self) -> bool:
        """The match is over for its players: the game ended or stopped working, or the harness ended the match
        (cancel_match)."""
        return (self.failed is not None or self.game_over
                or ((match := self.match) is not None and match.status in FINAL))

    def _out(self, slot: int) -> bool:
        """This player's game is over: the match is, or the player has a result."""
        return self._ended() or bool(self._result_of(slot))

    def _seconds(self) -> float:
        return max((o.get("game_time_seconds") or 0.0 for o in self.obs.values()), default=0.0)

    def _given(self, slot: int) -> str:
        return (self.obs.get(slot) or {}).get("result") or ""

    def _result_of(self, slot: int) -> str:
        """The game's result for a player, else victory once every player on the other teams is defeated: the game
        lets allies win together only with the lobby's allied victory, which teams made by alliances don't have."""
        own = self._given(slot)
        if own:
            return own
        team = next((x["team"] for x in self.players if x["slot"] == slot), None)
        rivals = [x["slot"] for x in self.players if x["team"] != team]
        if rivals and all(self._given(r) == "defeat" for r in rivals):
            return "victory"
        return "time_limit" if self._seconds() >= self.scenario["time_limit_seconds"] else ""

    async def _play(self, slot: int, batch: list[dict], ms: int) -> dict:
        """A player's orders and `ms` of game time: one step, or, with several agents stepping, a turn of the lockstep
        clock, which moves when they all wait. The step's refusals and sites for this player's orders."""
        await self._ready(slot)
        self.last_move[slot] = self._seconds()
        ms = max(25, min(ms, round((self.scenario["time_limit_seconds"] - self._seconds()) * 1000) // 25 * 25))
        self.step_info[slot] = {"rejected": [], "placements": [], "sent": len(batch)}
        if self.lockstep is None:
            await self._chunk({slot: batch}, ms / 1000)
        else:
            await self.lockstep.step(slot, batch, ms / 1000)
        if self.failed:   # another player's step found the game dead
            raise WorkerError("game_failed", f"the game stopped working: {self.failed}")
        return self.step_info.pop(slot)

    async def _ready(self, slot: int) -> None:
        """A player slot's first move says it is ready. The match starts once every player is (agentenv_game's start
        gate), with all their opening orders; until then a realtime game stands at its start, and a stepped one
        doesn't step. Lockstep's stall_seconds after the game was created, it starts without the silent ones. The
        default game, at the root without a lobby, starts at its first move."""
        if self.match is None:
            await self.start_clock()
            return
        await self.player_ready(str(slot))
        stall = self.scenario["lockstep"]["stall_seconds"]
        while not self.begun and not self._out(slot):
            left = stall - (time.monotonic() - self.created)
            if left <= 0:
                waiting = [p for p, x in self.match.player_states.items() if x.status is PlayerStatus.NOT_READY]
                log.warning("starting without %s: no first move in %s s", waiting, stall)
                await self.begin_match(f"started without player slot{'s' if len(waiting) > 1 else ''} "
                                       f"{', '.join(waiting)}: no first move in {stall:g} s")
                break
            try:
                await asyncio.wait_for(self.started.wait(), timeout=min(left, 1.0))
            except TimeoutError:
                pass

    @begin_game
    async def start_clock(self) -> None:
        """The match starts: a realtime game held at its start runs on its own clock from now."""
        self.begun = True
        async with self.lock:
            if self.started.is_set():
                return
            await self.bridge.call("release")
            self.started.set()
            log.warning("GAME STARTED at %.1f s", self._seconds())

    def _waiting(self) -> list[int]:
        """The player slots the match waits for before it starts: those yet to make their first move."""
        if self.begun or self.game_over or (match := self.match) is None:
            return []
        return [int(p) for p, x in match.player_states.items() if x.status is PlayerStatus.NOT_READY]

    async def _chunk(self, batches: dict[int, list], seconds: float) -> None:
        """One step of the game with every waiting player's orders; its observations become the game's state."""
        self.begun = True
        self.started.set()
        async with self.lock:
            if self.failed:   # a player that was waiting when the game failed hears why, not that there is no game
                raise WorkerError("game_failed", f"the game stopped working: {self.failed}")
            ms = max(25, round(seconds * 1000) // 25 * 25)
            try:
                result = await self.bridge.call("step", actions={str(k): v for k, v in batches.items()}, ms=ms)
            except WorkerError as e:
                if e.code in DEAD or e.code == "game_failed":
                    self.failed = self.failed or e.message
                raise
            self._observed(result)
            for slot, batch in batches.items():
                info = self.step_info.setdefault(slot, {"rejected": [], "placements": [], "sent": len(batch)})
                info["rejected"] += (result.get("rejected") or {}).get(str(slot), [])
                info["placements"] += (result.get("placements") or {}).get(str(slot), [])
                self.orders_sent[slot] = self.orders_sent.get(slot, 0) + len(batch)
                self.stats["orders_sent"] += len(batch)
                self.stats["orders_rejected"] += len((result.get("rejected") or {}).get(str(slot), []))
            for o in self.obs.values():
                self.names.see(o)
            for slot in self.recent:
                self.recent[slot] = render.events_text(self.obs.get(slot, {}).get("events") or [], self.ref,
                                                       self.names, slot)
            self._record()
            await self._aim()

    def _record(self) -> None:
        """A spectator frame of the game as it is now, with the feed's new events and the players' new notes."""
        if self.timeline is None or not self.obs:
            return
        notes, self.notes = self.notes, []
        wall = time.monotonic() - self.capture.started if self.capture is not None and self.capture.running else None
        frame = frames.frame(self.obs, self.result if self.game_over else "", self.feed.see(self.obs), self.ref, notes,
                             wall)
        for slot, stats in self.agents.items():
            frame["players"].setdefault(str(slot), {})["agent"] = stats
        self.timeline.add(frame)

    def _observed(self, result: dict) -> None:
        """A step's observations become the game's state: whether the game says it is over, and the first agent
        player slot's result (or the time limit), which today's summary reports."""
        self.obs = {int(k): v for k, v in result["observations"].items()}
        self.done = self.done or bool(result.get("done"))
        self.result = self._result_of(self.lead)
        if self.metrics is not None:
            self.metrics.see(self.obs)

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
        if (spot := self.director.choose(self._me(self.lead), self.feed.moments, time.monotonic())) is not None:
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

    def _footer(self, slot: int) -> str:
        return render.footer(self._me(slot), limit=self.scenario["time_limit_seconds"],
                             queued=len(self.queue.get(slot, ())), result=self._result_of(slot))

    async def _run(self, body, count: bool = True):
        """A tool call: one at a time, on a started game; game errors become tool errors the agent can read."""
        async with self.lock:
            self.stats["tool_calls"] += count
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
            slot = self._slot()
            player = next(x for x in self.players if x["slot"] == slot)
            return self._briefing(slot) + "\n" + render.state(
                self._me(slot), self.ref, me=slot, race=player["race"], map_name=self.scenario["map"],
                limit=self.scenario["time_limit_seconds"], queued=len(self.queue.get(slot, ())),
                result=self._result_of(slot), events=self.recent.get(slot, []))
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
            slot = self._slot()
            center = self._unit(slot, near) if near is not None else None
            return render.units_list(self._me(slot), self.ref, who=who, type_filter=type, near=center,
                                     radius=radius, details=details, limit=limit)
        return await self._run(body)

    @tool()
    async def resources(self, near: Annotated[int | None, Field(
                            description="The unit to measure from (default: your first structure).")] = None,
                        radius: Annotated[float, Field(gt=0, le=4000)] = 1200):
        """Gold mines in view, the nearest trees (harvest a tree's id for lumber) and items on the ground."""
        async def body():
            slot = self._slot()
            me = self._me(slot)
            center = self._unit(slot, near) if near is not None else next(
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
            slot = self._slot()
            if self._out(slot):
                raise WorkerError("game_over", f"the game is over ({self._result_of(slot) or 'ended'})")
            batch = [self._resolve(slot, a.model_dump()) for a in actions]
            await self.bridge.call("validate", slot=slot, actions=batch)
            self.queue[slot] = batch if clear else self.queue.get(slot, []) + batch
            return (f"Queued {len(batch)} order{'s' if len(batch) != 1 else ''}: "
                    + "; ".join(self._describe(a) for a in batch)
                    + f". {len(self.queue[slot])} in the queue, sent with your next advance.\n" + self._footer(slot))
        return await self._run(body)

    @tool()
    async def advance(self, seconds: Annotated[int, Field(ge=1, le=MAX_ADVANCE_SECONDS, description=(
                          "Game seconds to play, 1-60. Short steps for fights, longer ones while the economy runs."))]
                      = 5,
                      actions: Annotated[list[Action] | None, Field(
                          description="More orders to send with the queue, as act takes them.")] = None):
        """Send the queued orders (and any given here) and let the game run: the only way time passes. Returns
        what the game refused, the sites it chose for buildings, what happened, and the new state's footer."""
        async def before():
            slot = self._slot()
            if self._out(slot):
                raise WorkerError("game_over", f"the game is over ({self._result_of(slot) or 'ended'}); get_state "
                                               "shows the end")
            extra = [self._resolve(slot, a.model_dump()) for a in actions or ()]
            if extra:
                await self.bridge.call("validate", slot=slot, actions=extra)
            batch, dropped = await self._still_valid(slot, self.queue.get(slot, []) + extra)
            self.queue[slot] = []
            return slot, batch, dropped, self._seconds()

        slot, batch, dropped, start = await self._run(before)
        try:
            info = await self._play(slot, batch, seconds * 1000)
        except WorkerError as e:
            return await self._run(partial(_raise, e), count=False)

        async def after():
            self.stats["advances"] += 1
            me, events, rejected = self._me(slot), self.recent.get(slot, []), info["rejected"]
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
            for p in info["placements"]:
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
            if result := self._result_of(slot):
                lines.append(f"GAME OVER: {render.RESULTS.get(result, result)}. Reply with your result.")
            lines.append(self._footer(slot))
            return "\n".join(lines)
        return await self._run(after, count=False)

    # ---- orders ----

    async def _still_valid(self, slot: int, batch: list[dict]) -> tuple[list[dict], list[tuple[dict, str]]]:
        """The batch without the orders that stopped applying since they were queued (the unit died, the target
        went out of view): wc3env refuses a whole batch for one of them. Returns it and the dropped orders."""
        if not batch:
            return batch, []
        try:
            await self.bridge.call("validate", slot=slot, actions=batch)
            return batch, []
        except WorkerError as e:
            if e.code != "bad_actions":
                raise
        kept, dropped = [], []
        for action in batch:
            try:
                await self.bridge.call("validate", slot=slot, actions=[action])
                kept.append(action)
            except WorkerError as e:
                if e.code != "bad_actions":
                    raise
                dropped.append((action, e.message))
        return kept, dropped

    def _unit(self, slot: int, unit_id: int) -> dict:
        me = self._me(slot)
        for u in [*(me.get("units") or ()), *(me.get("visible_enemies") or ()), *(me.get("inside") or ())]:
            if u["unit_id"] == unit_id:
                return u
        raise WorkerError("unknown_unit", f"no unit {unit_id} in view; list_units shows the ids")

    def _resolve(self, slot: int, action: dict) -> dict:
        """The action as wc3env takes it: type, ability and order names turned into ids, `arguments` dropped when
        empty."""
        args = dict(action.get("arguments") or {})
        command = action["command"]
        if command in MAKES and isinstance(args.get("type_id"), str):
            args["type_id"] = self._made_by(slot, action["unit_id"], command, args["type_id"])
        if command == "buy" and isinstance(args.get("item_type_id"), str):
            hits = self.ref.find(args["item_type_id"], ("item",))
            args["item_type_id"] = hits[0][1] if len(hits) == 1 else args["item_type_id"]
        if command in ("cast", "learn"):
            own = next((u for u in self._me(slot).get("units") or () if u["unit_id"] == action["unit_id"]), None)
            abilities = [a["ability_id"] for a in (own or {}).get("abilities") or ()]
            if command == "cast" and isinstance(args.get("order"), str):
                args["order"] = self.ref.cast_order(args["order"], abilities)
            if command == "learn" and isinstance(args.get("ability_id"), str) and own is not None:
                skills = (self.ref.units.get(own["type_id"]) or {}).get("potential_hero_abilities") or []
                named = [s for s in skills if self.ref.name(s).lower() == args["ability_id"].strip().lower()]
                args["ability_id"] = named[0] if named else args["ability_id"]
        return {"unit_id": action["unit_id"], "command": command, **({"arguments": args} if args else {})}

    def _made_by(self, slot: int, unit_id: int, command: str, value: str) -> str:
        """The type `value` names for this unit's build, train or research: of the types a name covers (Human and
        Orc Barracks), the one the unit makes. One it doesn't make is refused here, naming the units that do."""
        own = self._me(slot).get("units") or ()
        unit = next((u for u in own if u["unit_id"] == unit_id), None)
        facts, named = self.ref.units.get(unit["type_id"]) if unit else None, self.ref.named(value)
        if not facts or not named:
            return self.ref.type_id(value)

        def makes(type_id: str) -> list[str]:
            return [t for key in MAKES[command] for t in (self.ref.units.get(type_id) or {}).get(key) or ()]

        if fits := [t for t in named if t in makes(unit["type_id"])]:
            return fits[0]
        who = sorted({self.ref.name(u["type_id"]) for u in own if set(named) & set(makes(u["type_id"]))})
        makers = sorted({self.ref.name(t) for t, f in self.ref.units.items()
                         if f.get("race") == facts.get("race") and set(named) & set(makes(t))})
        raise ValueError(f"{unit_id} {self.ref.name(unit['type_id'])} can't {command} {value}" + (
            f"; your {', '.join(who)} can" if who else f"; none of your units can yet ({', '.join(makers)} can)"
            if makers else ""))

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

    @get_data
    async def data_get(self) -> list[DataPart]:
        async with self.lock:
            if not self.obs and not self.failed:
                try:
                    await self._ensure_game()
                except WorkerError as e:
                    if e.code != "no_game":
                        self.failed = e.message
            lead = next(x for x in self.players if x["slot"] == self.lead)
            rival = next((x for x in self.players if x["team"] != lead["team"]), None)
            me, ai = self.obs.get(self.lead) or {}, self.obs.get(rival["slot"]) if rival else {}
            ai = ai or {}
            units = me.get("units") or []
            summary = {
                "game_time_seconds": self._seconds() if self.obs else 0.0,
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
                "player_slots": [{**player_slot(x),
                           "result": self._result_of(x["slot"]) if self.obs else "",
                           "orders_sent": self.orders_sent.get(x["slot"], 0),
                           "stalls": self.lockstep.stalls.get(x["slot"], 0) if self.lockstep else 0,
                           "last_move_seconds": self.last_move.get(x["slot"]),
                           "spend": self.agents.get(x["slot"]) or {},
                           "metrics": self.metrics.of(x["slot"], self.obs.get(x["slot"]) or {})
                           if self.metrics is not None and self.obs else {}} for x in self.players],
                "handles": {n: {"slot": h["slot"], "staged": len(h["ids"]),
                                "alive": len(set(h["ids"]) - self.metrics.deaths)}
                            for n, h in (self.metrics.handles if self.metrics else {}).items()},
            }
        return [DataPart(data=summary)]

    # ---- extensions (the harness's; each counts in data/get's harness) ----

    # ---- the license (urn:game:license/v1, served by AgentEnvGameEnv) ----

    @license_needs
    def activation_files(self) -> list[LicenseItem]:
        """The activation files the game still lacks: none on the fake game, nor once they are in the game's folder
        (given by add_license, or mounted at WC3_LICENSE_DIR as wc3env's own image takes them)."""
        if self._license_ready():
            return []
        return [LicenseItem(name=n, kind="file", group="warcraft3", max_bytes=4096,
                            description=f"{n} from your Warcraft III Legacy folder; agent-env wc3 license import "
                                        "stores it as " + ("WC3_ROC_W3K" if n == "roc.w3k" else "WC3_TFT_W3K"))
                for n in LICENSE_FILES if not (self.game_dir / n).exists()]

    @install_license
    def link_activation_files(self, parts: LicenseParts) -> None:
        attach_license(parts.files, self.license_store, self.game_dir)

    # ---- the lobby (urn:game:lobby/v1, served by AgentEnvGameEnv): settings in MatchSettings and SlotSettings ----

    @player_slot_limits
    def slot_limits(self, game_settings: MatchSettings, requested: PlayerSlotLimits) -> PlayerSlotLimits:
        """No more player slots than the map's start locations, each an agent or the game's AI."""
        starts = map_players(game_settings.map)
        if requested.max is not None and requested.max > starts:
            raise ValueError(f"{game_settings.map} has {starts} start locations, so no more than {starts} players")
        return PlayerSlotLimits(min=max(requested.min or 1, 1), max=requested.max or starts,
                                player_kinds=[PlayerKind.AGENT, PlayerKind.AI])

    @check_player_slot
    def slot_rules(self, slot: PlayerSlot, lobby: Lobby) -> None:
        """A player_id is one of the map's player numbers; ai_assist and omniscient are an agent's, and ai_level the
        game's AI's, one level for every AI player slot, since the game has one."""
        starts = map_players(lobby.game_settings["map"])
        if slot.player_id not in {str(n) for n in range(starts)}:
            raise GameError("bad_slot", f"a player slot is a player number of {lobby.game_settings['map']}, \"0\" "
                                        f"to \"{starts - 1}\", not {slot.player_id!r}")
        given = slot.game_settings
        if slot.player_kind is PlayerKind.AGENT:
            if given["ai_level"] is not None:
                raise ValueError("ai_level is the game's AI's setting, not an agent's")
            return
        if given["ai_assist"] or given["omniscient"]:
            raise ValueError("ai_assist and omniscient are an agent's settings, not the game's AI's")
        level = given["ai_level"] or DEFAULT_SCENARIO["ai_difficulty"]
        levels = {s.game_settings["ai_level"] or DEFAULT_SCENARIO["ai_difficulty"] for s in lobby.player_slots
                  if s.player_kind is PlayerKind.AI} - {level}
        if levels:
            raise ValueError(f"every AI player slot plays at one level, since the game has one: this lobby's is "
                             f"{levels.pop()}, not {level}")

    @player_slot_card
    def player_card(self, slot: PlayerSlot) -> EnvironmentCard:
        """An agent's player slot: its MCP tools, and the urn:rts session a program plays it through (observe, step,
        debug, note), both at the slot's address."""
        card = self._served_card()
        session = [e for e in card.capabilities.extensions or () if e.uri in (OBSERVE, STEP, DEBUG, NOTE)]
        return EnvironmentCard(name=f"{card.name}/{slot.player_id}",
                               additionalInterfaces=[EnvironmentInterface(url=MCP_PATH, transport=MCP_TRANSPORT)],
                               capabilities=EnvironmentCapabilities(operations=[], extensions=session))

    def _served_card(self) -> EnvironmentCard:
        """The env's card as it is served, built once."""
        if "_served" not in self.__dict__:
            self.__dict__["_served"] = AgentEnvFastMCPApplication(self._build_card(), self).environment_card
        return self.__dict__["_served"]

    @player_teams
    def teams(self, lobby: Lobby) -> list[PlayerTeam]:
        """Player slots with one team are allies; one without a team plays on its own."""
        teams: dict[int, list[str]] = {}
        for s in lobby.player_slots:
            teams.setdefault(team_of(s), []).append(s.player_id)
        return [PlayerTeam(team_id=str(team), player_ids=ids) for team, ids in teams.items()]

    @create_game
    async def new_match(self, lobby: Lobby) -> None:
        """The match the closed lobby describes: its settings, and a player per player slot."""
        scenario = scenario_of(lobby)
        async with self.lock:
            try:
                await self._new_game(scenario)
            except WorkerError as e:
                raise RuntimeError(f"{e.code}: {e.message}") from e

    # ---- the match (urn:game:match/v1, served by AgentEnvGameEnv): its start gate is start_clock ----

    @match_report
    def report(self) -> MatchReport:
        """The match as the game sees it: over once every agent's game is, or at the time limit; its clock; each
        player slot's result and scores."""
        over, limit = self.game_over, self.scenario["time_limit_seconds"]
        rate = (1.0 if self.begun and not over else 0) if self.scenario["mode"] == "realtime" else None
        return MatchReport(
            status="failed" if self.failed else "finished" if over else "started",
            status_detail=self.failed or (self._ending() if over else None),
            progress=[Counter(name="game", unit="seconds", value=round(self._seconds(), 1), limit=limit, rate=rate)],
            outcomes={str(x["slot"]): OUTCOMES[r] for x in self.players
                      if (r := self._result_of(x["slot"])) in OUTCOMES},
            scores={str(x["slot"]): self._scores(x["slot"]) for x in self.players} if self.metrics and self.obs else {})

    def _ending(self) -> str:
        """How the game ended, in words: who won, a draw, or the time limit; and whether finish played it out."""
        won = [self._who(x) for x in self.players if self._result_of(x["slot"]) == "victory"]
        how = (f"{' and '.join(won)} won" if won else
               "a draw" if any(self._result_of(x["slot"]) == "draw" for x in self.players) else
               f"the time limit, {render.clock(self.scenario['time_limit_seconds'])}")
        return how + (f"; played out from {render.clock(self.played_out_from)}"
                      if self.played_out_from is not None else "")

    def _scores(self, slot: int) -> list[Score]:
        m = self.metrics.of(slot, self.obs.get(slot) or {})
        return [Score(name="score", value=m.get("total", 0), better="higher", unit="points"),
                Score(name="units_killed", value=m.get("units_killed", 0), better="higher"),
                Score(name="army", value=m.get("army", 0), better="higher")]

    @play_out
    async def run_out(self) -> None:
        """finish: the game plays on with no more orders from its agents, which wait on the lock, to its end, the
        time limit at most."""
        async with self.lock:
            await self._session_game()
            self.played_out_from = self._seconds()
            try:
                played = await self._run_out(self.scenario["time_limit_seconds"] - self._seconds() + 1)
            except WorkerError as e:
                self.failed = self.failed or (e.message if e.code in DEAD or e.code == "game_failed" else None)
                raise RuntimeError(f"{e.code}: {e.message}") from e
            self.stats["finish_seconds"] += round(played)
            self._record()

    async def _run_out(self, seconds: float) -> float:
        """Lets the game run with no orders for `seconds` or to its end, under the lock: stepped in chunks, or in
        realtime, where the game runs on its own, looked in on every second. The game seconds that passed."""
        start, limit = self._seconds(), self.scenario["time_limit_seconds"]
        while not self.game_over and self._seconds() - start < seconds:
            step = min(MAX_ADVANCE_SECONDS, seconds - (self._seconds() - start), limit - self._seconds())
            result = await self.bridge.call("step", actions={}, ms=max(25, round(step * 1000) // 25 * 25))
            self._observed(result)
            self._record()
            await self._aim()
            if self.scenario["mode"] == "realtime":
                await asyncio.sleep(1)
        return self._seconds() - start

    @extension(STAGE_EXTENSION,
               description="Stage the game before play, for drills: the hook's staging ops in order (spawn, level, "
                           "give, item, hp, mana, kill, remove, resources, ai, research, invulnerable, alliance, "
                           "destructable). `player` is an agent's name, `opponent` or a player number. Places come "
                           "from the first agent's start: home, enemy_home, nearest_camp, camp:<n>, "
                           "building:<name>, toward:<place>:<distance>, with dx and dy. `as` names the units an op "
                           "makes, for later ops (`unit`) and the summary's metrics (army, hero, enemy). "
                           "warmup_seconds lets the game run first. Harness time, not the agents'.")
    async def stage(self, ops: list[dict], warmup_seconds: int = 0) -> dict:
        self.stats["extension_calls"] += 1
        if not isinstance(ops, list) or not 0 <= int(warmup_seconds) <= 600:
            raise ValueError("ops must be a list, and warmup_seconds 0 to 600")
        async with self.lock:
            await self._session_game()
            try:
                if warmup_seconds:
                    result = await self.bridge.call("step", actions={}, ms=int(warmup_seconds) * 1000)
                    self._observed(result)
                    self.stats["staged_seconds"] += int(warmup_seconds)
                handles: dict[str, list[int]] = {}
                for i, op in enumerate(ops):
                    try:
                        await self._stage_op(op, handles)
                    except (KeyError, TypeError, ValueError) as e:
                        raise ValueError(f"op {i} ({op.get('op') if isinstance(op, dict) else op!r}): {e}") from e
                    except WorkerError as e:
                        raise RuntimeError(f"op {i} ({op.get('op')}): {e.code}: {e.message}") from e
                self._observed(await self.bridge.call("observe"))
            except WorkerError as e:
                raise RuntimeError(f"{e.code}: {e.message}") from e
            self._record()
            return {"handles": {name: len(ids) for name, ids in handles.items()}, "game_time_seconds": self._seconds()}

    async def _stage_op(self, op: dict, handles: dict[str, list[int]]) -> None:
        kind = op["op"]
        if kind not in STAGE_OPS:
            raise ValueError(f"unknown op; the staging ops are {', '.join(STAGE_OPS)}")
        units = handles.get(op["unit"], []) if "unit" in op else None
        if units is not None and not units:
            raise ValueError(f"no units named {op['unit']!r} yet")
        if kind == "spawn":
            player = self._staged_player(op.get("player"))
            args = {"type_id": op["type"], "player": player, "n": int(op.get("n", 1)), **self._place(op),
                    **{k: op[k] for k in ("columns", "spacing") if k in op}}
            ids = (await self.bridge.call("debug", op="spawn", args=args)).get("unit_ids") or []
            if name := op.get("as"):
                handles.setdefault(name, []).extend(ids)
                self.metrics.stage(name, player, ids)
        elif kind in ("level", "give", "hp", "mana", "kill", "remove"):
            extra = {"level": {"level": op.get("level")}, "give": {"type_id": op.get("type")},
                     "hp": {"value": op.get("value")}, "mana": {"value": op.get("value")}}.get(kind, {})
            for uid in units if units is not None else [op["unit_id"]]:
                await self.bridge.call("debug", op=kind, args={"unit_id": uid, **extra})
        elif kind == "item":
            await self.bridge.call("debug", op="item", args={"type_id": op["type"], **self._place(op)})
        else:
            args = {k: v for k, v in op.items() if k not in ("op", "player", "other", "type")}
            if "type" in op:
                args["type_id"] = op["type"]
            if isinstance(args.get("paused"), bool):
                args["paused"] = int(args["paused"])
            args["player"] = self._staged_player(op.get("player"))
            if "other" in op:
                args["other"] = self._staged_player(op["other"])
            await self.bridge.call("debug", op=kind, args=args)

    def _staged_player(self, ref) -> int:
        """A stage op's player: a player number, an agent's name, or `opponent` (the first player not on the first
        agent's team); by default the first agent."""
        lead = next(x for x in self.players if x["slot"] == self.lead)
        if ref is None:
            return self.lead
        if isinstance(ref, int):
            return ref
        if ref == "opponent":
            return next(x["slot"] for x in self.players if x["team"] != lead["team"])
        player = next((x for x in self.players if x["agent"] == ref), None)
        if player is None:
            raise ValueError(f"no player {ref!r}")
        return player["slot"]

    def _place(self, spec: dict) -> dict:
        """A named place as x, y: wc3agent's names, from the first agent's start, so a drill works from
        either start location."""
        home = self.metrics.home(self.obs.get(self.lead) or {})
        if home is None:
            raise ValueError("the first agent has no hall to find its start from")
        info = render.map_info(self.scenario["map"])

        def named(word: str) -> dict:
            if word == "home":
                return home
            if word == "enemy_home":
                return next(s for s in self.metrics.starts if s is not home)
            if word == "nearest_camp":
                return min(self.metrics.camps, key=lambda c: math.dist((c["x"], c["y"]), (home["x"], home["y"])))
            if word.startswith("camp:"):
                return next(c for c in self.metrics.camps if c["number"] == int(word[5:]))
            if word.startswith("building:"):
                found = [b for b in info.get("neutral_buildings") or () if b.get("name") == word[9:]]
                return min(found, key=lambda b: math.dist((b["x"], b["y"]), (home["x"], home["y"])))
            raise ValueError(f"unknown place {word!r}")

        anchor = spec.get("at", "home")
        if anchor.startswith("toward:"):
            word, short = anchor[len("toward:"):].rsplit(":", 1)
            target = named(word)
            gap = math.dist((home["x"], home["y"]), (target["x"], target["y"]))
            k = max(0.0, (gap - float(short)) / gap) if gap else 0.0
            point = {"x": home["x"] + (target["x"] - home["x"]) * k, "y": home["y"] + (target["y"] - home["y"]) * k}
        else:
            point = named(anchor)
        return {"x": float(point["x"] + spec.get("dx", 0)), "y": float(point["y"] + spec.get("dy", 0))}

    # ---- the urn:rts:* session (agentenv_rts.session): a program plays through raw observations and actions ----

    @extension(OBSERVE, description="The game as a program plays it: at a player slot's address, its own raw wc3env "
                                    "observation (every player's for an omniscient player slot), its `player_id` and "
                                    "the player_slots; at the root, every player's; whether it is over, and the "
                                    "scenario and setup it was started with.")
    async def session_observe(self) -> dict:
        self.stats["extension_calls"] += 1
        async with self.lock:
            await self._session_game()
            return self._session_state(self._session_player())

    @extension(STEP, description="Send raw wc3env actions ({slot: [action]}; at a player slot's address, its own "
                                 "player number's) and step the game `ms` milliseconds (default 1000; with several "
                                 "agents, the game moves when every one has stepped; in realtime the game runs on its "
                                 "own clock and this only sends and observes). Orders that no longer apply are "
                                 "dropped and reported as rejected, like the game's own refusals, by their index in "
                                 "the batch.")
    async def session_step(self, actions: dict | None = None, ms: int | None = None) -> dict:
        self.stats["extension_calls"] += 1
        ms = DEFAULT_STEP_MS if ms is None else int(ms)
        if not 25 <= ms <= MAX_ADVANCE_SECONDS * 1000:
            raise ValueError(f"ms must be 25 to {MAX_ADVANCE_SECONDS * 1000}")
        async with self.lock:
            await self._session_game()
            player = self._session_player()
            if self._out(player["slot"]) if player else self._ended():
                return {**self._session_state(player), "rejected": {}, "placements": {}, "elapsed_ms": 0}
            given = {int(k): list(v or ()) for k, v in (actions or {}).items()}
            if player is not None and set(given) - {player["slot"]}:
                raise ValueError(f"a player slot orders only its own units: player {player['slot']}")
            sent, dropped = {}, {}
            for slot, batch in given.items():
                if slot not in self.obs:
                    raise ValueError(f"slot {slot} is not a player in this game")
                sent[slot], dropped[slot] = await self._still_valid_indexed(slot, batch)
        before = self._seconds()
        ms = max(25, min(ms, round((self.scenario["time_limit_seconds"] - before) * 1000) // 25 * 25))
        try:
            if player is not None:
                mine = player["slot"]
                infos = {mine: await self._play(mine, [a for _, a in sent.get(mine, [])], ms)}
            else:
                await self._ready(self.lead)
                self.last_move[self.lead] = self._seconds()
                self.step_info = {slot: {"rejected": [], "placements": [], "sent": len(k)} for slot, k in sent.items()}
                await self._chunk({slot: [a for _, a in k] for slot, k in sent.items()}, ms / 1000)
                infos, self.step_info = self.step_info, {}
        except WorkerError as e:
            if e.code in DEAD or e.code == "game_failed":
                self.failed = self.failed or e.message
            raise RuntimeError(f"{e.code}: {e.message}") from e
        rejected, placements = {}, {}
        for slot, kept in sent.items():
            index = [i for i, _ in kept]
            info = infos.get(slot) or {"rejected": [], "placements": []}
            rejected[str(slot)] = sorted([{**r, "index": index[r["index"]]} for r in info["rejected"]
                                          if 0 <= r.get("index", -1) < len(index)] + dropped[slot],
                                         key=lambda r: r["index"])
            placements[str(slot)] = [{**p, "index": index[p["index"]]} for p in info["placements"]
                                     if 0 <= p.get("index", -1) < len(index)]
            self.stats["orders_rejected"] += len(dropped[slot])
        self.stats["session_steps"] += 1
        return {**self._session_state(player), "rejected": rejected, "placements": placements,
                "elapsed_ms": round((self._seconds() - before) * 1000)}

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
    async def session_note(self, kind: str, text: str = "", slot: int | None = None, data: dict | None = None) -> dict:
        self.stats["extension_calls"] += 1
        if kind not in NOTE_KINDS:
            raise ValueError(f"kind must be one of {', '.join(NOTE_KINDS)}")
        text = " ".join(str(text or "").split())[:NOTE_CHARS]
        async with self.lock:
            if self.timeline is None or self.feed is None:
                raise RuntimeError("no game has started")
            player = self._session_player()
            slot = player["slot"] if player is not None else self.lead if slot is None else slot
            if kind == "player" and text:
                for shown in self.timeline.static["players"]:
                    if shown["slot"] == slot:
                        shown["label"] = text
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

    def _session_player(self) -> dict | None:
        try:
            return self._playing()
        except WorkerError as e:
            raise RuntimeError(f"{e.code}: {e.message}") from e

    def _session_state(self, player: dict | None) -> dict:
        """The game as `player` sees it: its own observation (every one for an omniscient player or at the root), and
        done once its own game is over."""
        shown = None if player is None or player["omniscient"] else {player["slot"]}
        state = {"observations": {str(k): v for k, v in self.obs.items() if shown is None or k in shown},
                 "done": self._out(player["slot"]) if player else self._ended(),
                 "result": self._result_of(player["slot"]) if player else self.result,
                 "scenario": self.scenario, "setup": self.setup,
                 "time_limit_seconds": self.scenario["time_limit_seconds"],
                 "player_slots": [player_slot(x) for x in self.players]}
        return {**state, "player_id": str(player["slot"])} if player else state

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

    # ---- spectators: the spectator view and the match's files (agentenv_rts) ----

    @spectator_card
    def spectators(self) -> EnvironmentCard:
        """The game alone, full-frame, as a broadcast frames it (/live?view): the game's own picture with the map as
        its minimap (client_view), else the map."""
        return EnvironmentCard(name=f"{self._served_card().name}/spectators",
                               additionalInterfaces=[EnvironmentInterface(url="/live?view", transport="http")],
                               capabilities=EnvironmentCapabilities(operations=[]))

    @match_files
    async def kept(self, kinds: list[str] | None) -> MatchFiles:
        """The finished match's files, `kinds` of FILE_KINDS or DEFAULT_FILE_KINDS. client_video and highlights are
        the game's own picture (client_view), so asking for them ends its capture."""
        wanted = tuple(dict.fromkeys(kinds or DEFAULT_FILE_KINDS))
        if unknown := sorted(set(wanted) - set(FILE_KINDS)):
            raise ValueError(f"Warcraft III keeps {', '.join(FILE_KINDS)}, not {unknown}")
        async with self.lock:
            timeline = self.timeline
            if timeline is None or not timeline.frames:
                return MatchFiles(notes=["no game has been played"])
            stem = re.sub(r"[^A-Za-z0-9_.-]", "", f"wc3-{Path(self.scenario['map']).stem}-{timeline.static['game']}")
            client = (await asyncio.to_thread(self.capture.stop)
                      if {"client_video", "highlights"} & set(wanted) and self.capture is not None else None)
            replay = await self._replay() if "replay" in wanted else None
        if self.recordings is None:
            self.recordings = Path(tempfile.mkdtemp(prefix="wc3-match-files-"))
        folder = self.recordings / "latest"
        shutil.rmtree(folder, ignore_errors=True)
        made, notes = await asyncio.to_thread(rts_recording.files, timeline, folder, stem,
                                              tuple(k for k in wanted if k in rts_recording.KINDS), client)
        files = [MatchFile(name=f["name"], kind=f["kind"], content_type=f["content_type"], file=f["path"])
                 for f in made]
        if isinstance(replay, bytes):
            (path := folder / f"{stem}.w3g").write_bytes(replay)
            files.append(MatchFile(name=path.name, kind="replay", file=path))
        elif replay is not None:
            notes.append(replay)
        return MatchFiles(files=files, notes=notes)

    async def _replay(self) -> bytes | str:
        """The game's native replay, or why there is none; it ends the game's own recording of itself."""
        if self.bridge is None or not self.game_over:
            return "no replay: the game did not reach its end"
        try:
            f = await self.bridge.call("replay")
        except WorkerError as e:
            return f"no replay: {e.message}"   # e.g. the fake game records none
        return base64.b64decode(f["base64"])

    def create_app(self):
        app = super().create_app()
        app.custom_route("/live", methods=["GET"])(self._live_page)
        app.custom_route("/live/data.json", methods=["GET"])(self._live_data)
        app.custom_route("/live/client", methods=["GET"])(self._live_client)
        app.custom_route("/live/client.jpg", methods=["GET"])(self._live_client_frame)
        return app

    async def _live_page(self, request: Request) -> Response:
        return HTMLResponse(rts_live.page(), headers={"Cache-Control": "no-cache"})

    async def _live_data(self, request: Request) -> Response:
        return JSONResponse(rts_live.data(self.timeline, request.query_params.get("since"), self.capture is not None,
                                          self._waiting()),
                            headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"})

    def _who(self, player: dict) -> str:
        """A player as people say it: "Claude Sonnet 5.5 (human)", "the game's normal orc AI"."""
        race = player["race"].replace("_", " ")
        if player["computer"]:
            return f"the game's own {player['computer']} {race} AI"
        label = next((p["label"] for p in (self.timeline.static["players"] if self.timeline else ())
                      if p["slot"] == player["slot"]), None)
        return f"{label or player['agent'] or 'an AI agent'} ({race})"

    def _briefing(self, slot: int) -> str:
        """Who an agent is and how its game ends, for get_state's first lines: a prompt need not repeat the match."""
        me = next(x for x in self.players if x["slot"] == slot)
        allies = [self._who(x) for x in self.players if x["team"] == me["team"] and x is not me]
        enemies = [self._who(x) for x in self.players if x["team"] != me["team"]]
        return (f"You are player slot {slot}, {me['race'].replace('_', ' ')}, team {me['team']}"
                + (f", with {', '.join(allies)}" if allies else "")
                + f". Against: {', '.join(enemies) or 'nobody'}. The game ends when a side has no buildings left, or "
                f"at the {self.scenario['time_limit_seconds'] // 60}-minute limit, where the higher score is ahead."
                + ("" if self.begun or self._out(slot) else
                   " The clock starts once every player has made its first move: plan as long as you like; your "
                   "first advance sends your opening orders."))

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
        if self.recordings is not None:
            shutil.rmtree(self.recordings, ignore_errors=True)


async def _now(value):
    return value


async def _raise(error: Exception):
    raise error


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
