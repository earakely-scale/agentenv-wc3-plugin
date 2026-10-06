"""The spectator view's page and data, for any env that keeps a Timeline: `page()` is the viewer (`GET /live`;
`?stream` lays it out 1920×1080 for a broadcast), `standalone()` the same page with a whole game embedded (the HTML
recording), and `data()` what `GET /live/data.json?since=T` returns. With `client`, the env also serves the game's
own picture at `/live/client` (display.py), and the page shows it next to the map; `waiting` are the slots of the
players the game waits for before it begins (their first move), which the page names, and `holds` what else holds
its start (a broadcast going live)."""

from __future__ import annotations

import json
from pathlib import Path

from .timeline import Timeline

VIEWER = Path(__file__).with_name("viewer")
EMPTY = {"static": None, "frames": [], "live": {"t": None, "result": "", "over": False, "frames": 0, "waiting": [],
                                                "holds": []}}


def page(data: dict | None = None) -> str:
    html = (VIEWER / "index.html").read_text()
    script = f"window.RTS_DATA = {json.dumps(data, separators=(',', ':'))};" if data is not None else ""
    return (html.replace("/*CSS*/", (VIEWER / "app.css").read_text())
            .replace("/*DATA*/", script.replace("</", "<\\/"))
            .replace("/*JS*/", (VIEWER / "app.js").read_text()))


def standalone(timeline: Timeline, video: str | None = None) -> str:
    """`video` names the game's own video, a file beside the page, which then plays in step with the map."""
    doc = timeline.doc()
    return page({**doc, "video": video} if video else doc)


def data(timeline: Timeline | None, since: str | None, client: bool = False, waiting: list[int] = (),
         holds: list[str] = ()) -> dict:
    if timeline is None:
        return {**EMPTY, "live": {**EMPTY["live"], "client": client}}
    try:
        after = float(since) if since not in (None, "") else None
    except ValueError:
        after = None
    doc = timeline.doc(None if after is None or after < 0 else after)
    return {**doc, "live": {**doc["live"], "client": client, "waiting": list(waiting), "holds": list(holds)}}


HISTORY_SECONDS = 30
CAST_STATS = ("score", "army", "gold", "lumber", "food", "units", "structures")


def casting(timeline: Timeline | None, since: str | None, desk: str = "") -> dict:
    """What the casters read (`GET /live/casting.json?since=T`, the streamer's caster.py): the game in a few
    fields, whatever the RTS. `desk` tells them what game this is and how it is won; `moments` are the feed's
    events after T, `notes` what players told the spectators after T, and `history` each player's score and army
    every HISTORY_SECONDS of the whole game."""
    if timeline is None or not timeline.frames:
        return {"game": None, "desk": desk, "clock": {"t": None, "limit": None}, "over": False, "result": "",
                "players": [], "moments": [], "notes": [], "history": []}
    try:
        after = float(since) if since not in (None, "") else -1.0
    except ValueError:
        after = -1.0
    static, frames, last = timeline.static, timeline.frames, timeline.frames[-1]

    def stats(frame: dict, slot: int) -> dict:
        return (frame.get("players") or {}).get(str(slot)) or {}

    players = [{"slot": p["slot"], "label": p["label"], "faction": p.get("race"), "controller": p.get("controller"),
                "stats": {k: stats(last, p["slot"]).get(k) for k in CAST_STATS},
                "agent": stats(last, p["slot"]).get("agent")} for p in static.get("players") or ()]
    moments, notes = [], []
    for f in frames:
        if f["t"] <= after:
            continue
        for k, e in enumerate(f.get("events") or ()):
            if isinstance(e, dict):
                moments.append({"id": f"{f['t']:.2f}-{k}", "t": f["t"], "kind": e.get("kind"),
                                "major": bool(e.get("major")), "side": e.get("side"), "text": e.get("text")})
        notes += [{"t": f["t"], **n} for n in f.get("notes") or ()]
    history, mark = [], None
    for f in frames:
        if mark is None or f["t"] - mark >= HISTORY_SECONDS or f is last:
            mark = f["t"]
            history.append({"t": f["t"], "players": {str(p["slot"]): {"score": stats(f, p["slot"]).get("score"),
                                                                      "army": stats(f, p["slot"]).get("army")}
                                                     for p in static.get("players") or ()}})
    return {"game": static.get("game"), "title": static.get("title"), "desk": desk,
            "clock": {"t": last["t"], "limit": static.get("time_limit")}, "over": bool(last.get("result")),
            "result": last.get("result") or "", "players": players, "moments": moments, "notes": notes,
            "history": history}
