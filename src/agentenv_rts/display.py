"""The game's own picture, for an RTS that draws to an X display (Xvfb in its container): one ffmpeg reads the game
window and writes both the match's video (H.264 MP4, the recording's `client` file) and a JPEG stream that the live
page shows next to its map. A game adapter starts a Capture with its window's region once the game is up, and stops it
when the game ends."""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import threading
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path

FPS = 24
LIVE_FPS = 12
BOUNDARY = "frame"
JPEG_START, JPEG_END = b"\xff\xd8", b"\xff\xd9"
# xwininfo -tree: `0xa00005 "Warcraft III": ("warcraft iii.exe" ...)  960x540+4+30  +4+30`, the last pair absolute
WINDOW_LINE = re.compile(r'"(?P<name>[^"]*)".*?(?P<w>\d+)x(?P<h>\d+)[+-]-?\d+[+-]-?\d+\s+'
                         r'\+(?P<x>-?\d+)\+(?P<y>-?\d+)\s*$')


class DisplayError(RuntimeError):
    pass


def window_region(display: str, name: str) -> tuple[int, int, int, int] | None:
    """(x, y, width, height) of the largest window titled `name` that is on the screen, from xwininfo."""
    try:
        out = subprocess.run(["xwininfo", "-display", display, "-root", "-tree"], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    found = [(int(m["x"]), int(m["y"]), int(m["w"]), int(m["h"])) for m in map(WINDOW_LINE.search, out.splitlines())
             if m and m["name"] == name and int(m["x"]) >= 0 and int(m["y"]) >= 0]
    return max(found, key=lambda r: r[2] * r[3], default=None)


def split_jpegs(buffer: bytes) -> tuple[list[bytes], bytes]:
    """The whole JPEGs at the front of an MJPEG byte stream, and the rest (the start of the next one)."""
    frames = []
    while (end := buffer.find(JPEG_END)) >= 0:
        frame, buffer = buffer[:end + 2], buffer[end + 2:]
        if (start := frame.find(JPEG_START)) >= 0:
            frames.append(frame[start:])
    return frames, buffer


class Capture:
    """ffmpeg reading `source` (x11grab of a display region, by `of_display`): the whole match to `path`, and the
    newest frame for the live page as `latest`. `stop` ends the video cleanly; the file is then complete."""

    def __init__(self, source: list[str], path: Path, fps: int = FPS, live_fps: int = LIVE_FPS):
        self.source, self.path, self.fps, self.live_fps = source, Path(path), fps, live_fps
        self.proc: subprocess.Popen | None = None
        self.frame: bytes | None = None
        self.frames = 0
        self.errors = b""
        self.started = 0.0   # time.monotonic() when the video began: a frame's "w" is the video time it shows

    @classmethod
    def of_display(cls, display: str, region: tuple[int, int, int, int], path: Path, fps: int = FPS) -> Capture:
        x, y, w, h = region
        return cls(["-f", "x11grab", "-draw_mouse", "0", "-framerate", str(fps), "-video_size",
                    f"{w // 2 * 2}x{h // 2 * 2}", "-i", f"{display}+{x},{y}"], path, fps)

    def start(self) -> None:
        if shutil.which("ffmpeg") is None:
            raise DisplayError("ffmpeg is not installed")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats", "-y", *self.source,
                   "-map", "0:v", "-c:v", "libx264", "-preset", "veryfast", "-crf", "25", "-pix_fmt", "yuv420p",
                   "-r", str(self.fps), "-movflags", "+faststart", str(self.path),
                   "-map", "0:v", "-r", str(self.live_fps), "-c:v", "mjpeg", "-q:v", "4", "-f", "image2pipe", "-"]
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.started = time.monotonic()
        threading.Thread(target=self._read, daemon=True, name="capture-frames").start()
        threading.Thread(target=self._drain, daemon=True, name="capture-errors").start()

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def latest(self) -> bytes | None:
        return self.frame

    def stop(self, timeout: float = 60) -> Path | None:
        """Ends the video (ffmpeg's `q` writes its index) and returns it, or None when there is none."""
        proc, self.proc = self.proc, None
        if proc is None:
            return self.path if self.path.is_file() and self.path.stat().st_size else None
        if proc.poll() is None:
            try:
                proc.stdin.write(b"q")
                proc.stdin.close()
                proc.wait(timeout)
            except (OSError, subprocess.TimeoutExpired):
                proc.kill()
                proc.wait()
        return self.path if self.path.is_file() and self.path.stat().st_size else None

    def _read(self) -> None:
        rest, stream = b"", self.proc.stdout
        while chunk := stream.read1(1 << 16):
            frames, rest = split_jpegs(rest + chunk)
            if frames:
                self.frame, self.frames = frames[-1], self.frames + len(frames)

    def _drain(self) -> None:
        for line in self.proc.stderr:
            self.errors = (self.errors + line)[-2000:]


class Grab:
    """ffmpeg reading a display region as JPEGs, with no video of its own: `fresh` is the first frame read after a
    call, so a renderer that steps a game and then takes a frame gets one drawn after the step."""

    def __init__(self, display: str, region: tuple[int, int, int, int], fps: int = 30):
        x, y, w, h = region
        self.command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats", "-f", "x11grab", "-draw_mouse",
                        "0", "-framerate", str(fps), "-video_size", f"{w // 2 * 2}x{h // 2 * 2}",
                        "-i", f"{display}+{x},{y}", "-c:v", "mjpeg", "-q:v", "3", "-f", "image2pipe", "-"]
        self.proc: subprocess.Popen | None = None
        self.frame: bytes | None = None
        self.frames = 0
        self.ready = threading.Condition()
        self.errors = b""

    def start(self) -> None:
        if shutil.which("ffmpeg") is None:
            raise DisplayError("ffmpeg is not installed")
        self.proc = subprocess.Popen(self.command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        threading.Thread(target=self._read, daemon=True, name="grab-frames").start()
        threading.Thread(target=self._drain, daemon=True, name="grab-errors").start()

    def fresh(self, timeout: float = 10) -> bytes:
        """A frame read after this call began (the second one, since the first may have been on its way)."""
        with self.ready:
            after = self.frames + 1
            if not self.ready.wait_for(lambda: self.frames > after or self.proc is None or self.proc.poll() is not None,
                                       timeout):
                raise DisplayError(f"no frame from the display in {timeout} s")
            if self.frame is None or self.frames <= after:
                raise DisplayError(f"the display capture stopped: {self.errors.decode(errors='replace')[-300:]}")
            return self.frame

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
        with self.ready:
            self.ready.notify_all()

    def _read(self) -> None:
        rest, stream = b"", self.proc.stdout
        while chunk := stream.read1(1 << 16):
            frames, rest = split_jpegs(rest + chunk)
            if frames:
                with self.ready:
                    self.frame, self.frames = frames[-1], self.frames + len(frames)
                    self.ready.notify_all()
        with self.ready:
            self.ready.notify_all()

    def _drain(self) -> None:
        for line in self.proc.stderr:
            self.errors = (self.errors + line)[-2000:]


async def mjpeg(latest: Callable[[], bytes | None], fps: int = LIVE_FPS) -> AsyncIterator[bytes]:
    """multipart/x-mixed-replace parts (boundary BOUNDARY) of each new frame `latest` gives, for an <img> to show."""
    sent = None
    while True:
        frame = latest()
        if frame is not None and frame is not sent:
            sent = frame
            yield (f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(frame)}\r\n\r\n".encode()
                   + frame + b"\r\n")
        await asyncio.sleep(1 / fps)
