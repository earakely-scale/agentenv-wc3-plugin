import shlex
import sys
from pathlib import Path

import pytest
from mcp.server.fastmcp.exceptions import ToolError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentenv_wc3.server import WORKER, WC3Env  # noqa: E402

FAKE_CMD = f"{shlex.quote(sys.executable)} {shlex.quote(str(WORKER))} --fake"


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
    """agent-env on its local default stores under tmp_path, whatever config the machine has."""
    import os

    from agent_env.config import reset_config

    config = tmp_path / "config.toml"
    config.write_text("")
    for var in [v for v in os.environ if v.startswith("AGENT_ENV_")]:
        monkeypatch.delenv(var)
    monkeypatch.setenv("AGENT_ENV_CONFIG", str(config))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    reset_config()
    yield tmp_path / "state"
    reset_config()


@pytest.fixture
def license_dir(tmp_path, monkeypatch):
    """Stand-in activation files, where the wc3_match step looks for them."""
    directory = tmp_path / "wc3-license"
    directory.mkdir()
    (directory / "roc.w3k").write_bytes(b"roc key")
    (directory / "tft.w3k").write_bytes(b"tft key")
    monkeypatch.setenv("WC3_LICENSE_DIR", str(directory))
    return directory
