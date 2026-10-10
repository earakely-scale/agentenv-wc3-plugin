"""`agent-env wc3`: check what a Warcraft III env needs on this machine, build wc3env's worker image from your game
files, build and register the env from it (or on wc3env's fake game, on any machine), build and register the agents,
serve the env locally against the fake game, watch a running game live, and copy a run's match files and broadcast
into one folder."""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.parse
import urllib.request
from pathlib import Path

import click
import yaml
from agent_env.config import get_config

from . import drills, license, sweep

ENVIRONMENT_NAME = "wc3"
BASE_IMAGE = "wc3-worker:local"
STANDIN_IMAGE = "wc3-worker:standin"
WC3ENV_COMMIT = "eb660aa558fb6e5c639a1ff404082f7dc0ee483e"   # the one agents/wc3-player/Dockerfile pins
# wc3env's hook at WC3ENV_COMMIT with patches/, as the Hook workflow built and released it.
HOOK_RELEASE = "wc3hook-eb660aa-261b1079f262"
HOOK_URL = f"https://github.com/earakely-scale/agentenv-wc3-plugin/releases/download/{HOOK_RELEASE}/wc3hook.dll"
HOOK_SHA256 = "9d072cf8b15a65fac54950a5c20c56fefcca72e9966c0bbe2fd9aa846ee07d76"
# Each agent `setup --agent` registers: its directory under agents/, what it is, and its A2AAgent metadata.
AGENTS = {
    "wc3-macro-micro": ("wc3-player", "Warcraft III wc3agent (macro + micro)",
                        {"default_model": "anthropic/claude-sonnet-5-5"}),
    "wc3-scripted": ("wc3-scripted", "Warcraft III scripted opponent (attack, raid or idle)", {}),
    "wc3-llm": ("wc3-llm", "Warcraft III: any chat model through the env's MCP tools",
                {"default_model": "anthropic/claude-haiku-4-5"}),
}
ENV_PORT = re.compile(r":(\d+)->18765/tcp")
STYLES = ("raw", "commander")   # styles.py: the game styles an env image serves


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


def _register_agent(root: Path, build_platform: str, agent_id: str) -> None:
    """Build one of AGENTS from its directory under agents/ and register it under its id."""
    from agent_env.a2a_agent import A2AAgent
    from agent_env.artifact import DockerImageArtifact

    directory, description, metadata = AGENTS[agent_id]
    dockerfile, image = root / "agents" / directory / "Dockerfile", f"a2a-agent-{agent_id}"
    click.echo(f"Building {image} for {build_platform}")
    build = ["docker", "build", "--platform", build_platform, "-t", image, "-f", str(dockerfile), str(root)]
    if subprocess.run(build).returncode:
        raise click.ClickException(f"docker build of the {agent_id} agent failed")
    artifact = DockerImageArtifact.put(id=image, image_name=image, description=description,
                                       build_context_path=str(root), dockerfile_path=str(dockerfile))
    agent = A2AAgent.put(id=agent_id, docker_image_artifact=artifact, metadata=metadata)
    click.echo(f"Registered A2A agent {agent.id!r} version {agent.version} ({description})")


def _game_env(root: Path) -> str:
    """The agentenv-game-env archive the plugin's pyproject pins, for the env image."""
    pinned = next((d for d in tomllib.loads((root / "pyproject.toml").read_text())["project"]["dependencies"]
                   if d.startswith("agentenv-game-env @ ")), None)
    if pinned is None:
        raise click.ClickException(f"{root}/pyproject.toml pins no agentenv-game-env archive")
    return pinned.removeprefix("agentenv-game-env @ ").strip()


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
        click.echo(f"✗ no image {base}: build it from your own installation with agent-env wc3 build-worker "
                   "GAME_DIR")
    where, problem = _license_source(license_dir)
    ok = ok and problem is None
    click.echo(f"✓ activation files in {where}" if problem is None else f"✗ {problem}")
    if not ok:
        raise SystemExit(1)


@wc3.command("build-worker")
@click.argument("game_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--tag", default=BASE_IMAGE, show_default=True, help="The worker image to build.")
@click.option("--source", type=click.Path(exists=True, file_okay=False, path_type=Path),
              help="Checkout of this plugin, for its patches. Default: the one an editable install runs from, or the "
                   "cwd.")
def build_worker(game_dir: Path, tag: str, source: Path | None):
    """Build wc3env's worker image on this x86-64 host from your Warcraft III Legacy installation in GAME_DIR, with no
    Windows machine: wc3env at the pinned commit with this plugin's patches, the hook the plugin's Hook workflow
    built for them (its hash checked), and the game files wc3env's prepare.py picks, never the activation files."""
    if platform.machine().lower() not in ("x86_64", "amd64"):
        raise click.ClickException(f"this machine is {platform.machine()}: build the worker on an x86-64 host")
    root = _checkout(source)
    with tempfile.TemporaryDirectory() as tmp:
        wc3env = _wc3env(None, Path(tmp))
        for patch in sorted((root / "patches").glob("*.patch")):
            click.echo(f"Applying {patch.name}")
            if subprocess.run(["git", "apply", str(patch)], cwd=wc3env).returncode:
                raise click.ClickException(f"{patch.name} does not apply to wc3env {WC3ENV_COMMIT[:7]}")
        click.echo(f"Downloading the hook ({HOOK_RELEASE})")
        with urllib.request.urlopen(HOOK_URL, timeout=300) as r:
            hook = r.read()
        if hashlib.sha256(hook).hexdigest() != HOOK_SHA256:
            raise click.ClickException(f"{HOOK_URL} is not the hook this plugin pins (its SHA-256 differs)")
        (wc3env / "src/wc3env/native").mkdir(parents=True, exist_ok=True)
        (wc3env / "src/wc3env/native/wc3hook.dll").write_bytes(hook)
        context = Path(tmp) / "context"
        prepare = [sys.executable, str(wc3env / "docker/prepare.py"), "--game-dir", str(game_dir), "--output",
                   str(context)]
        if subprocess.run(prepare).returncode:
            raise click.ClickException(f"wc3env's prepare.py refused {game_dir}: it needs Warcraft III Legacy 1.29.2")
        for path in [context, *context.rglob("*")]:   # the image's build reads it as a non-root user
            path.chmod(path.stat().st_mode | (0o555 if path.is_dir() else 0o444))
        click.echo(f"Building {tag} (linux/amd64): Wine and the game, several minutes")
        build = ["docker", "build", "--platform", "linux/amd64", "--target", "environment", "-t", tag, str(context)]
        if subprocess.run(build).returncode:
            raise click.ClickException("docker build of the worker failed")
    click.echo(f"Built {tag}. Next: agent-env wc3 license import {game_dir}, then agent-env wc3 setup --agent")


def _license_source(license_dir: Path | None = None) -> tuple[str, str | None]:
    """Whether the tasks' add_license steps find the activation files in agent-env's secret store here, and what to
    do if not (no contents shown)."""
    where = "agent-env's secret store (" + ", ".join(license.LICENSE_SECRETS.values()) + ")"
    try:
        if license.license_from_secrets(license.LICENSE_SECRETS):
            return where, None
    except RuntimeError as e:
        return where, str(e)
    directory = license.license_dir(str(license_dir) if license_dir else None)
    found = license.read_license(directory) is not None
    return where, (f"no activation files in {where}: "
                   + (f"agent-env wc3 license import {directory} stores the ones there" if found else
                      "copy roc.w3k and tft.w3k from your Warcraft III Legacy installation into a folder, then "
                      "agent-env wc3 license import DIR"))


def _local_secrets_file() -> Path | None:
    """The YAML file behind agent-env's local secret store ([stores.secret] config.file_path), if it is one."""
    section = get_config().section("stores", "secret")
    if section.get("impl") and "LocalSecretStore" not in str(section["impl"]):
        return None
    path = (section.get("config") or {}).get("file_path")
    return Path(path).expanduser() if path else None


@wc3.group("license")
def license_group():
    """Your activation files (roc.w3k, tft.w3k). A task's add_license step gives them to the env from agent-env's
    secret store, where they are WC3_ROC_W3K and WC3_TFT_W3K (each file in base64), so a run on any machine finds
    them. `import` puts them there from a folder ([plugins.agentenv-wc3] license_dir, $WC3_LICENSE_DIR,
    ~/.wc3-license)."""


@license_group.command("import")
@click.argument("directory", required=False, type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--secrets-file", type=click.Path(dir_okay=False, path_type=Path),
              help="The YAML secrets file to write. Default: the file of agent-env's local secret store "
                   "([stores.secret] config.file_path).")
def import_license(directory: Path | None, secrets_file: Path | None):
    """Store the activation files in DIRECTORY (default: the license folder) in agent-env's local secret store, as
    WC3_ROC_W3K and WC3_TFT_W3K. For a cloud secret store, store the base64 of each file under those names."""
    directory = directory or license.license_dir()
    files = license.read_license(directory)
    if files is None:
        raise click.ClickException(f"no roc.w3k and tft.w3k in {directory}")
    configured = _local_secrets_file()
    target = (secrets_file.expanduser() if secrets_file else None) or configured
    if target is None:
        raise click.ClickException(
            "agent-env's secret store here has no local file: name one with --secrets-file and set [stores.secret] "
            "config.file_path to it, or store the base64 of roc.w3k as WC3_ROC_W3K and of tft.w3k as WC3_TFT_W3K in "
            "your secret store yourself")
    doc = (yaml.safe_load(target.read_text()) if target.is_file() else None) or {}
    if not isinstance(doc, dict):
        raise click.ClickException(f"{target} is not a flat mapping of secret names to values")
    doc.update({license.LICENSE_SECRETS[name]: value for name, value in files.items()})
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(doc, sort_keys=False, width=float("inf")))
    target.chmod(0o600)
    click.echo(f"Stored roc.w3k and tft.w3k from {directory} as WC3_ROC_W3K and WC3_TFT_W3K in {target}"
               + ("" if target == configured else f"; set [stores.secret] config.file_path to {target}"))


@license_group.command("show")
def show_license():
    """Whether add_license finds the activation files in agent-env's secret store here (their contents are never
    shown)."""
    where, problem = _license_source()
    click.echo(f"The activation files come from {where}" if problem is None else problem)
    if problem is not None:
        raise SystemExit(1)


@wc3.command()
@click.option("--id", "env_id", help="Env id to register. Default: wc3 for the raw style (the bundle's tasks deploy "
                                     "it), wc3-<style> for another.")
@click.option("--style", type=click.Choice(STYLES), default="raw", show_default=True,
              help="The game style: raw, each unit's orders by id through general tools; commander, wc3agent's "
                   "interface (named units, an order language, groups with objectives, unit menus).")
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
@click.option("--agent", is_flag=True, help="Also build and register the agents: wc3-llm (any chat model through the "
                                            "tools), wc3-macro-micro (wc3env's wc3agent) and wc3-scripted (its "
                                            "scripted opponent).")
def setup(env_id: str | None, style: str, base: str, source: Path | None, image: str | None, fake: bool,
          wc3env_dir: Path | None, agent: bool):
    """Build the env image on top of your wc3env worker image and register it as an MCP server env. The image holds
    your game files: it stays in agent-env's local registry; never push it anywhere public. With --fake, the env is
    built on wc3env's fake game instead; with --agent, the wc3-llm, wc3-macro-micro and wc3-scripted agents are
    built and registered too."""
    from agent_env.artifact import DockerImageArtifact
    from agent_env.env import MCPServerEnv

    root = _checkout(source)
    env_id = env_id or (ENVIRONMENT_NAME if style == "raw" else f"{ENVIRONMENT_NAME}-{style}")
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
                                           "first (agent-env wc3 build-worker GAME_DIR), pass --base, or build the "
                                           "fake game with --fake")
        image = f"mcp-server-{env_id}"
        click.echo(f"Building {image} on {base} for {build_platform} from {root}")
        build = ["docker", "build", "--platform", build_platform, "--build-arg", f"BASE={base}", "--build-arg",
                 f"GAME_ENV={_game_env(root)}", "--build-arg", f"WC3ENV_COMMIT={WC3ENV_COMMIT}", "--build-arg",
                 f"STYLE={style}", "-t", image, str(root)]
        if subprocess.run(build).returncode:
            raise click.ClickException("docker build failed")
    click.echo("Storing the image (docker save, can take a few minutes" + ("" if fake else ": it holds the game") + ")")
    description = ("Warcraft III env on wc3env's fake game" if fake else
                   "Warcraft III env: wc3env under Wine and its MCP server") + f", {style} style"
    artifact = DockerImageArtifact.put(id=f"mcp-server-{env_id}", image_name=image, description=description)
    env = MCPServerEnv.put(id=env_id, docker_image_artifact=artifact, environment_name=ENVIRONMENT_NAME,
                           env_provider_type="server")
    click.echo(f"Registered env {env.id!r} version {env.version} (image {artifact.image_name}"
               + (", the FAKE game" if fake else "") + ")")
    if agent:
        build_platform = _docker_platform()
        for agent_id in AGENTS:
            _register_agent(root, build_platform, agent_id)
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
    each player's resources and what happened, as the game plays (add ?view for the game alone, as a broadcast frames
    it)."""
    views = _live_views()
    for url, name in views:
        click.echo(f"{url}  ({name})")
    if not views:
        raise click.ClickException("no wc3 env is running; agent-env run wc3 --task ... starts one")
    if open_page:
        click.launch(views[0][0])


@wc3.command()
@click.argument("instance", required=False)
@click.option("--out", "out_dir", type=click.Path(file_okay=False, path_type=Path), default=Path("recordings"),
              show_default=True, help="The folder to copy them into.")
def recordings(instance: str | None, out_dir: Path):
    """Copy a run's match files and broadcast video into one folder: the game's video, its highlight reel, the map's
    video, the HTML replay (which plays the game's video beside the map), the .w3g and the broadcast. INSTANCE is the
    one `agent-env run` printed; without it, the newest run that saved any."""
    from agent_env.artifact import FileArtifact
    from agent_env.store.document_store import Filter, Sort
    from agent_env.store.routing import namespace_routing
    from agent_env.task.store import TASK_INSTANCES_COLLECTION

    with namespace_routing():   # `agent-env run`'s instances live in the @local namespace's own store
        docs = get_config().get_document_store()
        found = docs.query(TASK_INSTANCES_COLLECTION, Filter.of(**({"instance_id": instance} if instance else {})),
                           sort=Sort.by("created_at_utc"), limit=None if instance else 50)
        saved = []
        for doc in found:
            metadata = (doc.get("context") or {}).get("metadata") or {}
            saved = [*(f for files in (metadata.get("match_files") or {}).values() for f in files),
                     *(f for b in (metadata.get("broadcasts") or {}).values() for f in b.get("videos") or ())]
            if saved:
                break
        if not saved:
            raise click.ClickException(f"no run {instance!r} with a recording" if instance else
                                       "no run has saved a recording yet: agent-env run wc3 --task smoke")
        out_dir.mkdir(parents=True, exist_ok=True)
        click.echo(f"{doc['instance_id']} ({doc.get('created_at_utc')}):")
        for f in saved:
            content = FileArtifact.get(f["artifact_id"], f["version"]).load()
            (out_dir / f["name"]).write_bytes(content)
            click.echo(f"  {out_dir / f['name']}  {len(content) / 1e6:.1f} MB")
    page = next((f["name"] for f in saved if f["name"].endswith(".html")), None)
    if page:
        click.echo(f"Open {out_dir / page} to watch it, with the game's video beside the map when it has one.")


@wc3.command()
@click.argument("replay", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--task", "task_file", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="The task the game played: its map, player slots and staging.")
@click.option("--timeline", "timeline_file", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="The game's timeline, for the plans its agent wrote.")
@click.option("--out", "out_file", required=True, type=click.Path(dir_okay=False, path_type=Path),
              help="The MP4 to write; <out>.json beside it has the frames' pace and the game's final scores.")
@click.option("--speed", type=float, default=1.0, show_default=True, help="Game seconds per video second.")
@click.option("--fps", type=int, default=20, show_default=True)
@click.option("--image", default="mcp-server-wc3:latest", show_default=True, help="The env's image (setup builds it).")
@click.option("--source", type=click.Path(exists=True, file_okay=False, path_type=Path),
              help="A plugin checkout whose src/ runs in the image instead of the image's own code.")
def render(replay: Path, task_file: Path, timeline_file: Path | None, out_file: Path, speed: float, fps: int,
           image: str, source: Path | None):
    """A game's video in the game's own picture, from its replay (.w3g, with its .w3g.json beside it): the env plays
    it back with the picture on, stages it as the task did, and points the camera as a live game's director does.
    Needs the env's image and your activation files, as a real game does."""
    from .replay_video import spec_of, startup_of

    startup = replay.with_name(replay.name + ".json")
    if not startup.is_file():
        raise click.ClickException(f"no {startup.name} beside the replay: wc3env plays a replay back with the setup "
                                   "it saved there")
    files = license.license_from_secrets(license.LICENSE_SECRETS) or license.read_license(license.license_dir())
    if not files:
        raise click.ClickException(_license_source()[1] or "no activation files")
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        work.chmod(0o777)   # the image's user writes the video here
        task = json.loads(task_file.read_text())
        saved = startup_of(task, json.loads(startup.read_text()))
        shutil.copyfile(replay, work / "game.w3g")
        (work / "game.w3g.json").write_text(json.dumps(saved))
        timeline = json.loads(timeline_file.read_text()) if timeline_file else None
        spec = spec_of(task, "Z:\\work\\game.w3g", saved, timeline, speed, fps, "/work/video.mp4")
        (work / "spec.json").write_text(json.dumps(spec))
        mounts = ["-v", f"{work}:/work"] + (["-v", f"{source.resolve() / 'src'}:/src:ro", "-e", "PYTHONPATH=/src"]
                                            if source else [])
        command = ["docker", "run", "--rm", *mounts, "-e", "WC3_ROC_W3K", "-e", "WC3_TFT_W3K", "--entrypoint", "bash",
                   image, "/opt/agentenv/with-display.sh", "/opt/agentenv/bin/python", "-m",
                   "agentenv_wc3.replay_video", "/work/spec.json"]
        env = {**os.environ, "WC3_ROC_W3K": files["roc.w3k"], "WC3_TFT_W3K": files["tft.w3k"]}
        proc = subprocess.run(command, env=env, capture_output=True, text=True)
        if proc.returncode or not (work / "video.mp4").is_file():
            raise click.ClickException(f"the render failed: {(proc.stderr or proc.stdout).strip()[-1500:]}")
        out_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(work / "video.mp4", out_file)
        shutil.copyfile(work / "video.mp4.json", out_file.with_name(out_file.name + ".json"))
    summary = json.loads(out_file.with_name(out_file.name + ".json").read_text())
    click.echo(f"{out_file}: {summary['frames']} frames, game {summary['start']:.0f}-{summary['end']:.0f} s, "
               f"{summary['result'] or 'no result'}, scores {summary['scores']}")


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


@wc3.group("drills")
def drills_group():
    """Drills: short staged tasks with a goal and checks, from wc3agent's scenarios."""


@drills_group.command("import")
@click.option("--wc3env", "wc3env_dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
              envvar="WC3ENV_DIR", help="A wc3env checkout. Default: $WC3ENV_DIR, else wc3env at the pinned commit, "
                                        "downloaded.")
@click.option("--out", "out_dir", type=click.Path(file_okay=False, path_type=Path), default=drills.TASKS,
              help="Where the task files go. Default: the wc3 bundle's tasks.")
def import_drills(wc3env_dir: Path | None, out_dir: Path):
    """Convert wc3agent's scenario definitions into drill tasks, drill-<name>.json, once: the bundle owns them from
    then on."""
    with tempfile.TemporaryDirectory() as tmp:
        try:
            written = drills.import_all(_wc3env(wc3env_dir, Path(tmp)) / drills.DEFINITIONS, out_dir)
        except ValueError as e:
            raise click.ClickException(str(e)) from e
    click.echo(f"Wrote {len(written)} drill tasks into {out_dir}")


@wc3.group("sweep")
def sweep_group():
    """Sweeps: one template task crossed over models, maps, races, opponents and seeds, played as an eval under a
    spend budget and tabulated."""


@sweep_group.command("generate")
@click.argument("spec_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.argument("out_dir", type=click.Path(file_okay=False, path_type=Path))
def generate_sweep(spec_file: Path, out_dir: Path):
    """Write the sweep SPEC_FILE describes into OUT_DIR, a bundle folder: a task per combination, an eval over
    them and sweep.json, each task's axes."""
    try:
        names = sweep.generate(sweep.Spec.load(spec_file), out_dir)
    except (ValueError, TypeError) as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"Wrote {len(names)} tasks into {out_dir}; play them with: agent-env wc3 sweep run {out_dir} "
               f"--budget <usd>")


@sweep_group.command("run")
@click.argument("out_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--budget", type=float, required=True,
              help="The model spend, in USD, the sweep stays within: no game starts that could take it past this "
                   "at the sweep's per-game cap.")
@click.option("--parallel", default=2, show_default=True, help="Games at a time.")
def run_sweep(out_dir: Path, budget: float, parallel: int):
    """Play the sweep in OUT_DIR, one `agent-env run` per game, logged under OUT_DIR/logs. Each finished game is
    a line of OUT_DIR/results.jsonl; run it again to play the games not in it yet."""
    sweep.run(out_dir, budget, parallel, echo=click.echo)
    click.echo(sweep.report(out_dir))


@sweep_group.command("report")
@click.argument("out_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--out", "out_file", type=click.Path(dir_okay=False, path_type=Path),
              help="Also write the report to this Markdown file.")
def report_sweep(out_dir: Path, out_file: Path | None):
    """The sweep's results by model, map and seed."""
    text = sweep.report(out_dir)
    if out_file:
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(text)
    click.echo(text)
