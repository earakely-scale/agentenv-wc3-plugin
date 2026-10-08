"""Broadcast an RTS env's live view (`/live?stream`): the streamer image (agentenv_rts/streamer) shows it in a headless
browser on a virtual display, and ffmpeg encodes it once, to every RTMP target and a recording, with two AI casters
voicing the game. `agent-env wc3 stream` and the `rts_broadcast` step both run it through `Broadcast`.

Stream keys come from agent-env's secret store ([stores.secret]), else the environment; they reach the container in
its environment, never on a command line."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from agent_env.config import ConfigError, get_config

STREAMER = Path(__file__).with_name("streamer")
IMAGE = "rts-streamer"
TWITCH = "rtmp://live.twitch.tv/app"
STREAM_KEY, X_SERVER, X_STREAM_KEY = "TWITCH_STREAM_KEY", "X_STREAM_SERVER", "X_STREAM_KEY"
CASTER_MODEL = "anthropic/claude-haiku-4-5"
DESTINATIONS = ("twitch", "x")


class BroadcastError(RuntimeError):
    pass


def secret(name: str) -> str | None:
    """A secret from agent-env's secret store, else the environment."""
    return get_config().get_secret_store().get(name) or os.environ.get(name)


@dataclass
class Broadcast:
    """Where a broadcast goes (`destinations`: twitch and x; none only records), how it looks, and whether the
    casters call it."""

    destinations: tuple[str, ...] = ("twitch",)
    server: str = TWITCH
    key_secret: str = STREAM_KEY
    x_server_secret: str = X_SERVER
    x_key_secret: str = X_STREAM_KEY
    size: str = "1920x1080"
    fps: int = 30
    bitrate: str = "4500k"
    linger: int = 60
    cast: bool = True
    caster_model: str = CASTER_MODEL
    title: str | None = None
    bandwidth_test: bool = False

    def __post_init__(self):
        if unknown := sorted(set(self.destinations) - set(DESTINATIONS)):
            raise BroadcastError(f"a broadcast goes to {', '.join(DESTINATIONS)}, not {unknown}")

    def targets(self) -> tuple[list[str], list[str]]:
        """The RTMP URLs, with their keys, and how to name each without its key."""
        urls, names = [], []
        test = " as a bandwidth test (not live; see Twitch Inspector)" if self.bandwidth_test else ""
        for name in dict.fromkeys(self.destinations):
            if name == "twitch":
                key = secret(self.key_secret)
                if not key:
                    raise BroadcastError(
                        f"no stream key: store your Twitch stream key as the secret {self.key_secret} in agent-env's "
                        f"secret store, or export {self.key_secret}; or only record")
                urls.append(f"{self.server.rstrip('/')}/{key}" + ("?bandwidthtest=true" if self.bandwidth_test else ""))
                names.append(f"to {self.server.rstrip('/')}/<stream key>{test}")
            else:
                server, key = secret(self.x_server_secret), secret(self.x_key_secret)
                if not server or not key:
                    raise BroadcastError(
                        f"no X stream: create a source in X's Live Studio and store its server URL and stream key as "
                        f"the secrets {self.x_server_secret} and {self.x_key_secret}, as for the Twitch key")
                urls.append(f"{server.rstrip('/')}/{key}")
                names.append(f"to {server.rstrip('/')}/<stream key> (press Go Live in X's Live Studio once it starts)")
        return urls, names

    def environment(self) -> dict[str, str]:
        """The container's environment: the targets, and the casters' model endpoint."""
        env = {"STREAM_URL": "\n".join(self.targets()[0])}
        if self.cast:
            config = get_config()
            try:
                env["CAST_BASE_URL"], env["CAST_API_KEY"] = config.get_litellm_base_url(), config.get_litellm_api_key()
            except ConfigError as e:
                raise BroadcastError(f"the casters need agent-env's model endpoint: {e}; or broadcast without "
                                     "them (--no-cast, or cast: false)") from e
            if sys.platform == "darwin":
                env["CAST_BASE_URL"] = from_container(env["CAST_BASE_URL"])
        return env

    def command(self, url: str, image: str, record_dir: Path | None, name: str | None = None,
                detach: bool = False) -> list[str]:
        """The `docker run` of the streamer for the live view at `url`, detached or not; the environment's names are
        passed through, so their values (the keys) stay off the command line."""
        network, page = ([], from_container(url)) if sys.platform == "darwin" else (["--network", "host"], url)
        mount = ([] if record_dir is None else
                 ["-v", f"{record_dir.resolve()}:/rec", "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp"])
        names = ["STREAM_URL", *(["CAST_BASE_URL", "CAST_API_KEY"] if self.cast else [])]
        return ["docker", "run", *(["-d"] if detach else []), "--rm", *(["--name", name] if name else []),
                "--shm-size", "1g", *network, *mount,
                *[a for n in names for a in ("-e", n)], image, "--url", page, "--size", self.size,
                "--fps", str(self.fps), "--bitrate", self.bitrate, "--linger", str(self.linger),
                *(["--cast-config", json.dumps({"model": self.caster_model})] if self.cast else []),
                *(["--title", self.title] if self.title else []), *(["--record", "/rec"] if record_dir else [])]


def image() -> str:
    """The streamer image's tag: a digest of agentenv_rts/streamer's files, so a changed streamer builds a new image
    instead of running an older one."""
    digest = hashlib.sha256()
    for path in sorted(p for p in STREAMER.iterdir() if p.is_file()):
        digest.update(path.name.encode() + b"\0" + path.read_bytes())
    return f"{IMAGE}:{digest.hexdigest()[:12]}"


def image_exists(tag: str) -> bool:
    try:
        return subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode == 0
    except OSError:
        return False


def build(tag: str) -> None:
    """Builds the streamer image unless it is there (about 1.5 GB, a few minutes the first time)."""
    if not image_exists(tag) and subprocess.run(["docker", "build", "-t", tag, str(STREAMER)]).returncode:
        raise BroadcastError("docker build of the streamer failed")


def state(url: str) -> dict | None:
    """Where the game behind a live view stands (/live/state.json); None while the env doesn't answer."""
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/state.json", timeout=10) as r:
            return json.load(r)
    except (OSError, ValueError):
        return None


def ready(url: str) -> bool:
    """Whether the env behind a live view has a game under way: a deployed env has none until its match starts one
    (until then the casters would have no players to talk about), and a finished one keeps serving its end."""
    s = state(url) or {}
    return s.get("t") is not None and not s.get("game_over")


def from_container(url: str) -> str:
    """`url` as a Docker Desktop container reaches it: this machine's loopback is host.docker.internal there."""
    parts = urllib.parse.urlsplit(url)
    if parts.hostname not in ("127.0.0.1", "localhost"):
        return url
    return parts._replace(netloc=parts.netloc.replace(parts.hostname, "host.docker.internal", 1)).geturl()
