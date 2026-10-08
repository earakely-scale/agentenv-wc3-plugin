"""The streamer (agentenv_rts/streamer/stream.py) and `agent-env wc3 stream`, with neither Docker nor the network:
the ffmpeg commands it runs, the page it shows, what it keeps out of its output, when it ends the stream, and the
docker run the command builds, its secrets in the environment only."""

import asyncio
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent_env.artifact import FileArtifact
from agent_env.config import ConfigError
from agent_env.task_step.context import TaskStepContext
from agentenv_game.steps import CancelMatchTaskStep
from click.testing import CliRunner
from conftest import new_game
from test_steps import deployed, run_context

from agentenv_rts import broadcast
from agentenv_rts.steps import RTSBroadcastTaskStep, SaveRTSBroadcastTaskStep
from agentenv_rts.streamer import stream
from agentenv_wc3 import cli
from agentenv_wc3.server import WC3Env

STREAM_KEY, X_KEY, MODEL_KEY = "live_123_secret", "x_456_secret", "sk-model-key-456"


@pytest.fixture
def env_state():
    """A live view whose /live/state.json answers with `doc`, or fails while `doc` is None (the env is gone)."""
    doc = {"game_over": False, "result": "", "t": 12.0, "client": False}
    box = {"doc": doc}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/live/state.json" or box["doc"] is None:
                self.send_error(503)
                return
            body = json.dumps(box["doc"]).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/live", box
    server.shutdown()


def test_the_streamer_encodes_once_for_one_leg_several_legs_or_only_the_recording():
    one = stream.ffmpeg_command("1920x1080", 30, "4500k", ["rtmp://t/app/key"], None)
    assert one[-3:] == ["-f", "flv", "rtmp://t/app/key"] and "-tune" not in one
    assert one[one.index("-stats"):one.index("-stats") + 3] == ["-stats", "-stats_period", "60"]
    assert one[one.index("x11grab") - 1:one.index("x11grab") + 7] == [
        "-f", "x11grab", "-video_size", "1920x1080", "-framerate", "30", "-draw_mouse", "0"]
    assert one[one.index("pulse") - 1:one.index("pulse") + 3] == ["-f", "pulse", "-i", "broadcast.monitor"]
    assert one[one.index("-c:v"):one.index("-c:v") + 4] == ["-c:v", "libx264", "-preset", "veryfast"]
    assert one[one.index("-bufsize"):one.index("-bufsize") + 4] == ["-bufsize", "9000k", "-g", "60"]
    assert one[one.index("-c:a"):one.index("-c:a") + 4] == ["-c:a", "aac", "-b:a", "128k"]
    legs = stream.ffmpeg_command("1280x720", 30, "3000k", ["rtmp://t/app/a", "rtmps://x:443/x/b"],
                                 Path("/rec/stream.mkv"))
    assert legs[-5:] == ["-flags", "+global_header", "-f", "tee",
                         "[f=flv:onfail=ignore]rtmp://t/app/a|[f=flv:onfail=ignore]rtmps://x:443/x/b"
                         "|[f=matroska]/rec/stream.mkv"]
    assert stream.ffmpeg_command("1280x720", 30, "3000k", [], Path("/rec/s.mkv"))[-3:] == [
        "-f", "matroska", "/rec/s.mkv"]
    assert stream.remux_command(Path("/rec/s.mkv"))[-8:] == [
        "/rec/s.mkv", "-map", "0", "-c", "copy", "-movflags", "+faststart", "/rec/s.mp4"]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="the remux needs ffmpeg")
def test_the_recording_becomes_an_mp4_that_plays_before_it_has_loaded(tmp_path):
    mkv = tmp_path / "stream-20261005T120000Z.mkv"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "2", "-c:v", "libx264",
                    "-preset", "veryfast", "-pix_fmt", "yuv420p", "-g", "60", "-c:a", "aac", "-f", "matroska",
                    str(mkv)], check=True)
    mp4 = stream.remux(mkv)
    assert mp4 == mkv.with_suffix(".mp4") and not mkv.exists()
    data = mp4.read_bytes()
    assert 0 < data.index(b"moov") < data.index(b"mdat")
    assert stream.remux(tmp_path / "never-written.mkv") is None


def test_the_streamer_page_asks_for_the_broadcast_layout_the_casters_and_the_title():
    assert stream.page_url("http://h:1/live") == "http://h:1/live?stream"
    assert stream.page_url("http://h:1/live", cast_port=41000, title="Orcs & Humans") == (
        "http://h:1/live?stream&cast=http://127.0.0.1:41000&title=Orcs%20%26%20Humans")
    assert stream.page_url("http://h:1/live?focus=0", title="Echo Isles") == (
        "http://h:1/live?focus=0&stream&title=Echo%20Isles")


def test_the_streamer_keeps_the_stream_keys_and_the_cast_key_out_of_its_output():
    secrets = stream.secrets_of(["rtmp://live.twitch.tv/app/live_9?bandwidthtest=true",
                                 "rtmps://or.pscp.tv:443/x/x_7"], "sk-cast-1")
    assert stream.redacted("rtmp://live.twitch.tv/app/live_9?bandwidthtest=true: broken pipe", secrets) == (
        "rtmp://live.twitch.tv/app/<stream key>?bandwidthtest=true: broken pipe")
    assert stream.redacted("[tee] Slave rtmps://or.pscp.tv:443/x/x_7 failed", secrets) == (
        "[tee] Slave rtmps://or.pscp.tv:443/x/<stream key> failed")
    assert stream.redacted("caster: HTTP 401 for key sk-cast-1", secrets) == "caster: HTTP 401 for key <cast key>"
    assert stream.redacted("frame=  900 fps= 30", stream.secrets_of([], "")) == "frame=  900 fps= 30"


def test_the_streamer_relays_each_line_as_it_comes_progress_lines_too_without_the_keys(capsys):
    chunks = [b"[flv @ 0x1] rtmp://t/app/liv", b"e_9: Broken pipe\nframe=  900 fps= 30 sp", b"eed=   1x    \r",
              b"\r\ncaster: HTTP 401 for sk-cast-1", b""]
    printed = []

    class Pipe:
        def read1(self, size):
            printed.append(capsys.readouterr().out)
            return chunks.pop(0)

    stream.relay(Pipe(), stream.secrets_of(["rtmp://t/app/live_9"], "sk-cast-1"))
    printed.append(capsys.readouterr().out)
    assert printed == ["", "", "[flv @ 0x1] rtmp://t/app/<stream key>: Broken pipe\n",
                       "frame=  900 fps= 30 speed=   1x\n", "", "caster: HTTP 401 for <cast key>\n"]


def test_the_streamer_ends_linger_seconds_after_game_over(env_state, capsys):
    url, box = env_state
    assert stream.state(url) == box["doc"]
    clock = [1000.0]

    def sleep(seconds):
        clock[0] += seconds
        if clock[0] == 1020:
            box["doc"] = {"game_over": True, "result": "victory", "t": 754.0, "client": False}

    assert stream.follow(url, 30, lambda: True, now=lambda: clock[0], sleep=sleep)
    assert clock[0] == 1050
    out = capsys.readouterr().out
    assert "GAME OVER (victory) at 12:34; the final screen stays on for 30 s" in out
    assert "The game is over; ending the stream" in out


def test_the_streamer_ends_a_minute_after_the_env_goes_and_with_its_encoder(env_state, capsys):
    url, box = env_state
    clock = [1000.0]

    def sleep(seconds):
        clock[0] += seconds
        box["doc"] = None if clock[0] in (1005, 1010) or clock[0] >= 1100 else {"game_over": False}

    assert not stream.follow(url, 30, lambda: True, now=lambda: clock[0], sleep=sleep)
    assert clock[0] == 1160   # gone at 1005 and back at 1015: the minute counts from 1100
    assert "The env has not answered for 60 s; ending the stream" in capsys.readouterr().out
    assert stream.state(url) is None
    assert not stream.follow(url, 30, lambda: False, sleep=lambda s: pytest.fail("polled after the encoder ended"))
    assert not stream.stop_at(None, None, 60, 1000)
    assert not stream.stop_at(950, None, 60, 1000) and stream.stop_at(940, None, 60, 1000)
    assert not stream.stop_at(None, 945, 60, 1000) and stream.stop_at(None, 940, 60, 1000)


class Docker:
    """`subprocess` as cli.py uses it, with no Docker: `docker ps` lists `ps`, `docker image inspect` finds the images
    built so far, and every build and run is recorded with the environment it got."""

    TimeoutExpired = subprocess.TimeoutExpired

    def __init__(self, ps: str = "", code: int = 0):
        self.ps, self.code, self.images, self.builds, self.runs = ps, code, set(), [], []

    def run(self, args, **kwargs):
        if args[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(args, 0 if args[3] in self.images else 1)
        if args[:2] == ["docker", "ps"]:
            return subprocess.CompletedProcess(args, 0, stdout=self.ps)
        assert args[:3] == ["docker", "build", "-t"], args
        self.builds.append(args)
        self.images.add(args[3])
        return subprocess.CompletedProcess(args, 0)

    def Popen(self, args, env=None):  # noqa: N802
        self.runs.append((args, env))
        return SimpleNamespace(wait=lambda: self.code)


@pytest.fixture
def docker(monkeypatch):
    fake = Docker()
    monkeypatch.setattr(cli, "subprocess", fake)
    monkeypatch.setattr(broadcast, "subprocess", fake)
    monkeypatch.setattr(cli.sys, "platform", "linux")
    return fake


@pytest.fixture
def playing(monkeypatch):
    """Every live view answers, with a game under way."""
    monkeypatch.setattr(broadcast, "state", lambda url: {"game_over": False, "result": "", "t": 0.0, "client": False})


def configure(monkeypatch, *, model: bool = True, **secrets):
    """agent-env's config as the stream command reads it: the secret store and, with `model`, the model endpoint."""
    def endpoint(value):
        def get():
            if not model:
                raise ConfigError("No model endpoint configured: set [model] base_url in .agentenv/config.toml")
            return value
        return get

    config = SimpleNamespace(get_secret_store=lambda: SimpleNamespace(get=secrets.get),
                             get_litellm_base_url=endpoint("http://localhost:4000"),
                             get_litellm_api_key=endpoint(MODEL_KEY))
    monkeypatch.setattr(broadcast, "get_config", lambda: config)


def invoke(*args: str):
    return CliRunner().invoke(cli.wc3, ["stream", *args])


def test_stream_sends_the_newest_game_still_playing_to_twitch_with_the_casters(docker, playing, monkeypatch):
    configure(monkeypatch, TWITCH_STREAM_KEY=STREAM_KEY)
    docker.ps = ("mcp-server-wc3\t127.0.0.1:42000->18765/tcp\tagent-local-over\n"
                 "postgres:16\t127.0.0.1:5432->5432/tcp\tdb\n"
                 "mcp-server-wc3\t127.0.0.1:41000->18765/tcp\tagent-local-playing\n")
    monkeypatch.setattr(broadcast, "state", lambda url: {"game_over": "42000" in url, "t": 300.0})
    result = invoke("--linger", "30")
    assert result.exit_code == 0, result.output
    (build,), [(args, env)] = docker.builds, docker.runs
    image = build[3]
    assert re.fullmatch(r"rts-streamer:[0-9a-f]{12}", image) and build == ["docker", "build", "-t", image,
                                                                         str(broadcast.STREAMER)]
    assert args == ["docker", "run", "--rm", "--shm-size", "1g", "--network", "host", "-e", "STREAM_URL",
                    "-e", "CAST_BASE_URL", "-e", "CAST_API_KEY", image, "--url", "http://127.0.0.1:41000/live",
                    "--size", "1920x1080", "--fps", "30", "--bitrate", "4500k", "--linger", "30",
                    "--cast-config", '{"model": "anthropic/claude-haiku-4-5"}']
    assert env["STREAM_URL"] == f"rtmp://live.twitch.tv/app/{STREAM_KEY}" and env["PATH"] == os.environ["PATH"]
    assert env["CAST_BASE_URL"] == "http://localhost:4000" and env["CAST_API_KEY"] == MODEL_KEY
    assert "to rtmp://live.twitch.tv/app/<stream key>, with the casters" in result.output
    assert STREAM_KEY not in result.output + " ".join(args) and MODEL_KEY not in result.output + " ".join(args)

    result = invoke("--test", "--no-cast", "--size", "1280x720", "--bitrate", "3000k")
    assert result.exit_code == 0, result.output
    assert len(docker.builds) == 1   # the image is there now
    args, env = docker.runs[-1]
    assert env["STREAM_URL"] == f"rtmp://live.twitch.tv/app/{STREAM_KEY}?bandwidthtest=true"
    assert "CAST_API_KEY" not in env and "--cast-config" not in args and "-e" not in args[args.index(image):]
    assert args[args.index("--size"):] == ["--size", "1280x720", "--fps", "30", "--bitrate", "3000k",
                                           "--linger", "60"]
    assert "as a bandwidth test" in result.output


def test_stream_sends_one_stream_to_twitch_and_x_at_once(docker, playing, monkeypatch):
    configure(monkeypatch, TWITCH_STREAM_KEY=STREAM_KEY, X_STREAM_SERVER="rtmps://or.pscp.tv:443/x/",
              X_STREAM_KEY=X_KEY)
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    both = ["--url", "http://127.0.0.1:41589/live", "--no-cast", "--to", "twitch", "--to", "x"]
    result = invoke(*both, "--title", "Orcs vs Humans")
    assert result.exit_code == 0, result.output
    args, env = docker.runs[-1]
    assert env["STREAM_URL"] == f"rtmp://live.twitch.tv/app/{STREAM_KEY}\nrtmps://or.pscp.tv:443/x/{X_KEY}"
    assert "--network" not in args and args[args.index("--url") + 1] == "http://host.docker.internal:41589/live"
    assert args[-2:] == ["--title", "Orcs vs Humans"]
    assert ("to rtmp://live.twitch.tv/app/<stream key> and to rtmps://or.pscp.tv:443/x/<stream key> (press Go Live "
            "in X's Live Studio once it starts)") in result.output
    assert "_secret" not in result.output + " ".join(args)

    configure(monkeypatch, X_STREAM_SERVER="rtmps://ca.pscp.tv:443/x")
    monkeypatch.setenv("MY_X_KEY", "x_789_secret")   # not in the secret store: an environment variable
    result = invoke("--url", "http://127.0.0.1:41589/live", "--no-cast", "--to", "x", "--x-key-secret", "MY_X_KEY")
    assert result.exit_code == 0, result.output
    assert docker.runs[-1][1]["STREAM_URL"] == "rtmps://ca.pscp.tv:443/x/x_789_secret"

    configure(monkeypatch, TWITCH_STREAM_KEY=STREAM_KEY, X_STREAM_SERVER="rtmps://or.pscp.tv:443/x")
    result = invoke(*both)
    assert result.exit_code == 1
    assert "store its server URL and stream key as the secrets X_STREAM_SERVER and X_STREAM_KEY" in result.output
    assert len(docker.runs) == 2


def test_stream_records_offline_as_you_with_the_casters_on_this_machines_endpoint(docker, playing, monkeypatch,
                                                                                    tmp_path):
    configure(monkeypatch)   # no stream key: offline needs none
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    rec = tmp_path / "rec" / "show"
    result = invoke("--url", "http://localhost:41589/live", "--offline", "--record", str(rec), "--caster-model",
                    "openai/gpt-5.6-luna")
    assert result.exit_code == 0, result.output
    assert rec.is_dir() and f"Streaming http://localhost:41589/live into {rec}, with the casters" in result.output
    args, env = docker.runs[-1]
    image = docker.builds[0][3]
    assert args[:args.index(image)] == [
        "docker", "run", "--rm", "--shm-size", "1g", "-v", f"{rec.resolve()}:/rec",
        "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
        "-e", "STREAM_URL", "-e", "CAST_BASE_URL", "-e", "CAST_API_KEY"]
    assert args[args.index("--url") + 1] == "http://host.docker.internal:41589/live"
    assert args[-4:] == ["--cast-config", '{"model": "openai/gpt-5.6-luna"}', "--record", "/rec"]
    assert env["STREAM_URL"] == "" and env["CAST_BASE_URL"] == "http://host.docker.internal:4000"
    assert MODEL_KEY not in result.output + " ".join(args)


def test_stream_explains_what_it_needs_before_it_runs_anything(docker, playing, monkeypatch, tmp_path):
    configure(monkeypatch, model=False)
    result = invoke("--offline")
    assert result.exit_code == 2 and "--offline only records: add --record DIR" in result.output
    result = invoke("--no-cast")
    assert result.exit_code == 1
    assert "store your Twitch stream key as the secret TWITCH_STREAM_KEY in agent-env's secret store" in result.output
    result = invoke("--offline", "--record", str(tmp_path))
    assert result.exit_code == 1
    assert "the casters need agent-env's model endpoint: No model endpoint configured" in result.output
    assert result.output.rstrip().endswith("or broadcast without them (--no-cast, or cast: false)")
    assert docker.builds == docker.runs == []

    docker.code = 1
    result = invoke("--url", "http://127.0.0.1:41589/live", "--offline", "--record", str(tmp_path), "--no-cast")
    assert result.exit_code == 1 and "the stream ended with an error (1)" in result.output


def test_stream_waits_until_the_env_has_a_game_under_way(docker, monkeypatch, env_state, tmp_path):
    url, box = env_state
    assert broadcast.ready(url)
    box["doc"] = {"game_over": True, "result": "defeat", "t": 300.0, "client": False}
    assert not broadcast.ready(url)
    box["doc"] = None
    assert broadcast.state(url) is None and not broadcast.ready(url)
    box["doc"] = {"game_over": False, "result": "", "t": None, "client": False}   # deployed, its match not begun
    assert not broadcast.ready(url)

    waits = []

    def sleep(seconds):
        waits.append(seconds)
        if len(waits) == 2:
            box["doc"] = {"game_over": False, "result": "", "t": 0.0, "client": False}

    monkeypatch.setattr(cli, "time", SimpleNamespace(sleep=sleep))
    configure(monkeypatch)
    result = invoke("--url", url, "--offline", "--record", str(tmp_path), "--no-cast")
    assert result.exit_code == 0, result.output
    assert f"Waiting for the game at {url} to start" in result.output and waits == [5, 5]
    args, _ = docker.runs[-1]
    assert args[args.index("--url") + 1] == url


FAKE_DOCKER = """#!{python}
import json, os, pathlib, signal, sys, time
args = sys.argv[1:]
here = pathlib.Path(os.environ["FAKE_DOCKER_DIR"])
with (here / "calls.jsonl").open("a") as f:
    f.write(json.dumps(args) + "\\n")


def alive(name):
    path = here / (name + ".pid")
    try:
        os.kill(int(path.read_text()), 0)
        return True
    except (OSError, ValueError):
        return False


if args[0] == "stop":
    if alive(args[-1]):
        os.kill(int((here / (args[-1] + ".pid")).read_text()), signal.SIGTERM)
    while alive(args[-1]):
        time.sleep(0.05)
elif args[0] == "inspect":
    if not alive(args[-1]):
        sys.exit(1)   # gone: --rm removed it
    print("true")
elif args[0] == "run":
    if "-d" in args and os.fork():
        time.sleep(0.3)   # detached: docker returns once the container runs
        sys.exit(0)
    (here / (args[args.index("--name") + 1] + ".pid")).write_text(str(os.getpid()))
    rec = next(a.split(":")[0] for a in args if a.endswith(":/rec"))

    pathlib.Path(rec, "stream-test.mkv").write_bytes(b"live")   # the streamer records as it goes

    def finish(*_):
        pathlib.Path(rec, "stream-test.mp4").write_bytes(b"\\0\\0\\0\\x18ftypisom" + b"x" * 100)
        pathlib.Path(rec, "stream-test.mkv").unlink()
        os._exit(0)

    signal.signal(signal.SIGTERM, finish)
    if os.environ.get("FAKE_STREAM") == "end":
        time.sleep(1)
        finish()
    if os.environ.get("FAKE_STREAM") == "die":   # killed: its live recording is all there is
        time.sleep(1)
        os._exit(137)
    while True:
        time.sleep(0.1)
"""


@pytest.fixture
def fake_docker(tmp_path, monkeypatch):
    """A `docker` on PATH that records its calls; a detached `run` records a broadcast and exits after a second
    (FAKE_STREAM=end), dies (die), or streams until `docker stop` (SIGTERM), as the streamer does; `inspect` says
    whether it still runs."""
    directory = tmp_path / "docker-bin"
    directory.mkdir()
    (directory / "docker").write_text(FAKE_DOCKER.format(python=sys.executable))
    (directory / "docker").chmod(0o755)
    monkeypatch.setenv("PATH", f"{directory}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DOCKER_DIR", str(directory))
    monkeypatch.setattr(SaveRTSBroadcastTaskStep, "POLL_SECONDS", 0.2)
    calls = directory / "calls.jsonl"
    return lambda: [json.loads(line) for line in calls.read_text().splitlines()] if calls.is_file() else []


async def on_air(record, **options) -> TaskStepContext:
    """A run's rts_broadcast step, once its match exists."""
    step = RTSBroadcastTaskStep(id="broadcast", version=None, env_id="wc3", cast=False, **options)
    return await step.execute(run_context(record))


async def saved(context: TaskStepContext, **options) -> TaskStepContext:
    return await SaveRTSBroadcastTaskStep(id="save", version=None, env_id="wc3", **options).execute(context)


@pytest.mark.anyio
async def test_a_broadcast_is_live_before_the_match_starts_and_its_video_is_kept_once_it_ends(
        env_vars, local_stores, fake_docker, monkeypatch):
    monkeypatch.setenv("FAKE_STREAM", "end")
    env = WC3Env()
    async with deployed(env) as record:
        await new_game(env)
        context = await on_air(record)
        started = context.metadata["broadcasts"]["broadcast"]
        assert started["container"].startswith("rts-broadcast-") and Path(started["recording"]).is_dir()
        assert env.match.status == "not_started"   # the step returned, live, before anyone moved
        context = await saved(context)
    [video] = context.metadata["broadcasts"]["broadcast"]["videos"]
    assert video["name"] == "stream-test.mp4" and FileArtifact.get(video["artifact_id"], 1).load()[4:8] == b"ftyp"
    assert not Path(started["recording"]).exists()
    run = next(c for c in fake_docker() if c[0] == "run")
    live = record.environment_url + "/live"
    assert run[:2] == ["run", "-d"] and run[-2:] == ["--record", "/rec"]
    assert run[run.index("--url") + 1] == (broadcast.from_container(live) if sys.platform == "darwin" else live)
    assert "STREAM_URL" in run and "--cast-config" not in run


@pytest.mark.anyio
async def test_a_broadcast_ends_when_the_match_stands_still_or_the_save_is_cancelled(
        env_vars, local_stores, fake_docker, monkeypatch):
    monkeypatch.setenv("FAKE_STREAM", "hang")
    env = WC3Env()
    async with deployed(env) as record:
        await new_game(env)
        context = await asyncio.wait_for(saved(await on_air(record), stall_seconds=1), 20)   # nobody plays: it stalls
        assert [c[0] for c in fake_docker() if c[0] != "inspect"][-2:] == ["run", "stop"]
        assert context.metadata["broadcasts"]["broadcast"]["videos"][0]["name"] == "stream-test.mp4"
        running = asyncio.create_task(saved(await on_air(record)))
        await asyncio.sleep(1)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert [c[0] for c in fake_docker() if c[0] != "inspect"][-2:] == ["run", "stop"]   # it did not outlive the run


@pytest.mark.anyio
async def test_a_broadcast_without_its_key_or_its_match_or_of_a_match_over_starts_nothing(
        env_vars, local_stores, fake_docker, monkeypatch):
    monkeypatch.delenv("TWITCH_STREAM_KEY", raising=False)
    env = WC3Env()
    async with deployed(env) as record:
        noted = await on_air(record)   # the run goes on, the error noted
        assert "has no match: put the step after close_lobby" in noted.metadata["broadcast_errors"]["broadcast"]
        await new_game(env)
        noted = await on_air(record, to=["twitch"])
        assert "TWITCH_STREAM_KEY" in noted.metadata["broadcast_errors"]["broadcast"]
        with pytest.raises(broadcast.BroadcastError, match="TWITCH_STREAM_KEY"):
            await on_air(record, to=["twitch"], fail_task_on_error=True)
        await CancelMatchTaskStep(id="cancel", version=None, env_id="wc3").execute(run_context(record))
        context = await saved(await on_air(record))
    assert context.metadata["broadcasts"]["broadcast"]["videos"] == [] and "broadcast_errors" not in context.metadata
    assert not any(c[0] == "run" for c in fake_docker())
    with pytest.raises(ValueError, match="goes nowhere"):
        RTSBroadcastTaskStep(id="b", version=None, env_id="wc3", record=False)


@pytest.mark.anyio
async def test_a_streamer_that_dies_leaves_what_it_recorded(env_vars, local_stores, fake_docker, monkeypatch):
    monkeypatch.setenv("FAKE_STREAM", "die")
    env = WC3Env()
    async with deployed(env) as record:
        await new_game(env)
        context = await saved(await on_air(record))
    [video] = context.metadata["broadcasts"]["broadcast"]["videos"]
    assert video["name"] == "stream-test.mkv" and "broadcast_errors" not in context.metadata
