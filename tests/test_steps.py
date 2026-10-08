"""The task steps against the env served over HTTP (on wc3env's fake game), with agent-env's local stores."""

import asyncio
import base64
import os
import socket
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import uvicorn
import yaml
from agent_env.artifact import FileArtifact
from agent_env.config import reset_config
from agent_env.env.env import DeployedEnv
from agent_env.task.task import Task
from agent_env.task_step.context import TaskStepContext
from agent_env.task_step.registry import get_task_step_registry
from agentenv_game import LICENSE
from agentenv_game.steps import (
    AddLicenseTaskStep,
    AddPlayerSlotTaskStep,
    CloseLobbyTaskStep,
    FinishMatchTaskStep,
    OpenLobbyTaskStep,
)
from agentenv_protocol import AgentEnvEnvironment, client, environment_card, extension
from agentenv_protocol.types import WELL_KNOWN_PATH
from click.testing import CliRunner
from conftest import ONE_AGENT, slot_request

from agentenv_rts.grade import RTSGradeTaskStep
from agentenv_wc3.license import LICENSE_SECRETS, license_from_secrets, read_license
from agentenv_wc3.server import WC3Env
from agentenv_wc3.steps import REPLAY_EXTENSION, SaveWC3ReplayTaskStep

pytestmark = pytest.mark.anyio

BUNDLE = Path(__file__).resolve().parents[1] / "src/agentenv_wc3/bundles/wc3"


@asynccontextmanager
async def deployed(env: AgentEnvEnvironment):
    """The env served on a free port, as the record a deploy_env step leaves."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(env.create_app().streamable_http_app(), host="127.0.0.1", port=port,
                                           log_level="warning"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    base = f"http://127.0.0.1:{port}"
    try:
        yield DeployedEnv(env_id="wc3", env_version=1, environment_card_url=base + WELL_KNOWN_PATH,
                          environment_card=await client.get_card(base))
    finally:
        server.should_exit = True
        await serving
        if isinstance(env, WC3Env):
            await env.close()


def run_context(record: DeployedEnv) -> TaskStepContext:
    return TaskStepContext(deployed_envs=[record], metadata={"task_id": "smoke"}, instance_id="i1")


def slot_of(index: int, player: dict) -> AddPlayerSlotTaskStep:
    """A player slot as a task's add_player_slot step (the agent connects on its own here)."""
    fill = slot_request(index, player)
    return AddPlayerSlotTaskStep(id=f"slot-{index}", version=None, env_id="wc3", player_id=fill.player_id,
                                 player_kind=fill.player_kind, player_name=fill.player_name,
                                 game_settings=fill.game_settings, register=False)


async def open_match(record: DeployedEnv, **settings) -> TaskStepContext:
    """A match opened as a task opens one: the activation files (the fake game needs none), then open_lobby with
    the match's settings."""
    context = run_context(record)
    await AddLicenseTaskStep(id="license", version=None, env_id="wc3", files=LICENSE_SECRETS).execute(context)
    return await OpenLobbyTaskStep(id="match", version=None, env_id="wc3", game_settings=settings).execute(context)


async def fill_and_close(context: TaskStepContext, players: list[dict] | None = None) -> TaskStepContext:
    """The players in their player slots and the game created, as a task's add_player_slot and close_lobby steps
    do."""
    for index, player in enumerate(players or ONE_AGENT):
        await slot_of(index, player).execute(context)
    return await CloseLobbyTaskStep(id="start", version=None, env_id="wc3").execute(context)


def test_the_steps_are_registered():
    registry = get_task_step_registry()
    assert registry["save_wc3_replay"] is SaveWC3ReplayTaskStep and "wc3_license" not in registry
    assert registry["open_lobby"] is OpenLobbyTaskStep and registry["add_license"] is AddLicenseTaskStep
    assert "rts_finish" not in registry and registry["finish_match"] is FinishMatchTaskStep


async def test_a_match_finish_plays_out_passes_the_smoke_rubric(env_vars, license_dir):
    env = WC3Env()
    async with deployed(env) as record:
        context = await open_match(record, seed=4, time_limit_seconds=90)
        assert context.metadata["game_lobby"]["game_settings"]["seed"] == 4
        assert context.metadata["game_lobby"]["game_settings"]["time_limit_seconds"] == 90
        await fill_and_close(context, [{"agent": "player", "race": "human"}, {"computer": "normal", "race": "orc"}])
        assert context.metadata["game_lobby"]["status"] == "closed"
        await FinishMatchTaskStep(id="finish", version=None, env_id="wc3").execute(context)
        match = context.metadata["game_match"]
        assert (match["status"], match["status_detail"]) == ("finished", "the time limit, 1:30; played out from 0:00")
        assert match["progress"] == [{"name": "game", "unit": "seconds", "value": 90.0, "limit": 90}]
        assert {p: s["status"] for p, s in match["player_states"].items()} == {"0": "undecided", "1": "undecided"}
        smoke = await RTSGradeTaskStep(id="grade", version=None, env_id="wc3", rubric="smoke").execute(context)
        [verification] = smoke.metadata["verifications"].values()
        assert verification["score"] == 1 and all(r["result"] for r in verification["results"]), verification
        finish = next(r for r in verification["results"] if r["name"] == "finish")
        assert finish["evidence"] == "the match finished: the time limit, 1:30; played out from 0:00"
        # The agents' rubric sees a game the harness played, and gives it nothing.
        melee = await RTSGradeTaskStep(id="grade", version=None, env_id="wc3").execute(run_context(record))
        [verification] = melee.metadata["verifications"].values()
        gate = next(r for r in verification["results"] if r["name"] == "agent_played")
        assert gate["result"] is False and verification["score"] == 0


async def test_the_fake_game_needs_no_activation_files_and_reads_no_secrets(env_vars, local_stores):
    async with deployed(WC3Env()) as record:
        status = await client.invoke_extension(record.environment_url, record.environment_card, LICENSE, method="get")
        assert status == {"missing": [], "installed": []}
        context = await fill_and_close(await open_match(record, time_limit_seconds=120))
    assert context.metadata["game_license"] == {"installed": []}
    assert context.metadata["game_lobby"]["game_settings"]["time_limit_seconds"] == 120


async def test_the_real_game_lacks_its_activation_files_until_add_license_gives_them(
        env_vars, tmp_path, monkeypatch, local_stores):
    real = WC3Env()
    real.fake, real.game_dir, real.license_mount = False, tmp_path / "game", tmp_path / "no-mount"
    real.license_store = tmp_path / "store"
    (tmp_path / "game").mkdir()
    step = AddLicenseTaskStep(id="license", version=None, env_id="wc3", files=LICENSE_SECRETS)
    async with deployed(real) as record:
        url, card = record.environment_url, record.environment_card
        missing = (await client.invoke_extension(url, card, LICENSE, method="get"))["missing"]
        assert [(i["name"], i["kind"], i["group"]) for i in missing] == [("roc.w3k", "file", "warcraft3"),
                                                                        ("tft.w3k", "file", "warcraft3")]
        context = await OpenLobbyTaskStep(id="match", version=None, env_id="wc3").execute(run_context(record))
        with pytest.raises(RuntimeError, match="lobby close: not_licensed: the game lacks file roc.w3k .*import"):
            await fill_and_close(context)   # without add_license, the lobby doesn't close
        with pytest.raises(RuntimeError, match="secret store has no 'WC3_ROC_W3K' \\(file roc.w3k"):
            await step.execute(context)
        monkeypatch.setenv("WC3_ROC_W3K", base64.b64encode(b"roc key").decode())
        monkeypatch.setenv("WC3_TFT_W3K", base64.b64encode(b"tft key").decode())
        await step.execute(context)
        assert (await client.invoke_extension(url, card, LICENSE, method="get"))["missing"] == []
    assert context.metadata["game_license"] == {"installed": ["roc.w3k", "tft.w3k"]}
    assert (tmp_path / "game" / "tft.w3k").is_symlink() and (tmp_path / "game" / "tft.w3k").read_bytes() == b"tft key"


def test_read_license(license_dir):
    assert read_license(license_dir) == {"roc.w3k": base64.b64encode(b"roc key").decode(),
                                         "tft.w3k": base64.b64encode(b"tft key").decode()}
    (license_dir / "tft.w3k").write_bytes(b"")
    assert read_license(license_dir) is None


def test_the_license_comes_from_the_secret_store_first(license_dir, local_stores, tmp_path):
    from agentenv_wc3.cli import wc3

    secrets = tmp_path / "secrets.yaml"
    secrets.write_text("OTHER: keep\n")
    config = Path(os.environ["AGENT_ENV_CONFIG"])
    config.write_text(config.read_text() + '\n[stores.secret]\nimpl = "agent_env.store.secret_store:LocalSecretStore"\n'
                      f'config = {{file_path = "{secrets}"}}\n')
    reset_config()
    assert license_from_secrets(LICENSE_SECRETS) is None
    result = CliRunner().invoke(wc3, ["license", "import"])   # from the license folder into the store's file
    assert result.exit_code == 0, result.output
    doc = yaml.safe_load(secrets.read_text())
    assert doc["OTHER"] == "keep" and base64.b64decode(doc["WC3_TFT_W3K"]) == b"tft key"
    assert secrets.stat().st_mode & 0o777 == 0o600 and "tft key" not in result.output
    reset_config()
    expected = read_license(license_dir)
    (license_dir / "roc.w3k").unlink()   # the folder no longer matters
    assert license_from_secrets(LICENSE_SECRETS) == expected
    assert "secret store" in CliRunner().invoke(wc3, ["license", "show"]).output
    secrets.write_text(yaml.safe_dump({"WC3_ROC_W3K": doc["WC3_ROC_W3K"]}))
    reset_config()
    with pytest.raises(RuntimeError, match="WC3_TFT_W3K missing"):
        license_from_secrets(LICENSE_SECRETS)
    secrets.write_text(yaml.safe_dump({"WC3_ROC_W3K": "not base64!", "WC3_TFT_W3K": doc["WC3_TFT_W3K"]}))
    reset_config()
    with pytest.raises(RuntimeError, match="not roc.w3k in base64"):
        license_from_secrets(LICENSE_SECRETS)


@environment_card(name="wc3")
class FakeReplay(AgentEnvEnvironment):
    @extension(REPLAY_EXTENSION, description="The replay.")
    async def replay(self) -> dict:
        return {"files": [{"name": "game.w3g", "content_type": "application/octet-stream",
                           "base64": base64.b64encode(b"W3G replay").decode()}]}


async def test_the_replay_becomes_a_file_artifact(local_stores):
    step = SaveWC3ReplayTaskStep(id="replay", version=None, env_id="wc3")
    async with deployed(FakeReplay()) as record:
        context = await step.execute(run_context(record))
    saved = context.metadata["replays"]["replay"]
    assert [(f["name"], f["artifact_id"], f["bytes"]) for f in saved] == [("game.w3g", "smoke-replay-i1.w3g", 10)]
    assert FileArtifact.get(saved[0]["artifact_id"], saved[0]["version"]).load() == b"W3G replay"


async def test_a_game_without_a_replay_saves_nothing_and_says_why(env_vars, license_dir, local_stores, caplog):
    env = WC3Env()
    async with deployed(env) as record:
        context = await fill_and_close(await open_match(record, time_limit_seconds=60))
        await FinishMatchTaskStep(id="finish", version=None, env_id="wc3").execute(context)
        context = await SaveWC3ReplayTaskStep(id="replay", version=None, env_id="wc3").execute(run_context(record))
    assert context.metadata["replays"]["replay"] == []
    assert any("save_wc3_replay: no replay" in m for m in caplog.messages)


@pytest.mark.parametrize("task", sorted(p.stem for p in (BUNDLE / "tasks").glob("*.json")))
def test_every_bundle_task_loads_is_graded_and_saves_its_replay_and_recording(local_stores, task):
    import json

    steps = json.loads((BUNDLE / "tasks" / f"{task}.json").read_text())
    registry = get_task_step_registry()
    for s in steps:
        assert registry[s["type"]].from_dict(s).to_dict()["id"] == s["id"]
    Task(id=task, version=None, steps=[registry[s["type"]].from_dict(s) for s in steps])   # a DAG, in order
    by_type = {s["type"]: s for s in steps}
    assert by_type["save_rts_recording"]["depends_on"] == by_type["save_wc3_replay"]["depends_on"]
    slots = [s for s in steps if s["type"] == "add_player_slot"]   # who plays: the lobby's slots, then the game
    assert by_type["close_lobby"]["depends_on"] == [*(s["id"] for s in slots), "license"] and len(slots) >= 2
    playing = {s["player_name"] for s in slots if s["player_kind"] == "agent"}
    assert {s["agent_name"] for s in steps if s["type"] == "prompt_agent"} <= playing
    assert all(s["env_ids"] == [] for s in steps if s["type"] == "deploy_agent")
    assert all(not ({"match", "player slot"} & set(s["depends_on"])) for s in steps if s["type"] == "prompt_agent")
    grade = by_type["rts_grade"]
    expected = (("smoke", "smoke") if task == "smoke" else ("checks", "drill") if task.startswith("drill-")
                else ("dense", "duel") if task.startswith("duel") else ("dense", task) if task.startswith("broadcast")
                else ("melee", "wc3"))
    assert grade["id"] == "grade" and (grade["rubric"], grade["verifier_id"]) == expected
    assert by_type["finish_match"]["id"] in grade["depends_on"]   # every match is played out before it is graded
    plays = [s for s in steps if s["type"] == "prompt_agent"]
    if task != "smoke":
        assert sorted(by_type["finish_match"]["depends_on"]) == sorted(s["id"] for s in plays)
    if broadcast := by_type.get("rts_broadcast"):   # on air before the first move, saved once the match is over
        assert all(broadcast["id"] in s["depends_on"] for s in plays)
        assert by_type["save_rts_broadcast"]["depends_on"] == [by_type["finish_match"]["id"]]
    if task.startswith("macro-micro"):
        agent = by_type["deploy_agent"]
        assert agent["a2a_agent_id"] == "wc3-macro-micro" and agent["env_vars"]["WC3_MICRO_MODEL"]
        mode = by_type["open_lobby"]["game_settings"]["mode"]
        assert mode == ("realtime" if task.endswith("realtime") else "stepping")
