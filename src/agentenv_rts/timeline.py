"""A game as spectators see it: the map once (`static`) and a compact frame per step, whatever the RTS. The live
view (live.py) and the recording (recording.py) draw from it; a game adapter fills it from its own observations.

static: {"game", "title", "map", "bounds": {min_x, min_y, max_x, max_y}, "terrain": {"origin": [x, y], "cell",
"width", "height", "codes"} (one byte per cell, base64: 0 open, 1 open but not buildable, 2 water, 3 blocked; rows
from the bottom), "trees": [[x, y]], "points": [{"kind": "gold" | "start" | "camp" | "shop", "x", "y", "label"}],
"players": [{"slot", "label", "race", "controller", "color"}], "types": {type id: name}, "time_limit"}

frame: {"t": game seconds, "players": {slot: {"gold", "lumber", "food": [used, cap], "score", "units",
"structures"}}, "units": [[id, owner slot, type id, x, y, hp %, kind]] (kind: 0 unit, 1 structure, 2 hero, 3
worker), "events": [text], "result"}
"""

from __future__ import annotations

UNIT, STRUCTURE, HERO, WORKER = 0, 1, 2, 3
COLORS = ["#e0473d", "#3d7fe0", "#22b0a0", "#8a45c4", "#e6c83c", "#e8892e", "#55c454", "#e65fa8", "#9c9c9c",
          "#7fb6e6", "#2a6e3e", "#7a5232"]
NEUTRAL = "#b8b8b8"
MIN_FRAME_SECONDS = 0.5


class Timeline:
    """One game's spectator record. `add` keeps a frame at most every MIN_FRAME_SECONDS of game time, so a realtime
    game observed many times a second stays small; the last frame and any game end are always kept."""

    def __init__(self, static: dict):
        self.static = static
        self.frames: list[dict] = []
        self.version = 0

    def add(self, frame: dict) -> None:
        last = self.frames[-1] if self.frames else None
        if last is not None and frame["t"] < last["t"]:
            return
        kept = self.frames[-2] if len(self.frames) > 1 else None   # the newest frame no later one replaces
        if (kept is not None and frame["t"] - kept["t"] < MIN_FRAME_SECONDS
                and not any(f.get(k) for f in (frame, last) for k in ("events", "result"))):
            self.frames[-1] = frame
        else:
            self.frames.append(frame)
        self.version += 1

    @property
    def last(self) -> dict | None:
        return self.frames[-1] if self.frames else None

    def doc(self, since: float | None = None) -> dict:
        """The static map and the frames after `since` (all of them when None), with where the game stands."""
        frames = self.frames if since is None else [f for f in self.frames if f["t"] > since]
        last = self.last or {}
        return {"static": self.static, "frames": frames,
                "live": {"t": last.get("t"), "result": last.get("result") or "", "over": bool(last.get("result")),
                         "frames": len(self.frames)}}


def player_color(slot: int) -> str:
    return COLORS[slot % len(COLORS)] if slot >= 0 else NEUTRAL
