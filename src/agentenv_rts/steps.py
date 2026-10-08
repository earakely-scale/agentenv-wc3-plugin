"""The RTS task steps any game's env can use (who plays is the env's lobby, and how the match goes its match:
agentenv_game's open_lobby, add_player_slot, close_lobby and finish_match):
- `rts_broadcast` starts broadcasting the match (agentenv_rts.broadcast), to Twitch or X or only recorded, and
  returns once the stream is live, so the match's players start on air; `save_rts_broadcast` waits for the broadcast
  to end and stores its video as a file artifact.
- `save_rts_recording` asks the env for its finished game's recording (`urn:rts:recording/v1`: the map video, the
  spectator page with the game embedded, the timeline as JSON, with `client` the game's own picture and with
  `highlights` a reel cut from it) and stores each file as a `file` artifact. The env lists the files; each is
  streamed from the path it gives, through a temporary file, to the object store, so an hour of video never sits in
  memory (an env that sends `base64` instead still works).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import ClassVar

import httpx
from agent_env.artifact import FileArtifact
from agent_env.entity_refs import EntityRef
from agent_env.env.env import DeployedEnv
from agent_env.task_step.context import TaskStepContext
from agent_env.task_step.task_step import TaskStep
from agentenv_game import MATCH
from agentenv_protocol import client

from . import broadcast
from .recording import RECORDING

log = logging.getLogger(__name__)
FORMATS = ("mp4", "html", "timeline", "client", "highlights")
DEFAULT_FORMATS = ("mp4", "html", "timeline")


class SaveRTSRecordingTaskStep(TaskStep):
    type: ClassVar[str] = "save_rts_recording"
    entity_refs = (EntityRef.env("env_id"),)

    def __init__(self, id: str, version: int | None, env_id: str, formats: list[str] | None = None,
                 timeout_seconds: int = 1800, depends_on: list | None = None, fail_task_on_error: bool = False):
        super().__init__(id, version, depends_on=depends_on, fail_task_on_error=fail_task_on_error)
        self.env_id, self.timeout_seconds = env_id, timeout_seconds
        self.formats = list(formats) if formats is not None else list(DEFAULT_FORMATS)
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
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / Path(f["name"]).name
                if "base64" in f:
                    path.write_bytes(base64.b64decode(f["base64"]))
                else:
                    await _download(f"{deployed.environment_url.rstrip('/')}{f['path']}", path, self.timeout_seconds)
                size = path.stat().st_size
                artifact = await asyncio.to_thread(FileArtifact.put, f"{stem}-{f['name']}",
                                                   description=f"Spectator recording of env {self.env_id!r}",
                                                   file_path=str(path))
            saved.append({"name": f["name"], "artifact_id": artifact.id, "version": artifact.version, "bytes": size})
            log.info("save_rts_recording: %s (%d bytes) is file artifact %s v%d", f["name"], size, artifact.id,
                     artifact.version)
        context.metadata.setdefault("recordings", {})[self.id] = saved
        return context


async def _download(url: str, path: Path, timeout: float) -> None:
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=30)) as http, http.stream("GET", url) as r:
        r.raise_for_status()
        with path.open("wb") as out:
            async for chunk in r.aiter_bytes(1 << 20):
                out.write(chunk)


def _deployed(context: TaskStepContext, env_id: str) -> DeployedEnv:
    deployed = next((d for d in context.deployed_envs if d.env_id == env_id), None)
    if deployed is None:
        raise RuntimeError(f"env {env_id!r} is not deployed in this run")
    return deployed


def _card(deployed: DeployedEnv, uri: str) -> dict:
    """The card that advertises ``uri``: the env's own, else one of its children (an env behind a gateway)."""
    own = deployed.environment_card or {}
    card = next((c for c in [own, *(own.get("children_environments") or [])] if client.find_extension(c, uri)), None)
    if card is None:
        raise RuntimeError(f"env {deployed.env_id!r} does not advertise {uri}")
    return card


MATCH_OVER = ("finished", "cancelled", "failed")


class RTSBroadcastTaskStep(TaskStep):
    """Start broadcasting a game env's match, and return once the stream is live: put it after close_lobby, and the
    match's prompt_agent steps after it, so the broadcast has the game from its first move (the match starts at its
    players' first moves). save_rts_broadcast, after finish_match, keeps its video. The streamer shows the env's
    /live?stream, with its casters (`cast`), to `to` (twitch, x; empty, the default, only records), and ends
    `linger_seconds` after GAME OVER, or once the env has been gone a minute, so a run that stops early leaves no
    stream up. Stream keys come from agent-env's secret store. A broadcast that fails to start is noted in
    `broadcast_errors` and the run goes on, unless `fail_task_on_error` says it should fail the task."""

    type: ClassVar[str] = "rts_broadcast"
    entity_refs = (EntityRef.env("env_id"),)
    OPTIONS = ("to", "cast", "caster_model", "title", "size", "fps", "bitrate", "linger_seconds", "test", "record",
               "key_secret", "x_server_secret", "x_key_secret")
    LIVE_SECONDS = 120.0   # the longest it waits for the stream to come up

    def __init__(self, id: str, version: int | None, env_id: str, to: list[str] | None = None, cast: bool = True,
                 caster_model: str = broadcast.CASTER_MODEL, title: str | None = None, size: str = "1920x1080",
                 fps: int = 30, bitrate: str = "4500k", linger_seconds: int = 60, test: bool = False,
                 record: bool = True, key_secret: str = broadcast.STREAM_KEY,
                 x_server_secret: str = broadcast.X_SERVER, x_key_secret: str = broadcast.X_STREAM_KEY,
                 depends_on: list | None = None, fail_task_on_error: bool = False):
        super().__init__(id, version, depends_on=depends_on, fail_task_on_error=fail_task_on_error)
        self.env_id, self.to = env_id, list(to or [])
        self.cast, self.caster_model, self.title, self.size, self.fps = cast, caster_model, title, size, fps
        self.bitrate, self.linger_seconds, self.test, self.record = bitrate, linger_seconds, test, record
        self.key_secret, self.x_server_secret, self.x_key_secret = key_secret, x_server_secret, x_key_secret
        self.show = broadcast.Broadcast(destinations=tuple(self.to), key_secret=key_secret,
                                        x_server_secret=x_server_secret, x_key_secret=x_key_secret, size=size, fps=fps,
                                        bitrate=bitrate, linger=linger_seconds, cast=cast, caster_model=caster_model,
                                        title=title, bandwidth_test=test)
        if not self.to and not record:
            raise ValueError("rts_broadcast goes nowhere: give `to` (twitch, x) or keep `record`")

    def to_dict(self) -> dict:
        return {**super().to_dict(), "env_id": self.env_id, **{k: getattr(self, k) for k in self.OPTIONS},
                "fail_task_on_error": self.fail_task_on_error}

    @classmethod
    def from_dict(cls, data: dict) -> RTSBroadcastTaskStep:
        return cls(**{**cls._base_from_dict(data), "fail_task_on_error": data.get("fail_task_on_error", False)},
                   env_id=data["env_id"], **{k: data[k] for k in cls.OPTIONS if k in data})

    async def execute(self, context: TaskStepContext) -> TaskStepContext:
        deployed = _deployed(context, self.env_id)
        broadcasts = context.metadata.setdefault("broadcasts", {})
        broadcasts[self.id] = {"container": None, "recording": None, "linger_seconds": self.linger_seconds,
                               "videos": []}
        try:
            match = await _match(deployed)
            if match is None:
                raise RuntimeError(f"rts_broadcast: env {self.env_id!r} has no match: put the step after close_lobby")
            if match["status"] in MATCH_OVER:
                log.warning("rts_broadcast: the match of %s is already %s: nothing to broadcast", self.env_id,
                            match["status"])
                return context
            broadcasts[self.id].update(await self._start(f"{deployed.environment_url.rstrip('/')}/live"))
        except (RuntimeError, OSError, httpx.HTTPError) as e:   # BroadcastError is a RuntimeError
            if self.fail_task_on_error:
                raise
            log.error("rts_broadcast: %s", e)
            context.metadata.setdefault("broadcast_errors", {})[self.id] = str(e)
        return context

    async def _start(self, url: str) -> dict:
        """Starts the streamer, detached, and returns once it is live: its recording has begun (or, streaming only,
        20 s on), or LIVE_SECONDS have passed, or it has stopped."""
        env = await asyncio.to_thread(self.show.environment)
        image = broadcast.image()
        if not broadcast.image_exists(image):
            log.info("rts_broadcast: building %s (a few minutes the first time)", image)
            await asyncio.to_thread(broadcast.build, image)
        name = f"rts-broadcast-{uuid.uuid4().hex[:12]}"
        record_dir = Path(tempfile.mkdtemp(prefix="rts-broadcast-")) if self.record else None
        proc = await asyncio.create_subprocess_exec(*self.show.command(url, image, record_dir, name, detach=True),
                                                    env={**os.environ, **env}, stdout=asyncio.subprocess.DEVNULL)
        if await proc.wait():
            if record_dir is not None:
                shutil.rmtree(record_dir, ignore_errors=True)
            raise RuntimeError(f"rts_broadcast: the streamer did not start (docker run exited {proc.returncode})")
        log.info("rts_broadcast: %s %s%s", url, " and ".join(self.show.targets()[1]) or "recorded only",
                 ", with the casters" if self.cast else "")
        began = time.monotonic()
        while time.monotonic() - began < self.LIVE_SECONDS and await _running(name):
            if record_dir is not None and any(p.stat().st_size for p in record_dir.glob("*.mkv")):
                break
            if record_dir is None and time.monotonic() - began > 20:
                break
            await asyncio.sleep(1)
        log.info("rts_broadcast: live after %.0f s; the match may start", time.monotonic() - began)
        return {"container": name, "recording": str(record_dir) if record_dir is not None else None}


class SaveRTSBroadcastTaskStep(TaskStep):
    """Keep a broadcast's video: put it after finish_match. It waits for the streamer that rts_broadcast `broadcast`
    (that step's id) started to end, `linger_seconds` after GAME OVER, stopping it if the match's clock stands still
    for `stall_seconds` (a run whose play failed, so its match never ends) or the match has been over a while, then
    stores the video as a file artifact (what a streamer that died had recorded too), in the run's
    `metadata["broadcasts"]`. If it fails, the error is noted in `broadcast_errors` and the run goes on, unless
    `fail_task_on_error` says it should fail the task."""

    type: ClassVar[str] = "save_rts_broadcast"
    entity_refs = (EntityRef.env("env_id"),)
    POLL_SECONDS = 10.0

    def __init__(self, id: str, version: int | None, env_id: str, broadcast: str = "broadcast",
                 stall_seconds: int = 900, depends_on: list | None = None, fail_task_on_error: bool = False):
        super().__init__(id, version, depends_on=depends_on, fail_task_on_error=fail_task_on_error)
        self.env_id, self.broadcast, self.stall_seconds = env_id, broadcast, stall_seconds

    def to_dict(self) -> dict:
        return {**super().to_dict(), "env_id": self.env_id, "broadcast": self.broadcast,
                "stall_seconds": self.stall_seconds, "fail_task_on_error": self.fail_task_on_error}

    @classmethod
    def from_dict(cls, data: dict) -> SaveRTSBroadcastTaskStep:
        return cls(**{**cls._base_from_dict(data), "fail_task_on_error": data.get("fail_task_on_error", False)},
                   env_id=data["env_id"], **{k: data[k] for k in ("broadcast", "stall_seconds") if k in data})

    async def execute(self, context: TaskStepContext) -> TaskStepContext:
        deployed = _deployed(context, self.env_id)
        started = (context.metadata.get("broadcasts") or {}).get(self.broadcast)
        if started is None:
            raise RuntimeError(f"save_rts_broadcast: no rts_broadcast step {self.broadcast!r} ran before it")
        try:
            if started["container"]:
                await self._until_ended(deployed, started)
            started["videos"] = await self._store(context, started)
            if started["recording"] and not started["videos"]:
                raise RuntimeError("save_rts_broadcast: the broadcast recorded nothing")
        except (RuntimeError, OSError, httpx.HTTPError) as e:
            if self.fail_task_on_error:
                raise
            log.error("save_rts_broadcast: %s", e)
            context.metadata.setdefault("broadcast_errors", {})[self.broadcast] = str(e)
        return context

    async def _until_ended(self, deployed: DeployedEnv, started: dict) -> None:
        """Waits for the streamer to end, ending it if the match's clock stands still for stall_seconds, or the
        match has been over longer than the streamer lingers (or the step is cancelled)."""
        name, clock, moved, over = started["container"], None, time.monotonic(), None
        try:
            while await _running(name):
                try:
                    match = await _match(deployed)
                except (RuntimeError, httpx.HTTPError):
                    match = None   # the env is going: the streamer ends itself a minute after it has gone
                now = time.monotonic()
                if match is None or match["status"] in MATCH_OVER:
                    over = over or now
                    stop = now - over > started["linger_seconds"] + 120
                elif match["status"] == "paused" or (value := (match.get("progress") or [{}])[0].get("value")) != clock:
                    clock, moved, stop = None if match["status"] == "paused" else value, now, False
                else:
                    stop = now - moved > self.stall_seconds
                    if stop:
                        log.warning("save_rts_broadcast: the match's clock has stood still for %d s: ending the "
                                    "broadcast", self.stall_seconds)
                if stop:
                    await _docker_stop(name)
                    return
                await asyncio.sleep(self.POLL_SECONDS)
        except asyncio.CancelledError:
            await _docker_stop(name)
            raise

    async def _store(self, context: TaskStepContext, started: dict) -> list[dict]:
        if not started["recording"]:
            return []
        directory = Path(started["recording"])
        stem = f"{context.metadata.get('task_id', self.env_id)}-broadcast-{context.instance_id or uuid.uuid4().hex}"
        saved = []
        for path in sorted(directory.glob("*.mp4")) or sorted(directory.glob("*.mkv")):   # an mkv: cut off
            artifact = await asyncio.to_thread(FileArtifact.put, f"{stem}-{path.name}",
                                               description=f"Broadcast of env {self.env_id!r}", file_path=str(path))
            saved.append({"name": path.name, "artifact_id": artifact.id, "version": artifact.version,
                          "bytes": path.stat().st_size})
        shutil.rmtree(directory, ignore_errors=True)
        return saved


async def _match(deployed: DeployedEnv) -> dict | None:
    """The env's match (urn:game:match/v1); None until its lobby closes."""
    return await client.invoke_extension(deployed.environment_url, _card(deployed, MATCH), MATCH, method="get")


async def _running(name: str) -> bool:
    proc = await asyncio.create_subprocess_exec("docker", "inspect", "-f", "{{.State.Running}}", name,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    return proc.returncode == 0 and out.strip() == b"true"


async def _docker_stop(name: str) -> None:
    proc = await asyncio.create_subprocess_exec("docker", "stop", "-t", "60", name, stdout=asyncio.subprocess.DEVNULL,
                                                stderr=asyncio.subprocess.DEVNULL)
    await proc.wait()
