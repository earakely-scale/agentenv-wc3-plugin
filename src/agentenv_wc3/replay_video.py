"""A finished game's video in the game's own picture, from its replay. Runs in the env's image (`agent-env wc3 render`
starts it there): the env plays the .w3g back with the picture on (wc3env's stepping playback, which takes the
players' orders and the AI from the recording), stages it first as the task did (staging is not in a replay), points
the camera with the live game's director, shows each plan the agent wrote when it wrote it, and keeps one frame per
step of game time, so the video runs at `speed` times the game's pace.

    python -m agentenv_wc3.replay_video SPEC.json

SPEC: {"replay": the game's path to the .w3g (its .w3g.json beside it), "map", "players" (the scenario's), "seed",
"lead" (the slot the camera follows), "stage": {"ops", "warmup_seconds"} or null, "notes": [{"t", "text"}],
"speed", "fps", "out": the MP4 to write}. Beside the MP4 goes <out>.json: the frames' pace and the game's final
scores, to check the playback against the recording."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import os
import subprocess
import sys
from pathlib import Path

from agentenv_rts import display as rts_display

from . import frames
from .server import (
    DEFAULT_SCENARIO,
    DIFFICULTIES,
    LICENSE_FILES,
    OVERLAY,
    PAN_MAX,
    PAN_SECONDS,
    STAGE_EXTENSION,
    WINDOW_NAME,
    WC3Env,
    WorkerError,
    attach_license,
    check_scenario,
    wrapped,
)

log = logging.getLogger(__name__)

WIDTH = 960            # the video's width; the game's window is scaled down to it
NOTE_SECONDS = 8.0     # video seconds a plan stays on the picture


def spec_of(task: list[dict], replay: str, startup: dict, timeline: dict | None, speed: float, fps: int,
            out: str) -> dict:
    """The render's spec from the task the game played (its map, its player slots as the lobby reads them, and its
    staging), the replay's saved startup (the seed the game really had) and its timeline (the lead agent's plans)."""
    lobby = next(s for s in task if s["type"] == "open_lobby").get("game_settings", {})
    players = []
    for s in (s for s in task if s["type"] == "add_player_slot"):
        given = s.get("game_settings", {})
        player = {"slot": int(s["player_id"]), "race": given.get("faction", "random"),
                  "team": given.get("team") or int(s["player_id"]) + 1}
        if s["player_kind"] == "agent":
            player.update(agent=s["player_name"], ai_assist=given.get("ai_assist", False),
                          autocast=given.get("autocast", False), omniscient=given.get("omniscient", False))
        else:
            player["computer"] = given.get("ai_level") or DEFAULT_SCENARIO["ai_difficulty"]
        players.append(player)
    players.sort(key=lambda x: x["slot"])
    lead = next(x["slot"] for x in players if "agent" in x)
    stage = next((d["args"] for s in task if s["type"] == "apply_server_config" for d in s.get("directives", ())
                  if d.get("uri") == STAGE_EXTENSION), None)
    notes = [{"t": f["t"], "text": n["text"]} for f in (timeline or {}).get("frames", ())
             for n in f.get("notes") or () if n.get("kind") == "plan" and n.get("slot") == lead and n.get("text")]
    return {"replay": replay, "map": lobby.get("map", DEFAULT_SCENARIO["map"]), "players": players,
            "seed": (startup.get("setup") or {}).get("seed", lobby.get("seed")), "lead": lead, "stage": stage,
            "notes": notes, "speed": speed, "fps": fps, "out": out}


def startup_of(task: list[dict], startup: dict) -> dict:
    """The replay's saved startup with the AI level its game started with, as the env passes it to every game (the
    first computer player's, else normal). wc3env saves a startup without its falsy values, so an easy AI's 0 is lost
    and the playback would field the map's default level against orders given against the easy AI."""
    level = next((s.get("game_settings", {}).get("ai_level") for s in task if s["type"] == "add_player_slot"
                  and s["player_kind"] != "agent"), None) or DEFAULT_SCENARIO["ai_difficulty"]
    return {**startup, "ai_difficulty": DIFFICULTIES[level]}


class ReplayEnv(WC3Env):
    """The env with a frame grabber in place of its live capture: the renderer takes the frames, one per step."""

    grab: rts_display.Grab | None = None

    async def _start_capture(self) -> None:
        display, region, seen = os.environ.get("DISPLAY", ":99"), None, None
        for _ in range(40):   # until the window is there and done resizing
            region = await asyncio.to_thread(rts_display.window_region, display, WINDOW_NAME)
            if region is not None and region == seen:
                break
            seen = region
            await asyncio.sleep(0.5)
        if region is None:
            raise RuntimeError(f"no {WINDOW_NAME!r} window on {display}")
        self.grab = rts_display.Grab(display, region)
        await asyncio.to_thread(self.grab.start)

    async def _drop_capture(self) -> None:
        if self.grab is not None:
            self.grab.stop()
            self.grab = None


class Camera:
    """The director's spots, eased over PAN_SECONDS of video as the live game eases them; a far one is a cut."""

    def __init__(self, fps: int):
        self.frames = max(1, round(PAN_SECONDS * fps))
        self.at: tuple[float, float] | None = None
        self.path: list[tuple[float, float]] = []

    def go(self, spot: tuple[float, float]) -> None:
        start = self.at
        if start is None or math.dist(start, spot) > PAN_MAX:
            self.path = [spot]
            return
        self.path = []
        for i in range(1, self.frames + 1):
            k = i / self.frames
            k = k * k * (3 - 2 * k)
            self.path.append((start[0] + (spot[0] - start[0]) * k, start[1] + (spot[1] - start[1]) * k))

    def next(self) -> tuple[float, float] | None:
        if not self.path:
            return None
        self.at = self.path.pop(0)
        return self.at


async def render(spec: dict) -> dict:
    env = ReplayEnv()
    given = {name: os.environ.get("WC3_" + name.replace(".", "_").upper()) for name in LICENSE_FILES}   # WC3_ROC_W3K
    if files := {name: base64.b64decode(value) for name, value in given.items() if value}:
        attach_license(files, env.license_store, env.game_dir)
    scenario = check_scenario({**DEFAULT_SCENARIO, "map": spec["map"], "players": spec["players"], "seed": spec["seed"],
                               "client_view": True, "replay": spec["replay"]})
    fps, speed = int(spec["fps"]), float(spec["speed"])
    frame_ms = max(25, round(speed * 1000 / fps / 25) * 25)
    out = Path(spec["out"])
    encoder = None
    try:
        await env._new_game(scenario)
        env.director = frames.Director(spec.get("lead", env.lead))
        if spec.get("stage"):
            await env.stage(**spec["stage"])
        encoder = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "image2pipe", "-c:v", "mjpeg",
             "-framerate", str(fps), "-i", "-", "-vf", f"scale={WIDTH}:-2:flags=lanczos", "-c:v", "libx264",
             "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)],
            stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        notes = sorted(spec.get("notes") or [], key=lambda n: n["t"])
        camera, shown_at, start, n = Camera(fps), None, env._seconds(), 0
        while not env.done:
            env._observed(await env.bridge.call("step", actions={}, ms=frame_ms))
            env.feed.see(env.obs)
            if env.done:   # the replay has ended: the game takes no more debug ops, and its picture stands
                encoder.stdin.write(await asyncio.to_thread(env.grab.fresh))
                n += 1
                break
            now, t = n / fps, env._seconds()
            while notes and notes[0]["t"] <= t:
                await _overlay(env, wrapped(notes.pop(0)["text"]))
                shown_at = now
            if shown_at is not None and now - shown_at >= NOTE_SECONDS:
                await _overlay(env, " ")
                shown_at = None
            if (spot := env.director.choose(env._me(env.director.me), env.feed.moments, now)) is not None:
                camera.go(spot)
            if (at := camera.next()) is not None:
                await env.bridge.call("debug", op="camera", args={"x": round(at[0]), "y": round(at[1])})
            encoder.stdin.write(await asyncio.to_thread(env.grab.fresh))
            n += 1
        encoder.stdin.close()
        if encoder.wait() or not out.is_file():
            raise RuntimeError(f"ffmpeg failed: {encoder.stderr.read().decode(errors='replace')[-300:]}")
        summary = {"fps": fps, "frame_seconds": frame_ms / 1000, "start": start, "frames": n, "end": env._seconds(),
                   "lead": env.director.me, "result": env.result,
                   "scores": {str(slot): (o.get("score") or {}).get("total") for slot, o in env.obs.items()}}
        out.with_name(out.name + ".json").write_text(json.dumps(summary))
        return summary
    finally:
        if encoder is not None and encoder.poll() is None:
            encoder.kill()
        await env._drop_capture()
        if env.bridge is not None:
            await env.bridge.close()


async def _overlay(env: WC3Env, text: str) -> None:
    try:
        await env.bridge.call("debug", op="overlay", args={"panel": text, **OVERLAY, "seconds": 3600})
    except WorkerError as e:
        log.warning("overlay: %s", e.message)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stderr)
    print(json.dumps(asyncio.run(render(json.loads(Path(sys.argv[1]).read_text())))))


if __name__ == "__main__":
    main()
