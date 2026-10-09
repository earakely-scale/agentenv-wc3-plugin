import json
import shlex
import sys
import tomllib
from pathlib import Path

import pytest
from mcp.server.fastmcp.exceptions import ToolError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentenv_game import SlotRequest  # noqa: E402

from agentenv_wc3.server import WORKER, WC3Env  # noqa: E402

FAKE_CMD = f"{shlex.quote(sys.executable)} {shlex.quote(str(WORKER))} --fake"
STEPS = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())["project"][
    "entry-points"]["agent_env.task_steps"]


ONE_AGENT = [{"agent": "wc3", "race": "human", "team": 1}, {"computer": "normal", "race": "orc", "team": 2}]


def slot_request(index: int, player: dict) -> SlotRequest:
    """A player, {agent | computer, race, team, label, ai_assist, autocast, omniscient}, as the fill of player slot
    `index`."""
    given = {"faction": player.get("race", "random"),
             **{k: player[k] for k in ("team", "label", "ai_assist", "autocast", "omniscient") if k in player}}
    if "computer" in player:
        return SlotRequest(player_id=str(index), player_kind="ai",
                           game_settings={**given, "ai_level": player["computer"]})
    return SlotRequest(player_id=str(index), player_kind="agent", player_name=player["agent"], game_settings=given)


async def new_game(env, players: list[dict] | None = None, **settings):
    """A game through the env's lobby, in process: opened with `settings` (a 2-minute game unless they say), a player
    slot per player slot (one agent against the normal AI by default), closed."""
    env.new_lobby({"time_limit_seconds": 120, **settings})
    for index, player in enumerate(players or ONE_AGENT):
        env.fill_slot(slot_request(index, player))
    return await env.close_lobby()


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def env_vars(monkeypatch):
    """The env's configuration: wc3env's fake game behind the worker, a 2-minute game."""
    monkeypatch.setenv("WC3_WORKER_CMD", FAKE_CMD)
    monkeypatch.setenv("WC3_TIME_LIMIT_SECONDS", "120")
    for key in ("ENVIRONMENT_NAME", "WC3_MAP", "WC3_RACE", "WC3_OPPONENT_RACE", "WC3_AI_DIFFICULTY", "WC3_SEED"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
async def env(env_vars):
    e = WC3Env()
    e.create_app()
    yield e
    await e.close()


class Tools:
    """Calls tools through FastMCP, as an MCP client would: argument validation and error wrapping included."""

    def __init__(self, env):
        self.env = env

    async def __call__(self, name: str, **args) -> str:
        return (await self.env.mcp.call_tool(name, args))[0].text

    async def error(self, name: str, **args) -> str:
        with pytest.raises(ToolError) as info:
            await self.env.mcp.call_tool(name, args)
        return str(info.value)


@pytest.fixture
def tools(env):
    return Tools(env)


@pytest.fixture
def local_stores(monkeypatch, tmp_path):
    """agent-env on its local default stores under tmp_path, whatever config the machine has. The config names the
    checkout's task steps too, so they register when the installed entry points are older than the checkout."""
    import os

    from agent_env.config import reset_config

    config = tmp_path / "config.toml"
    config.write_text(f"[task_steps]\nimpls = {json.dumps(sorted(STEPS.values()))}\n")
    for var in [v for v in os.environ if v.startswith("AGENT_ENV_")]:
        monkeypatch.delenv(var)
    monkeypatch.setenv("AGENT_ENV_CONFIG", str(config))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    reset_config()
    yield tmp_path / "state"
    reset_config()


@pytest.fixture
def license_dir(tmp_path, monkeypatch):
    """Stand-in activation files, where the env looks for them (WC3_LICENSE_DIR)."""
    directory = tmp_path / "wc3-license"
    directory.mkdir()
    (directory / "roc.w3k").write_bytes(b"roc key")
    (directory / "tft.w3k").write_bytes(b"tft key")
    monkeypatch.setenv("WC3_LICENSE_DIR", str(directory))
    return directory
