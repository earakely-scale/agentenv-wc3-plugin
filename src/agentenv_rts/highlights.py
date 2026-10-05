"""A recorded match as a broadcast deliverable, cut with ffmpeg from the game's own video (display.py): its major
moments on the video's clock (each frame's "w"), chapters embedded in that video, and a highlight reel. Game-agnostic:
it reads timeline.py's events, the `major` ones and each `fight` with the `fight_end` that closes it."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .timeline import Timeline

CHAPTER_GAP = 20.0             # video seconds from one chapter's start to the next, at least
TITLE_CHARS = 60
FIGHT_PAD = (4.0, 3.0)         # a fight's clip: from this long before it starts to this long after it ends
FIGHT_CLIP = 25.0
MOMENT_PAD = (5.0, 4.0)
LONG_FIGHT = 15.0
FIRST = ("death", "tier", "expand")   # the kinds a reel keeps before any other (a major death: a hero or a building)
SHORTEST = 4.0                 # a clip the reel's length would cut shorter than this is left out
FADE = 0.3
ESCAPE = re.compile(r"([=;#\\\n])")
WORD = re.compile(r"[\w'-]+")


class HighlightsError(RuntimeError):
    pass


def moments(timeline: Timeline) -> list[dict]:
    """The major events in game order as `{"t", "w", "kind", "side", "text"}`, `w` None for a frame without video
    time. A fight also has `end` (the video time of the fight_end that closes it, None if none did) and `outcome`
    (that event's text); a fight_end closes the open fight whose text opens with the most of its words, as feeds name
    both by place (whole words, so "the north" is not "the north-east")."""
    out, fighting = [], []
    for f in timeline.frames:
        for e in f.get("events") or ():
            if not isinstance(e, dict):
                continue
            if e.get("kind") == "fight_end":
                if fighting:
                    words = WORD.findall(e.get("text") or "")
                    fight = max(fighting, key=lambda m: len(os.path.commonprefix([WORD.findall(m["text"]), words])))
                    fighting.remove(fight)
                    fight.update(end=f.get("w"), outcome=e.get("text"))
            elif e.get("major"):
                out.append({"t": f["t"], "w": f.get("w"), "kind": e.get("kind"), "side": e.get("side"),
                            "text": e.get("text") or ""})
                if e.get("kind") == "fight":
                    out[-1].update(end=None, outcome=None)
                    fighting.append(out[-1])
    return out


def title(text: str) -> str:
    return text if len(text) <= TITLE_CHARS else text[:TITLE_CHARS - 1].rsplit(" ", 1)[0] + "…"


def chapters(timeline: Timeline, duration: float) -> list[dict]:
    """`{"start", "end", "title"}` in video seconds: "Start" at 0, then one at each moment at least CHAPTER_GAP after
    the last chapter began, named by it."""
    marks = [(0.0, "Start")]
    for m in moments(timeline):
        if m["w"] is not None and m["w"] - marks[-1][0] >= CHAPTER_GAP and m["w"] < duration:
            marks.append((m["w"], title(m["text"])))
    ends = [start for start, _ in marks[1:]] + [duration]
    return [{"start": start, "end": end, "title": name} for (start, name), end in zip(marks, ends, strict=True)]


def ffmetadata(marks: list[dict]) -> str:
    lines = [";FFMETADATA1"]
    for c in marks:
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={round(c['start'] * 1000)}",
                  f"END={round(c['end'] * 1000)}", "title=" + ESCAPE.sub(r"\\\1", c["title"])]
    return "\n".join(lines) + "\n"


def chaptered(timeline: Timeline, video: Path, out: Path) -> list[dict]:
    """`video` copied to `out` with the timeline's chapters, without re-encoding; returns the chapters."""
    marks = chapters(timeline, duration(video))
    with tempfile.TemporaryDirectory() as tmp:
        meta = Path(tmp) / "chapters.txt"
        meta.write_text(ffmetadata(marks))
        _ffmpeg(["-i", str(video), "-f", "ffmetadata", "-i", str(meta), "-map", "0", "-map_metadata", "1",
                 "-map_chapters", "1", "-c", "copy", "-movflags", "+faststart", str(out)])
    return marks


def clips(timeline: Timeline, duration: float, max_seconds: float = 120) -> list[tuple[float, float]]:
    """The reel's clips in video seconds, in order: each fight from FIGHT_PAD before it to FIGHT_PAD after its end (at
    most FIGHT_CLIP) and each other moment within MOMENT_PAD, overlapping ones merged, then the most telling (FIRST's
    kinds, then long fights) kept until max_seconds."""
    spans = []
    for m in moments(timeline):
        if m["w"] is None or m["w"] >= duration:
            continue
        if m["kind"] == "fight":
            end = m["end"] if m["end"] is not None else duration
            start = max(0.0, m["w"] - FIGHT_PAD[0])
            spans.append([start, min(duration, end + FIGHT_PAD[1], start + FIGHT_CLIP),
                          len(FIRST) + (end - m["w"] < LONG_FIGHT)])
        else:
            rank = FIRST.index(m["kind"]) if m["kind"] in FIRST else len(FIRST) + 1
            spans.append([max(0.0, m["w"] - MOMENT_PAD[0]), min(duration, m["w"] + MOMENT_PAD[1]), rank])
    merged: list[list] = []
    for start, end, rank in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1:] = [max(merged[-1][1], end), min(merged[-1][2], rank)]
        else:
            merged.append([start, end, rank])
    kept, left = [], max_seconds
    for start, end, _ in sorted(merged, key=lambda s: (s[2], s[0] - s[1], s[0])):
        if (length := min(end - start, left)) >= SHORTEST:
            kept.append((start, start + length))
            left -= length
    return sorted(kept)


def reel(timeline: Timeline, video: Path, out: Path, max_seconds: float = 120) -> list[tuple[float, float]]:
    """The clips cut from `video` into one H.264 MP4 at `out`, each fading in and out; returns the clips."""
    cuts = clips(timeline, duration(video), max_seconds)
    if not cuts:
        raise HighlightsError("no major moment on the video's clock")
    graph = "".join(f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS,fade=t=in:st=0:d={FADE},"
                    f"fade=t=out:st={b - a - FADE:.3f}:d={FADE}[c{i}];" for i, (a, b) in enumerate(cuts))
    graph += "".join(f"[c{i}]" for i in range(len(cuts))) + f"concat=n={len(cuts)}:v=1:a=0[reel]"
    _ffmpeg(["-i", str(video), "-filter_complex", graph, "-map", "[reel]", "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "23", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)])
    return cuts


def duration(video: Path) -> float:
    return float(_run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)]))


def _ffmpeg(args: list[str]) -> None:
    _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args])


def _run(command: list[str]) -> str:
    if shutil.which(command[0]) is None:
        raise HighlightsError(f"{command[0]} is not installed")
    proc = subprocess.run(command, capture_output=True, text=True)
    if proc.returncode:
        raise HighlightsError(f"{command[0]} failed: {proc.stderr.strip()[-300:]}")
    return proc.stdout
