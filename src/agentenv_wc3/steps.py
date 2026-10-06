"""The plugin's task steps: `wc3_license` sends your activation files to a deployed Warcraft III env (from
agent-env's secret store, else a folder on this machine), and `save_wc3_replay` stores the finished game's replay as a
file artifact. A match is agentenv_game's create_match, add_player_slot and start_match."""

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
        raise RuntimeError(f"wc3_license: the secret store has only some activation files: {', '.join(missing)} "
                           "missing (agent-env wc3 license import stores both)")
    for f, value in found.items():
        try:
            if not base64.b64decode(value, validate=True):
                raise binascii.Error("empty")
        except binascii.Error as e:
            raise RuntimeError(f"wc3_license: the secret {names[f]} is not {f} in base64 ({e})") from e
    return found


def read_license(directory: Path) -> dict[str, str] | None:
    """The activation files as the license extension takes them: {name: base64}; None when they are not there,
    which the fake game doesn't mind and the real one refuses with a clear error."""
    missing = [n for n in LICENSE_FILES if not (directory / n).is_file() or not (directory / n).stat().st_size]
    if missing:
        log.warning("wc3_license: no Warcraft III activation files in agent-env's secret store or at %s (missing %s); "
                    "the fake game needs none, the real one does: agent-env wc3 license import stores yours", directory,
                    ", ".join(missing))
        return None
    return {n: base64.b64encode((directory / n).read_bytes()).decode() for n in LICENSE_FILES}


class WC3LicenseTaskStep(TaskStep):
    """Send your activation files (roc.w3k, tft.w3k) to a deployed env, which the game needs when start_match
    creates it: from agent-env's secret store (`license_secrets`, by default WC3_ROC_W3K and WC3_TFT_W3K, each file in
    base64), else your license folder (`license_dir`, else [plugins.agentenv-wc3] license_dir, $WC3_LICENSE_DIR or
    ~/.wc3-license). Without them the fake game plays and the real one refuses to start."""

    type: ClassVar[str] = "wc3_license"
    entity_refs = (EntityRef.env("env_id"),)

    def __init__(self, id: str, version: int | None, env_id: str, license_dir: str | None = None,
                 license_secrets: dict | None = None, timeout_seconds: int = 60, depends_on: list | None = None,
                 fail_task_on_error: bool = True):
        super().__init__(id, version, depends_on=depends_on, fail_task_on_error=fail_task_on_error)
        if license_secrets is not None and set(license_secrets) != set(LICENSE_FILES):
            raise ValueError(f"wc3_license license_secrets names the secrets of {', '.join(LICENSE_FILES)}")
        self.env_id, self.license_dir, self.timeout_seconds = env_id, license_dir, timeout_seconds
        self.license_secrets = dict(license_secrets) if license_secrets is not None else None

    def to_dict(self) -> dict:
        return {**super().to_dict(), "env_id": self.env_id, "license_dir": self.license_dir,
                "license_secrets": self.license_secrets, "timeout_seconds": self.timeout_seconds}

    @classmethod
    def from_dict(cls, data: dict) -> WC3LicenseTaskStep:
        return cls(**cls._base_from_dict(data), env_id=data["env_id"],
                   **{k: data[k] for k in ("license_dir", "license_secrets", "timeout_seconds") if k in data})

    async def execute(self, context: TaskStepContext) -> TaskStepContext:
        deployed = _deployed_env(context, self.env_id)
        files = (await asyncio.to_thread(license_from_secrets, self.license_secrets or LICENSE_SECRETS)
                 or await asyncio.to_thread(read_license, license_dir(self.license_dir)))
        if files is None:
            return context
        try:
            result = await client.invoke_extension(deployed.environment_url,
                                                   _extension_card(deployed, LICENSE_EXTENSION), LICENSE_EXTENSION,
                                                   {"files": files}, timeout=self.timeout_seconds)
        except HTTPStatusError as e:
            raise RuntimeError(f"env {self.env_id!r} refused the activation files: "
                               f"{error_message(e.response.text)}") from e
        log.info("wc3_license: the activation files went to %s (licensed: %s)", self.env_id, result.get("licensed"))
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
