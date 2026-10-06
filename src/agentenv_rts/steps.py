"""The RTS task steps any game's env can use (who plays is the env's lobby: agentenv_game's add_player_slot and
start_match):
- `rts_finish` settles the game once its agents have stopped playing (`urn:rts:finish/v1`): played out, forfeited
  or left as it is, before it is graded.
- `rts_broadcast` broadcasts the game while it is played (agentenv_rts.broadcast), to Twitch or X or only recorded,
  and stores the broadcast's video as a file artifact.
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
from agentenv_protocol import client

from . import broadcast
from .recording import RECORDING
from .session import FINISH, FINISH_RULES, HOLD, error_message

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


class RTSFinishTaskStep(TaskStep):
    """Settle the game once its agents have stopped playing, before it is graded (put it after every play step):
    `play_out` (default) lets it run with no orders to its end, so every game is graded at its end; `forfeit` gives
    every agent seat without a result a defeat; `as_is` leaves it where it stopped. A game already over is left as
    it is."""

    type: ClassVar[str] = "rts_finish"
    entity_refs = (EntityRef.env("env_id"),)

    def __init__(self, id: str, version: int | None, env_id: str, rule: str = "play_out",
                 timeout_seconds: int = 7200, depends_on: list | None = None, fail_task_on_error: bool = True):
        super().__init__(id, version, depends_on=depends_on, fail_task_on_error=fail_task_on_error)
        self.env_id, self.rule, self.timeout_seconds = env_id, rule, timeout_seconds
        if rule not in FINISH_RULES:
            raise ValueError(f"rts_finish rule must be one of {', '.join(FINISH_RULES)}, got {rule!r}")

    def to_dict(self) -> dict:
        return {**super().to_dict(), "env_id": self.env_id, "rule": self.rule, "timeout_seconds": self.timeout_seconds}

    @classmethod
    def from_dict(cls, data: dict) -> RTSFinishTaskStep:
        return cls(**{**cls._base_from_dict(data), "fail_task_on_error": data.get("fail_task_on_error", True)},
                   env_id=data["env_id"], rule=data.get("rule", "play_out"),
                   timeout_seconds=data.get("timeout_seconds", 7200))

    async def execute(self, context: TaskStepContext) -> TaskStepContext:
        step = self.from_dict({**self.to_dict(), **self.step_param_overrides(context)})
        deployed = _deployed(context, step.env_id)
        try:
            result = await client.invoke_extension(deployed.environment_url, _card(deployed, FINISH), FINISH,
                                                   {"rule": step.rule}, timeout=step.timeout_seconds)
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"env {step.env_id!r} did not finish the game: {error_message(e.response.text)}") from e
        context.metadata.setdefault("rts_finish", {})[self.id] = result
        log.info("rts_finish: %s from %s to %s game seconds (open seats: %s)", step.rule, result.get("from_seconds"),
                 result.get("to_seconds"), [x.get("agent") or x.get("slot") for x in result.get("open_seats") or ()])
        return context


class RTSBroadcastTaskStep(TaskStep):
    """Broadcast the game while it is played, and keep its video: start it with the env (it depends on deploy_env),
    beside the agents and the match. It holds the game's start (urn:rts:hold/v1, where the env takes it) until the
    stream is live, so the broadcast has the game from its first move. The streamer shows the env's /live?stream,
    with its casters (`cast`), to `to` (twitch, x; empty, the default, only records) and ends `linger_seconds`
    after GAME OVER. It also ends when the game clock has stood still for
    `stall_seconds` (a run whose play failed, so its game never ends), or when the step is cancelled. The video
    becomes a file artifact (what a streamer that died had recorded too); stream keys come from agent-env's secret
    store. A broadcast that fails is noted in `broadcast_errors` and the run goes on, unless `fail_task_on_error`
    says it should fail the task."""

    type: ClassVar[str] = "rts_broadcast"
    entity_refs = (EntityRef.env("env_id"),)
    OPTIONS = ("to", "cast", "caster_model", "title", "size", "fps", "bitrate", "linger_seconds", "test", "record",
               "key_secret", "x_server_secret", "x_key_secret", "wait_seconds", "stall_seconds")
    POLL_SECONDS = 10.0
    LIVE_SECONDS = 120.0   # the longest it holds the start for the stream to come up

    def __init__(self, id: str, version: int | None, env_id: str, to: list[str] | None = None, cast: bool = True,
                 caster_model: str = broadcast.CASTER_MODEL, title: str | None = None, size: str = "1920x1080",
                 fps: int = 30, bitrate: str = "4500k", linger_seconds: int = 60, test: bool = False,
                 record: bool = True, key_secret: str = broadcast.STREAM_KEY,
                 x_server_secret: str = broadcast.X_SERVER, x_key_secret: str = broadcast.X_STREAM_KEY,
                 wait_seconds: int = 1800, stall_seconds: int = 900, depends_on: list | None = None,
                 fail_task_on_error: bool = False):
        super().__init__(id, version, depends_on=depends_on, fail_task_on_error=fail_task_on_error)
        self.env_id, self.to = env_id, list(to or [])
        self.cast, self.caster_model, self.title, self.size, self.fps = cast, caster_model, title, size, fps
        self.bitrate, self.linger_seconds, self.test, self.record = bitrate, linger_seconds, test, record
        self.key_secret, self.x_server_secret, self.x_key_secret = key_secret, x_server_secret, x_key_secret
        self.wait_seconds, self.stall_seconds = wait_seconds, stall_seconds
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
        url = f"{deployed.environment_url.rstrip('/')}/live"
        own = deployed.environment_card or {}
        card = next((c for c in [own, *(own.get("children_environments") or [])] if client.find_extension(c, HOLD)),
                    None)

        async def hold(on: bool) -> None:
            if card is not None:
                await client.invoke_extension(deployed.environment_url, card, HOLD,
                                              {"name": self.id, "hold": on, "seconds": self.wait_seconds + 900})

        try:
            await hold(True)
            return await self._broadcast(context, url, hold)
        except (RuntimeError, OSError, httpx.HTTPError) as e:   # BroadcastError is a RuntimeError
            if self.fail_task_on_error:
                raise
            log.error("rts_broadcast: %s", e)
            context.metadata.setdefault("broadcasts", {})[self.id] = []
            context.metadata.setdefault("broadcast_errors", {})[self.id] = str(e)
            return context
        finally:
            try:
                await hold(False)
            except (RuntimeError, OSError, httpx.HTTPError) as e:   # the env may be gone already
                log.info("rts_broadcast: letting go of the start: %s", e)

    async def _broadcast(self, context: TaskStepContext, url: str, hold) -> TaskStepContext:
        env = await asyncio.to_thread(self.show.environment)
        image = broadcast.image()
        if not broadcast.image_exists(image):
            log.info("rts_broadcast: building %s (a few minutes the first time)", image)
            await asyncio.to_thread(broadcast.build, image)
        began = time.monotonic()
        while (state := await asyncio.to_thread(broadcast.state, url)) is None or state.get("t") is None:
            if time.monotonic() - began > self.wait_seconds:
                raise RuntimeError(f"rts_broadcast: no game started at {url} in {self.wait_seconds} s")
            await asyncio.sleep(self.POLL_SECONDS / 2)
        saved = []
        if state.get("game_over"):
            log.warning("rts_broadcast: the game at %s is already over: nothing to broadcast", url)
        else:
            stem = f"{context.metadata.get('task_id', self.env_id)}-broadcast-{context.instance_id or uuid.uuid4().hex}"
            with tempfile.TemporaryDirectory(prefix="rts-broadcast-") as tmp:
                code = await self._stream(url, image, env, Path(tmp) if self.record else None, hold)
                videos = sorted(Path(tmp).glob("*.mp4")) or sorted(Path(tmp).glob("*.mkv"))   # a cut-off one
                for path in videos:
                    artifact = await asyncio.to_thread(FileArtifact.put, f"{stem}-{path.name}",
                                                       description=f"Broadcast of env {self.env_id!r}",
                                                       file_path=str(path))
                    saved.append({"name": path.name, "artifact_id": artifact.id, "version": artifact.version,
                                  "bytes": path.stat().st_size})
            if code:
                log.warning("rts_broadcast: the streamer ended with an error (%s); kept what it recorded", code)
        context.metadata.setdefault("broadcasts", {})[self.id] = saved
        if not saved and self.record and not state.get("game_over"):
            raise RuntimeError("rts_broadcast: the broadcast recorded nothing")
        return context

    async def _stream(self, url: str, image: str, env: dict[str, str], record_dir: Path | None, hold) -> int:
        """Runs the streamer to its end, ending it early if the game clock stands still for stall_seconds (or the
        step is cancelled); lets the game start once the stream is live. The streamer's exit code."""
        name = f"rts-broadcast-{uuid.uuid4().hex[:12]}"
        proc = await asyncio.create_subprocess_exec(*self.show.command(url, image, record_dir, name),
                                                    env={**os.environ, **env})
        live = asyncio.create_task(self._when_live(proc, record_dir, hold))
        log.info("rts_broadcast: %s %s%s", url, " and ".join(self.show.targets()[1]) or "recorded only",
                 ", with the casters" if self.cast else "")
        clock, moved, over = None, time.monotonic(), None
        try:
            while True:
                try:
                    return await asyncio.wait_for(proc.wait(), timeout=self.POLL_SECONDS)
                except TimeoutError:
                    pass
                state, now = await asyncio.to_thread(broadcast.state, url) or {}, time.monotonic()
                if state.get("game_over"):
                    over = over or now
                    stop = now - over > self.linger_seconds + 120   # the streamer ends itself after its linger
                elif state.get("t") != clock:
                    clock, moved, stop = state.get("t"), now, False
                else:
                    stop = now - moved > self.stall_seconds
                    if stop:
                        log.warning("rts_broadcast: the game clock has stood still for %d s: ending the broadcast",
                                    self.stall_seconds)
                if stop:
                    await _docker_stop(name)
                    return await proc.wait()
        finally:
            live.cancel()
            if proc.returncode is None:   # cancelled: let the streamer finish its recording
                await _docker_stop(name)
                await proc.wait()

    async def _when_live(self, proc, record_dir: Path | None, hold) -> None:
        """Lets the game start once the streamer is live: its recording has begun (or, streaming only, 20 s on),
        or LIVE_SECONDS have passed, or it has stopped."""
        began = time.monotonic()
        while proc.returncode is None and time.monotonic() - began < self.LIVE_SECONDS:
            if record_dir is not None and any(p.stat().st_size for p in record_dir.glob("*.mkv")):
                break
            if record_dir is None and time.monotonic() - began > 20:
                break
            await asyncio.sleep(1)
        log.info("rts_broadcast: live after %.0f s; the game may start", time.monotonic() - began)
        await hold(False)


async def _docker_stop(name: str) -> None:
    proc = await asyncio.create_subprocess_exec("docker", "stop", "-t", "60", name, stdout=asyncio.subprocess.DEVNULL,
                                                stderr=asyncio.subprocess.DEVNULL)
    await proc.wait()
