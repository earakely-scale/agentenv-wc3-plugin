"""The env's side of the worker protocol (worker.py, docs/protocol.md): one worker process, one request at a time."""

from __future__ import annotations

import asyncio
import collections
import json
import logging

log = logging.getLogger(__name__)

READY_TIMEOUT = 120      # Wine's first start can take a while
DEFAULT_TIMEOUT = 120
TIMEOUTS = {"start": 600, "replay": 120}   # launching the game under Wine, and writing a replay
DEAD = {"worker_down", "timeout"}          # the worker is gone: start a new one


class WorkerError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


class Bridge:
    """A worker process; `call` sends one command and returns its result or raises WorkerError."""

    def __init__(self, command: list[str]):
        self.command = list(command)
        self.proc: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()
        self.next_id = 1
        self.stderr_tail: collections.deque[str] = collections.deque(maxlen=20)
        self.stderr_task: asyncio.Task | None = None

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def start(self) -> dict:
        self.proc = await asyncio.create_subprocess_exec(
            *self.command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=64 * 1024 * 1024)
        self.stderr_task = asyncio.create_task(self._drain_stderr())
        reply = await self._read(0, READY_TIMEOUT)
        return reply["result"]

    async def call(self, cmd: str, **args) -> dict:
        async with self.lock:
            if not self.alive:
                raise WorkerError("worker_down", self._down_message())
            rid, self.next_id = self.next_id, self.next_id + 1
            self.proc.stdin.write((json.dumps({"id": rid, "cmd": cmd, "args": args}) + "\n").encode())
            try:
                await self.proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as e:
                raise WorkerError("worker_down", self._down_message()) from e
            reply = await self._read(rid, TIMEOUTS.get(cmd, DEFAULT_TIMEOUT))
        if not reply.get("ok"):
            error = reply.get("error") or {}
            raise WorkerError(error.get("code", "worker_error"), error.get("message", "the worker failed"))
        return reply.get("result") or {}

    async def _read(self, rid: int, timeout: float) -> dict:
        try:
            async with asyncio.timeout(timeout):
                while True:
                    line = await self.proc.stdout.readline()
                    if not line:
                        await self.proc.wait()
                        raise WorkerError("worker_down", self._down_message())
                    try:
                        reply = json.loads(line)
                    except ValueError:
                        log.debug("worker: %s", line.decode(errors="replace").rstrip())
                        continue
                    if reply.get("id") == rid:
                        return reply
        except TimeoutError as e:
            self.proc.kill()
            raise WorkerError("timeout", f"the game did not answer within {timeout:.0f} s") from e

    async def _drain_stderr(self) -> None:
        async for line in self.proc.stderr:
            text = line.decode(errors="replace").rstrip()
            self.stderr_tail.append(text)
            log.debug("worker: %s", text)

    def _down_message(self) -> str:
        code = self.proc.returncode if self.proc else None
        tail = "\n".join(self.stderr_tail)
        return f"the game worker exited ({code})" + (f":\n{tail}" if tail else "")

    async def close(self) -> None:
        if self.alive:
            try:
                await asyncio.wait_for(self.call("close"), 30)
            except (WorkerError, TimeoutError):
                pass
            if self.alive:
                self.proc.stdin.close()
                try:
                    await asyncio.wait_for(self.proc.wait(), 10)
                except TimeoutError:
                    self.proc.kill()
        if self.stderr_task:
            self.stderr_task.cancel()
