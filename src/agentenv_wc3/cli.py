"""`agent-env wc3`: check what a Warcraft III env needs on this machine, build and register the env from your
wc3env worker image, and serve the env locally against wc3env's fake game."""

from __future__ import annotations

import os
import platform
import shlex
import subprocess
import sys
from pathlib import Path

import click

from . import steps

ENVIRONMENT_NAME = "wc3"
BASE_IMAGE = "wc3-worker:local"
WC3ENV = "https://github.com/pwang724/wc3env"


def _checkout(source: Path | None) -> Path:
    """The checkout of this plugin to build from: `source`, else the one an editable install runs from, else the
    cwd."""
    for root in [source] if source else [Path(__file__).resolve().parents[2], Path.cwd()]:
        if (root / "Dockerfile").is_file() and (root / "src/agentenv_wc3").is_dir():
            return root
    raise click.UsageError("no checkout of agentenv-wc3-plugin found (a Dockerfile and src/agentenv_wc3); pass "
                           "--source")


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
def setup(env_id: str, base: str, source: Path | None, image: str | None):
    """Build the env image on top of your wc3env worker image and register it as an MCP server env. The image holds
    your game files: it stays in agent-env's local registry; never push it anywhere public."""
    from agent_env.artifact import DockerImageArtifact
    from agent_env.env import MCPServerEnv

    if image is None:
        root = _checkout(source)
        if not _image_exists(base):
            raise click.ClickException(f"no image {base}: build wc3env's worker image from your own installation "
                                       f"first ({WC3ENV}/blob/main/docker/README.md), or pass --base")
        image = f"mcp-server-{env_id}"
        click.echo(f"Building {image} on {base} from {root}")
        build = ["docker", "build", "--platform", "linux/amd64", "--build-arg", f"BASE={base}", "-t", image, str(root)]
        if subprocess.run(build).returncode:
            raise click.ClickException("docker build failed")
    click.echo("Storing the image (docker save, can take a few minutes: it holds the game)")
    artifact = DockerImageArtifact.put(id=f"mcp-server-{env_id}", image_name=image,
                                       description="Warcraft III env: wc3env under Wine and its MCP server")
    env = MCPServerEnv.put(id=env_id, docker_image_artifact=artifact, environment_name=ENVIRONMENT_NAME,
                           env_provider_type="server")
    click.echo(f"Registered env {env.id!r} version {env.version} (image {artifact.image_name})")
    click.echo("Next: agent-env run wc3 --task vs-ai-quick")


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
