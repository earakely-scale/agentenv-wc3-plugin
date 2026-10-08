"""The plugin's task step: `save_wc3_replay` stores the finished game's replay as a file artifact. The rest of a
WC3 task is agentenv_game's: add_license (your activation files), open_lobby, add_player_slot, close_lobby and
finish_match."""

from __future__ import annotations

import asyncio
import base64
import logging
import uuid
from typing import ClassVar

from agent_env.artifact import FileArtifact
from agent_env.entity_refs import EntityRef
from agent_env.env.env import DeployedEnv
from agent_env.task_step.context import TaskStepContext
from agent_env.task_step.task_step import TaskStep
from agentenv_protocol import client

log = logging.getLogger(__name__)

REPLAY_EXTENSION = "urn:wc3:replay/v1"


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
