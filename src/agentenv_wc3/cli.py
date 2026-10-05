"""`agent-env wc3`: check what a Warcraft III env needs on this machine, build and register the env from your
wc3env worker image (or on wc3env's fake game, on any machine), build and register the wc3-macro-micro agent, serve
the env locally against the fake game, and watch a running game live."""

from __future__ import annotations

import io
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

import click

from . import steps

ENVIRONMENT_NAME = "wc3"
BASE_IMAGE = "wc3-worker:local"
STANDIN_IMAGE = "wc3-worker:standin"
WC3ENV = "https://github.com/pwang724/wc3env"
WC3ENV_COMMIT = "eb660aa558fb6e5c639a1ff404082f7dc0ee483e"   # the one agents/wc3-player/Dockerfile pins
AGENT_ID = "wc3-macro-micro"
ENV_PORT = re.compile(r":(\d+)->18765/tcp")


def _checkout(source: Path | None) -> Path:
    """The checkout of this plugin to build from: `source`, else the one an editable install runs from, else the
    cwd."""
    for root in [source] if source else [Path(__file__).resolve().parents[2], Path.cwd()]:
        if (root / "Dockerfile").is_file() and (root / "src/agentenv_wc3").is_dir():
            return root
    raise click.UsageError("no checkout of agentenv-wc3-plugin found (a Dockerfile and src/agentenv_wc3); pass "
                           "--source")


def _docker_platform() -> str:
    """The Docker host's own platform (linux/arm64 on Apple Silicon): the fake game and the agent need no emulation."""
    try:
        out = subprocess.run(["docker", "version", "--format", "{{.Server.Os}}/{{.Server.Arch}}"], capture_output=True,
                             text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise click.ClickException(f"docker is not available: {e}") from e
    if out.returncode:
        raise click.ClickException(f"docker is not running: {out.stderr.strip()}")
    return out.stdout.strip()


def _wc3env(given: Path | None, into: Path) -> Path:
    """A wc3env checkout: `given`, else wc3env at the pinned commit, downloaded into `into`."""
    if given is not None:
        if not (given / "src/wc3env").is_dir():
            raise click.UsageError(f"{given} is not a wc3env checkout (no src/wc3env)")
        return given
    url = f"https://codeload.github.com/pwang724/wc3env/tar.gz/{WC3ENV_COMMIT}"
    click.echo(f"Downloading wc3env {WC3ENV_COMMIT[:7]}")
    with urllib.request.urlopen(url, timeout=300) as r:
        tarfile.open(fileobj=io.BytesIO(r.read())).extractall(into, filter="data")
    return into / f"wc3env-{WC3ENV_COMMIT}"


def _build_standin(root: Path, wc3env: Path, build_platform: str) -> None:
    """wc3-worker:standin, wc3env's worker image layout on its fake game (scripts/standin.sh)."""
    with tempfile.TemporaryDirectory() as tmp:
        context = Path(tmp)
        shutil.copy(root / "scripts/standin/Dockerfile", context)
        shutil.copy(wc3env / "docker/with-display.sh", context)
        shutil.copytree(wc3env / "src/wc3env", context / "wc3env")
        click.echo(f"Building {STANDIN_IMAGE} for {build_platform} (wc3env's fake game, no Wine, no Warcraft III)")
        build = ["docker", "build", "--platform", build_platform, "-t", STANDIN_IMAGE, str(context)]
        if subprocess.run(build).returncode:
            raise click.ClickException("docker build of the stand-in failed")


def _register_agent(root: Path, build_platform: str) -> None:
    """Build agents/wc3-player (wc3env's wc3agent behind A2A) and register it as wc3-macro-micro."""
    from agent_env.a2a_agent import A2AAgent
    from agent_env.artifact import DockerImageArtifact

    dockerfile, image = root / "agents/wc3-player/Dockerfile", f"a2a-agent-{AGENT_ID}"
    click.echo(f"Building {image} for {build_platform}")
    build = ["docker", "build", "--platform", build_platform, "-t", image, "-f", str(dockerfile), str(root)]
    if subprocess.run(build).returncode:
        raise click.ClickException("docker build of the wc3-macro-micro agent failed")
    artifact = DockerImageArtifact.put(id=image, image_name=image, description="Warcraft III wc3agent (macro + micro)",
                                       build_context_path=str(root), dockerfile_path=str(dockerfile))
    agent = A2AAgent.put(id=AGENT_ID, docker_image_artifact=artifact,
                         metadata={"default_model": "anthropic/claude-sonnet-5-5"})
    click.echo(f"Registered A2A agent {agent.id!r} version {agent.version} (wc3agent: macro + micro)")


def _image_exists(image: str) -> bool:
    try:
        return subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0
    except OSError:
        return False


@click.group()
def wc3():
    """Warcraft III (wc3env): check this machine, build and register the env, serve it against the fake game."""


@wc3.command()
@click.option("--base", default=BASE_IMAGE, show_default=True, help="Your wc3env worker image.")
@click.option("--license-dir", type=click.Path(file_okay=False, path_type=Path),
              help="Where roc.w3k and tft.w3k are. Default: [plugins.agentenv-wc3] license_dir, $WC3_LICENSE_DIR "
                   "or ~/.wc3-license.")
def check(base: str, license_dir: Path | None):
    """What a Warcraft III env needs here: an x86-64 Docker host, your wc3env worker image and your activation
    files."""
    ok = True
    arch = platform.machine().lower()
    if arch not in ("x86_64", "amd64"):
        ok = False
        click.echo(f"✗ this machine is {arch}: the game runs under Wine on x86-64 Linux only")
    else:
        click.echo(f"✓ x86-64 ({sys.platform})")
    if _image_exists(base):
        click.echo(f"✓ the wc3env worker image {base}")
    else:
        ok = False
        click.echo(f"✗ no image {base}: build it from your own installation as {WC3ENV}/blob/main/docker/README.md "
                   "shows (docker build --platform linux/amd64 --target environment -t wc3-worker:local ...)")
    directory = steps.license_dir(str(license_dir) if license_dir else None)
    missing = [n for n in steps.LICENSE_FILES if not (directory / n).is_file()]
    if missing:
        ok = False
        click.echo(f"✗ no {', '.join(missing)} in {directory}: copy them from your Warcraft III Legacy installation")
    else:
        click.echo(f"✓ activation files in {directory}")
    if not ok:
        raise SystemExit(1)


@wc3.command()
@click.option("--id", "env_id", default=ENVIRONMENT_NAME, show_default=True,
              help="Env id to register. The bundle's tasks deploy 'wc3'.")
@click.option("--base", default=BASE_IMAGE, show_default=True,
              help="Your wc3env worker image (its `environment` target), which holds your game files.")
@click.option("--source", type=click.Path(exists=True, file_okay=False, path_type=Path),
              help="Checkout of this plugin to build. Default: the one an editable install runs from, or the cwd.")
@click.option("--image", help="Register this existing local image instead of building one.")
@click.option("--fake", is_flag=True,
              help="Build the env on wc3env's fake game instead (a town hall, five peasants and a distant enemy hall; "
                   "units move and stop, nothing else), for this machine's own architecture: every task runs end to "
                   "end, on a Mac too, without Warcraft III.")
@click.option("--wc3env", "wc3env_dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
              envvar="WC3ENV_DIR", help="A wc3env checkout for --fake. Default: $WC3ENV_DIR, else wc3env at the "
                                        "pinned commit, downloaded.")
@click.option("--agent", is_flag=True, help="Also build and register the wc3-macro-micro agent (wc3env's wc3agent).")
def setup(env_id: str, base: str, source: Path | None, image: str | None, fake: bool, wc3env_dir: Path | None,
          agent: bool):
    """Build the env image on top of your wc3env worker image and register it as an MCP server env. The image holds
    your game files: it stays in agent-env's local registry; never push it anywhere public. With --fake, the env is
    built on wc3env's fake game instead; with --agent, the wc3-macro-micro agent is built and registered too."""
    from agent_env.artifact import DockerImageArtifact
    from agent_env.env import MCPServerEnv

    root = _checkout(source)
    if image is None:
        if fake:
            build_platform = _docker_platform()
            with tempfile.TemporaryDirectory() as tmp:
                _build_standin(root, _wc3env(wc3env_dir, Path(tmp)), build_platform)
            base = STANDIN_IMAGE
        else:
            build_platform = "linux/amd64"
            if not _image_exists(base):
                raise click.ClickException(f"no image {base}: build wc3env's worker image from your own installation "
                                           f"first ({WC3ENV}/blob/main/docker/README.md), pass --base, or build the "
                                           "fake game with --fake")
        image = f"mcp-server-{env_id}"
        click.echo(f"Building {image} on {base} for {build_platform} from {root}")
        build = ["docker", "build", "--platform", build_platform, "--build-arg", f"BASE={base}", "-t", image,
                 str(root)]
        if subprocess.run(build).returncode:
            raise click.ClickException("docker build failed")
    click.echo("Storing the image (docker save, can take a few minutes" + ("" if fake else ": it holds the game") + ")")
    description = ("Warcraft III env on wc3env's fake game" if fake else
                   "Warcraft III env: wc3env under Wine and its MCP server")
    artifact = DockerImageArtifact.put(id=f"mcp-server-{env_id}", image_name=image, description=description)
    env = MCPServerEnv.put(id=env_id, docker_image_artifact=artifact, environment_name=ENVIRONMENT_NAME,
                           env_provider_type="server")
    click.echo(f"Registered env {env.id!r} version {env.version} (image {artifact.image_name}"
               + (", the FAKE game" if fake else "") + ")")
    if agent:
        _register_agent(root, _docker_platform())
    click.echo("Next: agent-env run wc3 --task " + ("macro-micro-quick" if agent else "smoke"))


def _live_views() -> list[tuple[str, str]]:
    """The live view of each wc3 env running in Docker, newest first, with its container's name."""
    try:
        out = subprocess.run(["docker", "ps", "--format", "{{.Image}}\t{{.Ports}}\t{{.Names}}"], capture_output=True,
                             text=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    views = []
    for line in out.splitlines():
        image, ports, name = (line.split("\t") + ["", ""])[:3]
        if "mcp-server-wc3" in image and (port := ENV_PORT.search(ports)):
            views.append((f"http://127.0.0.1:{port[1]}/live", name))
    return views


@wc3.command()
@click.option("--open", "open_page", is_flag=True, help="Open the newest game's live view in the browser.")
def watch(open_page: bool):
    """Print the live view of every Warcraft III game running in Docker, newest first: the map, every unit in view,
    each player's resources and what happened, as the game plays (add ?stream for a 1920x1080 broadcast layout)."""
    views = _live_views()
    for url, name in views:
        click.echo(f"{url}  ({name})")
    if not views:
        raise click.ClickException("no wc3 env is running; agent-env run wc3 --task ... starts one")
    if open_page:
        click.launch(views[0][0])


@wc3.command()
@click.option("--port", default=18765, show_default=True)
@click.option("--worker", help="The game worker's command line. Default: wc3env's fake game (no Warcraft III).")
def serve(port: int, worker: str | None):
    """Serve the env on this machine. Without --worker it plays wc3env's fake game, a tiny stand-in world, so you
    can try the tools and your agent without the game."""
    fake = shlex.quote(str(Path(__file__).with_name("worker.py")))
    command = worker or f"{shlex.quote(sys.executable)} {fake} --fake"
    os.environ.update({"WC3_WORKER_CMD": command, "MCP_PORT": str(port)})
    click.echo(f"Serving the wc3 env at http://127.0.0.1:{port}/mcp with {command}")
    from .server import main

    main()
