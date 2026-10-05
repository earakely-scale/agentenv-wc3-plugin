"""`save_rts_recording`: ask a deployed RTS env for its finished game's recording (`urn:rts:recording/v1`: the
map video and the spectator page with the game embedded) and store each file as a `file` artifact."""

from __future__ import annotations

import asyncio
import base64
import logging
import uuid
from typing import ClassVar

from agent_env.artifact import FileArtifact
from agent_env.entity_refs import EntityRef
from agent_env.task_step.context import TaskStepContext
from agent_env.task_step.task_step import TaskStep
from agentenv_protocol import client

from .recording import RECORDING

log = logging.getLogger(__name__)
FORMATS = ("mp4", "html")


class SaveRTSRecordingTaskStep(TaskStep):
    type: ClassVar[str] = "save_rts_recording"
    entity_refs = (EntityRef.env("env_id"),)

    def __init__(self, id: str, version: int | None, env_id: str, formats: list[str] | None = None,
                 timeout_seconds: int = 1800, depends_on: list | None = None, fail_task_on_error: bool = False):
        super().__init__(id, version, depends_on=depends_on, fail_task_on_error=fail_task_on_error)
        self.env_id, self.timeout_seconds = env_id, timeout_seconds
        self.formats = list(formats) if formats is not None else list(FORMATS)
        if unknown := sorted(set(self.formats) - set(FORMATS)):
            raise ValueError(f"save_rts_recording formats must be among {', '.join(FORMATS)}, got {unknown}")

    def to_dict(self) -> dict:
        return {**super().to_dict(), "env_id": self.env_id, "formats": self.formats,
                "timeout_seconds": self.timeout_seconds}

    @classmethod
    def from_dict(cls, data: dict) -> SaveRTSRecordingTaskStep:
        return cls(**{**cls._base_from_dict(data), "fail_task_on_error": data.get("fail_task_on_error", False)},
                   env_id=data["env_id"], formats=data.get("formats"),
                   timeout_seconds=data.get("timeout_seconds", 1800))

    async def execute(self, context: TaskStepContext) -> TaskStepContext:
        deployed = next((d for d in context.deployed_envs if d.env_id == self.env_id), None)
        if deployed is None:
            raise RuntimeError(f"env {self.env_id!r} is not deployed in this run")
        own = deployed.environment_card or {}
        card = next((c for c in [own, *(own.get("children_environments") or [])]
                     if client.find_extension(c, RECORDING)), None)
        if card is None:
            raise RuntimeError(f"env {self.env_id!r} does not advertise {RECORDING}")
        result = await client.invoke_extension(deployed.environment_url, card, RECORDING,
                                               {"formats": self.formats}, timeout=self.timeout_seconds)
        for note in (result or {}).get("notes") or []:
            log.warning("save_rts_recording: %s", note)
        stem = f"{context.metadata.get('task_id', self.env_id)}-recording-{context.instance_id or uuid.uuid4().hex}"
        saved = []
        for f in (result or {}).get("files") or []:
            content = base64.b64decode(f["base64"])
            artifact = await asyncio.to_thread(
                FileArtifact.put_bytes, f"{stem}.{f['name'].rpartition('.')[2]}",
                description=f"Spectator recording of env {self.env_id!r}", filename=f["name"], content=content,
                content_type=f["content_type"])
            saved.append({"name": f["name"], "artifact_id": artifact.id, "version": artifact.version,
                          "bytes": len(content)})
            log.info("save_rts_recording: %s (%d bytes) is file artifact %s v%d", f["name"], len(content),
                     artifact.id, artifact.version)
        context.metadata.setdefault("recordings", {})[self.id] = saved
        return context
