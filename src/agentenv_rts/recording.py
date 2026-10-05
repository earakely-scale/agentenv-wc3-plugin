"""A finished game's recording from its Timeline: an MP4 of the map, a frame per step (Pillow draws, ffmpeg
encodes), the spectator page with the whole game embedded, which plays in any browser, and the game's own picture
when the env captured it (display.py). `files()` is what an env returns from `urn:rts:recording/v1`; the
`save_rts_recording` step stores them as file artifacts."""

from __future__ import annotations

import base64
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import live
from .timeline import NEUTRAL, Timeline

RECORDING = "urn:rts:recording/v1"
TERRAIN = [(47, 74, 42), (70, 69, 47), (31, 68, 102), (18, 20, 24)]
WIDTH = 960
BAR = 64
FPS = 10


def files(timeline: Timeline, stem: str, formats: tuple[str, ...] = ("mp4", "html"),
          client: Path | None = None) -> tuple[list[dict], list[str]]:
    """The recording as `{"name", "content_type", "base64"}` files, and notes on what could not be made. `client`
    is the game's own video, for the `client` format."""
    out, notes = [], []
    if "client" in formats:
        if client is not None and client.is_file():
            out.append(_file(f"{stem}-client.mp4", "video/mp4", client.read_bytes()))
        else:
            notes.append("no client video: the game was played without its picture (client_view)")
    if "html" in formats:
        out.append(_file(f"{stem}.html", "text/html", live.standalone(timeline).encode()))
    if "mp4" in formats:
        try:
            out.append(_file(f"{stem}.mp4", "video/mp4", mp4(timeline)))
        except RecordingError as e:
            notes.append(f"no MP4: {e}")
    return out, notes


class RecordingError(RuntimeError):
    pass


def _file(name: str, content_type: str, data: bytes) -> dict:
    return {"name": name, "content_type": content_type, "base64": base64.b64encode(data).decode()}


def mp4(timeline: Timeline) -> bytes:
    if not timeline.frames:
        raise RecordingError("no game was played")
    if shutil.which("ffmpeg") is None:
        raise RecordingError("ffmpeg is not installed")
    try:
        from PIL import Image  # noqa: F401
    except ImportError as e:
        raise RecordingError("Pillow is not installed") from e
    painter = Painter(timeline.static)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "game.mp4"
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
                   "-s", f"{painter.width}x{painter.height}", "-r", str(FPS), "-i", "-", "-c:v", "libx264",
                   "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "23", "-movflags", "+faststart", str(path)]
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            for frame in timeline.frames:
                proc.stdin.write(painter.paint(frame).tobytes())
            for _ in range(FPS * 2):   # hold the last frame
                proc.stdin.write(painter.paint(timeline.frames[-1]).tobytes())
        finally:
            proc.stdin.close()
            err = proc.stderr.read().decode(errors="replace")
            proc.wait()
        if proc.returncode or not path.is_file():
            raise RecordingError(f"ffmpeg failed: {err.strip()[-300:]}")
        return path.read_bytes()


class Painter:
    """Draws frames the way the spectator page does: the terrain, trees and points once, then units on top."""

    def __init__(self, static: dict):
        from PIL import Image, ImageDraw, ImageFont

        self.static, self.bounds = static, static["bounds"]
        b = self.bounds
        self.scale = WIDTH / (b["max_x"] - b["min_x"])
        self.width = WIDTH
        self.height = BAR + (round((b["max_y"] - b["min_y"]) * self.scale) // 2 * 2)
        self.font = ImageFont.load_default(size=18)
        self.small = ImageFont.load_default(size=14)
        self.colors = {p["slot"]: p.get("color") or NEUTRAL for p in static.get("players") or []}
        base = Image.new("RGB", (self.width, self.height), (11, 13, 16))
        draw = ImageDraw.Draw(base)
        terrain = static.get("terrain")
        if terrain and terrain.get("codes"):
            codes = base64.b64decode(terrain["codes"])
            w, h = terrain["width"], terrain["height"]
            grid = Image.frombytes("RGB", (w, h), bytes(c for j in range(h - 1, -1, -1)
                                                        for code in codes[j * w:(j + 1) * w]
                                                        for c in TERRAIN[min(code, 3)]))
            x0, y0 = self.px(terrain["origin"][0], terrain["origin"][1] + h * terrain["cell"])
            size = (max(1, round(w * terrain["cell"] * self.scale)), max(1, round(h * terrain["cell"] * self.scale)))
            base.paste(grid.resize(size, Image.NEAREST), (round(x0), round(y0)))
        for x, y in static.get("trees") or []:
            px, py = self.px(x, y)
            draw.rectangle((px - 1, py - 1, px + 1, py + 1), fill=(10, 40, 18))
        for p in static.get("points") or []:
            px, py = self.px(p["x"], p["y"])
            if p["kind"] == "gold":
                draw.polygon([(px, py - 6), (px + 6, py), (px, py + 6), (px - 6, py)], fill=(242, 200, 75))
            elif p["kind"] == "start":
                draw.ellipse((px - 14, py - 14, px + 14, py + 14), outline=(200, 200, 200), width=2)
            elif p["kind"] == "camp":
                draw.rectangle((px - 2, py - 2, px + 2, py + 2), fill=(230, 90, 70))
            elif p["kind"] == "shop":
                draw.rectangle((px - 4, py - 4, px + 4, py + 4), fill=(176, 124, 232))
        draw.rectangle((0, 0, self.width, BAR), fill=(22, 27, 34))
        draw.text((12, 8), static.get("title") or "", font=self.font, fill=(216, 222, 230))
        self.base = base

    def px(self, x: float, y: float) -> tuple[float, float]:
        b = self.bounds
        return (x - b["min_x"]) * self.scale, BAR + (b["max_y"] - y) * self.scale

    def paint(self, frame: dict):
        from PIL import ImageDraw

        image = self.base.copy()
        draw = ImageDraw.Draw(image)
        for _, owner, _, x, y, _hp, kind in frame.get("units") or ():
            px, py = self.px(x, y)
            color = self.colors.get(owner, NEUTRAL)
            if kind == 1:
                draw.rectangle((px - 6, py - 6, px + 6, py + 6), fill=color, outline=(0, 0, 0))
            else:
                r = 6.5 if kind == 2 else 3 if kind == 3 else 4.5
                draw.ellipse((px - r, py - r, px + r, py + r), fill=color,
                             outline=(255, 255, 255) if kind == 2 else (0, 0, 0), width=2 if kind == 2 else 1)
        t = int(frame.get("t") or 0)
        result = (frame.get("result") or "").replace("_", " ").upper()
        draw.text((self.width - 12, 8), f"{t // 60}:{t % 60:02d}" + (f"  {result}" if result else ""),
                  font=self.font, fill=(230, 200, 60) if result else (216, 222, 230), anchor="ra")
        x = 12
        for p in self.static.get("players") or []:
            s = (frame.get("players") or {}).get(str(p["slot"])) or (frame.get("players") or {}).get(p["slot"]) or {}
            text = f"{p['label']}: {s.get('gold', '–')}g {s.get('lumber', '–')}l  {s.get('units', '–')} units"
            draw.rectangle((x, 38, x + 10, 48), fill=self.colors.get(p["slot"], NEUTRAL))
            draw.text((x + 16, 34), text, font=self.small, fill=(200, 206, 214))
            x += 24 + round(draw.textlength(text, font=self.small)) + 12
        return image
