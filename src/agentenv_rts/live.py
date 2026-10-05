"""The spectator view's page and data, for any env that keeps a Timeline: `page()` is the viewer (`GET /live`;
`?stream` lays it out 1920×1080 for a broadcast), `standalone()` the same page with a whole game embedded (the HTML
recording), and `data()` what `GET /live/data.json?since=T` returns. With `client`, the env also serves the game's
own picture at `/live/client` (display.py), and the page shows it next to the map."""

from __future__ import annotations

import json
from pathlib import Path

from .timeline import Timeline

VIEWER = Path(__file__).with_name("viewer")
EMPTY = {"static": None, "frames": [], "live": {"t": None, "result": "", "over": False, "frames": 0}}


def page(data: dict | None = None) -> str:
    html = (VIEWER / "index.html").read_text()
    script = f"window.RTS_DATA = {json.dumps(data, separators=(',', ':'))};" if data is not None else ""
    return (html.replace("/*CSS*/", (VIEWER / "app.css").read_text())
            .replace("/*DATA*/", script.replace("</", "<\\/"))
            .replace("/*JS*/", (VIEWER / "app.js").read_text()))


def standalone(timeline: Timeline) -> str:
    return page(timeline.doc())


def data(timeline: Timeline | None, since: str | None, client: bool = False) -> dict:
    if timeline is None:
        return {**EMPTY, "live": {**EMPTY["live"], "client": client}}
    try:
        after = float(since) if since not in (None, "") else None
    except ValueError:
        after = None
    doc = timeline.doc(None if after is None or after < 0 else after)
    return {**doc, "live": {**doc["live"], "client": client}}
