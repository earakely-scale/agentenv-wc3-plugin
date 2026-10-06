"""The plugin's task steps: `wc3_match` opens a deployed Warcraft III env's lobby for a match, sending your
activation files first (from agent-env's secret store, else a folder on this machine), and `save_wc3_replay` stores
the finished game's replay as a file artifact. The match's players are the lobby's slots (agentenv_game's
add_player_slot), and start_match creates the game."""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
import uuid
from pathlib import Path
from typing import ClassVar

from agent_env.artifact import FileArtifact
from agent_env.config import get_config
from agent_env.entity_refs import EntityRef
from agent_env.env.env import DeployedEnv
from agent_env.task_step.context import TaskStepContext
from agent_env.task_step.task_step import TaskStep
from agentenv_game import LOBBY
from agentenv_protocol import client
from httpx import HTTPStatusError

from agentenv_rts.session import error_message

log = logging.getLogger(__name__)

PLUGIN = "agentenv-wc3"
LICENSE_EXTENSION = "urn:wc3:license/v1"
REPLAY_EXTENSION = "urn:wc3:replay/v1"
LICENSE_FILES = ("roc.w3k", "tft.w3k")
LICENSE_SECRETS = {"roc.w3k": "WC3_ROC_W3K", "tft.w3k": "WC3_TFT_W3K"}
DEFAULT_LICENSE_DIR = "~/.wc3-license"
MATCH_OPTIONS = ("map", "seed", "randomize_starts", "time_limit_seconds", "mode", "allow_debug", "client_view",
                 "step_ms", "lockstep")
SEATED = ("seats", "race", "opponent_race", "ai_difficulty", "labels")
"""What wc3_match took before the lobby: who plays is the lobby's slots now."""


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


def license_from_secrets(names: dict[str, str]) -> dict[str, str] | None:
    """The activation files from agent-env's secret store ([stores.secret]; each secret is a file in base64), so a
    run on any machine can start the real game: None when neither secret is there."""
    store = get_config().get_secret_store()
    found = {f: store.get(key) for f, key in names.items()}
    if not any(found.values()):
        return None
    if missing := [names[f] for f, value in found.items() if not value]:
        raise RuntimeError(f"wc3_match: the secret store has only some activation files: {', '.join(missing)} "
                           "missing (agent-env wc3 license import stores both)")
    for f, value in found.items():
        try:
            if not base64.b64decode(value, validate=True):
                raise binascii.Error("empty")
        except binascii.Error as e:
            raise RuntimeError(f"wc3_match: the secret {names[f]} is not {f} in base64 ({e})") from e
    return found


def read_license(directory: Path) -> dict[str, str] | None:
    """The activation files as the license extension takes them: {name: base64}; None when they are not there,
    which the fake game doesn't mind and the real one refuses with a clear error."""
    missing = [n for n in LICENSE_FILES if not (directory / n).is_file() or not (directory / n).stat().st_size]
    if missing:
        log.warning("wc3_match: no Warcraft III activation files in agent-env's secret store or at %s (missing %s); "
                    "the fake game needs none, the real one does: agent-env wc3 license import stores yours", directory,
                    ", ".join(missing))
        return None
    return {n: base64.b64encode((directory / n).read_bytes()).decode() for n in LICENSE_FILES}


class WC3MatchTaskStep(TaskStep):
    """Open a deployed env's lobby for a match: a map, a seed, a time limit, `mode` (`stepping`: time passes as the
    agents step, in lockstep when there are several; `realtime`: the game runs on its own clock), `allow_debug` (the
    session's debug extension may stage the game), `client_view` (the game draws itself for spectators, live and
    in the recording). Your activation files go to the env first. The players are add_player_slot steps after it
    (an agent, or the game's AI, with its faction, team, ai_level and label), and start_match creates the game."""

    type: ClassVar[str] = "wc3_match"
    entity_refs = (EntityRef.env("env_id"),)

    def __init__(self, id: str, version: int | None, env_id: str, map: str = "(2)EchoIsles.w3x",
                 seed: int | None = None, randomize_starts: bool = False, time_limit_seconds: int = 1200,
                 mode: str = "stepping", allow_debug: bool = False, client_view: bool = False,
                 step_ms: int | None = None, lockstep: dict | None = None, license_dir: str | None = None,
                 license_secrets: dict | None = None, timeout_seconds: int = 120, depends_on: list | None = None,
                 fail_task_on_error: bool = True):
        super().__init__(id, version, depends_on=depends_on, fail_task_on_error=fail_task_on_error)
        self.env_id, self.map, self.seed, self.randomize_starts = env_id, map, seed, randomize_starts
        self.time_limit_seconds, self.license_dir = time_limit_seconds, license_dir
        self.license_secrets = dict(license_secrets) if license_secrets is not None else None
        if license_secrets is not None and set(license_secrets) != set(LICENSE_FILES):
            raise ValueError(f"wc3_match license_secrets names the secrets of {', '.join(LICENSE_FILES)}")
        self.mode, self.allow_debug, self.client_view = mode, allow_debug, client_view
        self.step_ms, self.lockstep, self.timeout_seconds = step_ms, lockstep, timeout_seconds
        if mode not in ("stepping", "realtime"):
            raise ValueError(f"wc3_match mode must be stepping or realtime, got {mode!r}")

    def to_dict(self) -> dict:
        return {**super().to_dict(), "env_id": self.env_id, **{k: getattr(self, k) for k in MATCH_OPTIONS},
                "license_dir": self.license_dir, "license_secrets": self.license_secrets,
                "timeout_seconds": self.timeout_seconds}

    @classmethod
    def from_dict(cls, data: dict) -> WC3MatchTaskStep:
        if seated := [k for k in SEATED if k in data]:
            raise ValueError(f"wc3_match takes no {', '.join(seated)}: seat each player with an add_player_slot step "
                             "(faction, team, ai_level, label) and create the game with start_match")
        options = {k: data[k] for k in (*MATCH_OPTIONS, "license_dir", "license_secrets", "timeout_seconds")
                   if k in data}
        return cls(**cls._base_from_dict(data), env_id=data["env_id"], **options)

    async def execute(self, context: TaskStepContext) -> TaskStepContext:
        deployed = _deployed_env(context, self.env_id)
        files = (await asyncio.to_thread(license_from_secrets, self.license_secrets or LICENSE_SECRETS)
                 or await asyncio.to_thread(read_license, license_dir(self.license_dir)))
        overrides = {k: v for k, v in self.step_param_overrides(context).items() if k in MATCH_OPTIONS}
        settings = {**{k: getattr(self, k) for k in MATCH_OPTIONS if getattr(self, k) is not None}, **overrides}
        try:
            if files is not None:
                await client.invoke_extension(deployed.environment_url, _extension_card(deployed, LICENSE_EXTENSION),
                                              LICENSE_EXTENSION, {"files": files}, timeout=self.timeout_seconds)
            lobby = await client.invoke_extension(deployed.environment_url, _extension_card(deployed, LOBBY), LOBBY,
                                                  {"additional_settings": settings}, timeout=self.timeout_seconds,
                                                  method="open")
        except HTTPStatusError as e:
            raise RuntimeError(f"env {self.env_id!r} did not open the match: {error_message(e.response.text)}") from e
        base_url = deployed.mcp_url.removesuffix("/mcp")
        context.metadata["wc3_match"] = {"settings": lobby["additional_settings"], "live_url": f"{base_url}/live"}
        log.info("wc3_match: %s, %d game seconds, %s; up to %d players; watch it live at %s/live",
                 lobby["additional_settings"]["map"], lobby["additional_settings"]["time_limit_seconds"],
                 lobby["additional_settings"]["mode"], (lobby.get("player_slot_settings") or {}).get("max", 0),
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
