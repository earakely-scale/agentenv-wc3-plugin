"""The plugin's task steps: `wc3_match` starts a game in a deployed Warcraft III env, sending your activation
files with it, and `save_wc3_replay` stores the finished game's replay as a file artifact."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import uuid
from pathlib import Path
from typing import ClassVar

from agent_env.artifact import FileArtifact
from agent_env.entity_refs import EntityRef
from agent_env.env.env import DeployedEnv
from agent_env.task_step.context import TaskStepContext
from agent_env.task_step.task_step import TaskStep
from agentenv_protocol import client
from httpx import HTTPStatusError

from agentenv_rts.session import error_message

log = logging.getLogger(__name__)

PLUGIN = "agentenv-wc3"
NEW_GAME_EXTENSION = "urn:wc3:new-game/v1"
REPLAY_EXTENSION = "urn:wc3:replay/v1"
LICENSE_FILES = ("roc.w3k", "tft.w3k")
DEFAULT_LICENSE_DIR = "~/.wc3-license"
MATCH_OPTIONS = ("map", "race", "opponent_race", "ai_difficulty", "seed", "randomize_starts", "time_limit_seconds",
                 "mode", "allow_debug", "client_view", "labels")


def _deployed_env(context: TaskStepContext, env_id: str) -> DeployedEnv:
    deployed = next((d for d in context.deployed_envs if d.env_id == env_id), None)
    if deployed is None:
        raise RuntimeError(f"env {env_id!r} is not deployed in this run")
    return deployed


def _extension_card(deployed: DeployedEnv, uri: str) -> dict:
    """The card that advertises ``uri``: the env's own, else one of its children (an env behind a gateway)."""
    own = deployed.environment_card or {}
    card = next((c for c in [own, *(own.get("children_environments") or [])] if client.find_extension(c, uri)), None)
    if card is None:
        raise RuntimeError(f"env {deployed.env_id!r} does not advertise {uri}")
    return card


def license_dir(given: str | None = None) -> Path:
    """Where your activation files are: the step's `license_dir`, else `license_dir` in [plugins.agentenv-wc3] of
    .agentenv/config.toml, else $WC3_LICENSE_DIR, else ~/.wc3-license."""
    if given is None:
        try:
            from agent_env.plugins import settings

            given = settings(PLUGIN).get("license_dir")
        except Exception:   # no config file: the other places still apply
            given = None
    return Path(given or os.environ.get("WC3_LICENSE_DIR") or DEFAULT_LICENSE_DIR).expanduser()


def read_license(directory: Path) -> dict[str, str] | None:
    """The activation files as the new-game extension takes them: {name: base64}; None when they are not there,
    which the fake game doesn't mind and the real one refuses with a clear error."""
    missing = [n for n in LICENSE_FILES if not (directory / n).is_file() or not (directory / n).stat().st_size]
    if missing:
        log.warning("wc3_match: no Warcraft III activation files at %s (missing %s); the fake game needs none, the "
                    "real one does: copy roc.w3k and tft.w3k from your own installation there, or name their "
                    "directory with license_dir in [plugins.agentenv-wc3] of .agentenv/config.toml", directory,
                    ", ".join(missing))
        return None
    return {n: base64.b64encode((directory / n).read_bytes()).decode() for n in LICENSE_FILES}


class WC3MatchTaskStep(TaskStep):
    """Start a game in a deployed env: the agent's race against the game's AI, on a map, with a time limit.
    `mode` is `stepping` (time passes when the agent steps) or `realtime` (the game runs on its own clock);
    `allow_debug` lets the session's debug extension stage the game (scenarios); `client_view` draws the game for
    spectators: its picture at the live page and, with save_rts_recording's `client` format, its video; `labels`
    names the players for spectators by slot ({"1": "Orc AI"}), where the agent doesn't name itself."""

    type: ClassVar[str] = "wc3_match"
    entity_refs = (EntityRef.env("env_id"),)

    def __init__(self, id: str, version: int | None, env_id: str, map: str = "(2)EchoIsles.w3x",
                 race: str = "human", opponent_race: str = "orc", ai_difficulty: str = "normal",
                 seed: int | None = None, randomize_starts: bool = False, time_limit_seconds: int = 1200,
                 mode: str = "stepping", allow_debug: bool = False, client_view: bool = False,
                 labels: dict | None = None, license_dir: str | None = None,
                 timeout_seconds: int = 900, depends_on: list | None = None, fail_task_on_error: bool = True):
        super().__init__(id, version, depends_on=depends_on, fail_task_on_error=fail_task_on_error)
        self.env_id, self.map, self.race, self.opponent_race = env_id, map, race, opponent_race
        self.ai_difficulty, self.seed, self.randomize_starts = ai_difficulty, seed, randomize_starts
        self.time_limit_seconds, self.license_dir = time_limit_seconds, license_dir
        self.mode, self.allow_debug, self.client_view, self.labels = mode, allow_debug, client_view, labels
        self.timeout_seconds = timeout_seconds
        if mode not in ("stepping", "realtime"):
            raise ValueError(f"wc3_match mode must be stepping or realtime, got {mode!r}")

    def to_dict(self) -> dict:
        return {**super().to_dict(), "env_id": self.env_id, **{k: getattr(self, k) for k in MATCH_OPTIONS},
                "license_dir": self.license_dir, "timeout_seconds": self.timeout_seconds}

    @classmethod
    def from_dict(cls, data: dict) -> WC3MatchTaskStep:
        options = {k: data[k] for k in (*MATCH_OPTIONS, "license_dir", "timeout_seconds") if k in data}
        return cls(**cls._base_from_dict(data), env_id=data["env_id"], **options)

    async def execute(self, context: TaskStepContext) -> TaskStepContext:
        deployed = _deployed_env(context, self.env_id)
        card = _extension_card(deployed, NEW_GAME_EXTENSION)
        files = await asyncio.to_thread(read_license, license_dir(self.license_dir))
        args = {k: getattr(self, k) for k in MATCH_OPTIONS if getattr(self, k) is not None}
        if files is not None:
            args["license"] = files
        try:
            result = await client.invoke_extension(deployed.environment_url, card, NEW_GAME_EXTENSION, args,
                                                   timeout=self.timeout_seconds)
        except HTTPStatusError as e:
            raise RuntimeError(f"env {self.env_id!r} did not start the game: {error_message(e.response.text)}") from e
        scenario = (result or {}).get("scenario") or {k: v for k, v in args.items() if k != "license"}
        context.metadata["wc3_match"] = {"scenario": scenario, "setup": (result or {}).get("setup")}
        base_url = deployed.mcp_url.removesuffix("/mcp")
        context.metadata["wc3_match"]["live_url"] = f"{base_url}/live"
        log.info("wc3_match: %s on %s against the %s %s AI, %d game seconds, %s; watch it live at %s/live",
                 scenario.get("race"), scenario.get("map"), scenario.get("ai_difficulty"),
                 scenario.get("opponent_race"), scenario.get("time_limit_seconds", 0), scenario.get("mode", "stepping"),
                 base_url)
        return context


class SaveWC3ReplayTaskStep(TaskStep):
    """Ask a deployed env for its finished game's replay and store it as a ``file`` artifact."""

    type: ClassVar[str] = "save_wc3_replay"
    entity_refs = (EntityRef.env("env_id"),)

    def __init__(self, id: str, version: int | None, env_id: str, timeout_seconds: int = 300,
                 depends_on: list | None = None, fail_task_on_error: bool = False):
        super().__init__(id, version, depends_on=depends_on, fail_task_on_error=fail_task_on_error)
        self.env_id, self.timeout_seconds = env_id, timeout_seconds

    def to_dict(self) -> dict:
        return {**super().to_dict(), "env_id": self.env_id, "timeout_seconds": self.timeout_seconds}

    @classmethod
    def from_dict(cls, data: dict) -> SaveWC3ReplayTaskStep:
        return cls(**{**cls._base_from_dict(data), "fail_task_on_error": data.get("fail_task_on_error", False)},
                   env_id=data["env_id"], timeout_seconds=data.get("timeout_seconds", 300))

    async def execute(self, context: TaskStepContext) -> TaskStepContext:
        deployed = _deployed_env(context, self.env_id)
        card = _extension_card(deployed, REPLAY_EXTENSION)
        result = await client.invoke_extension(deployed.environment_url, card, REPLAY_EXTENSION, {},
                                               timeout=self.timeout_seconds)
        for note in (result or {}).get("notes") or []:
            log.warning("save_wc3_replay: %s", note)
        if not (result or {}).get("files"):
            context.metadata.setdefault("replays", {})[self.id] = []
            return context
        stem = f"{context.metadata.get('task_id', self.env_id)}-replay-{context.instance_id or uuid.uuid4().hex}"
        saved = []
        for f in result["files"]:
            content = base64.b64decode(f["base64"])
            artifact = await asyncio.to_thread(
                FileArtifact.put_bytes, f"{stem}.{f['name'].rpartition('.')[2]}",
                description=f"Warcraft III replay of env {self.env_id!r}", filename=f["name"], content=content,
                content_type=f["content_type"])
            saved.append({"name": f["name"], "artifact_id": artifact.id, "version": artifact.version,
                          "bytes": len(content)})
            log.info("save_wc3_replay: %s (%d bytes) is file artifact %s v%d", f["name"], len(content), artifact.id,
                     artifact.version)
        context.metadata.setdefault("replays", {})[self.id] = saved
        return context
