"""The task steps against the env served over HTTP (on wc3env's fake game), with agent-env's local stores."""

import asyncio
import base64
import socket
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import uvicorn
from agent_env.artifact import FileArtifact
from agent_env.env.env import DeployedEnv
from agent_env.task_step.context import TaskStepContext
from agent_env.task_step.registry import get_task_step_registry
from agentenv_protocol import AgentEnvEnvironment, client, environment_card, extension
from agentenv_protocol.types import WELL_KNOWN_PATH

from agentenv_rts.grade import RTSGradeTaskStep
from agentenv_wc3.server import IDLE_EXTENSION, WC3Env
from agentenv_wc3.steps import REPLAY_EXTENSION, SaveWC3ReplayTaskStep, WC3MatchTaskStep, read_license

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


def test_the_steps_are_registered():
    registry = get_task_step_registry()
    assert registry["wc3_match"] is WC3MatchTaskStep and registry["save_wc3_replay"] is SaveWC3ReplayTaskStep
    step = WC3MatchTaskStep.from_dict({"id": "match", "type": "wc3_match", "env_id": "wc3", "ai_difficulty": "easy",
                                       "time_limit_seconds": 300})
    assert step.to_dict()["ai_difficulty"] == "easy" and step.to_dict()["map"] == "(2)EchoIsles.w3x"


async def test_match_then_idle_passes_the_smoke_rubric(env_vars, license_dir):
    env = WC3Env()
    step = WC3MatchTaskStep(id="match", version=None, env_id="wc3", seed=4, time_limit_seconds=90)
    async with deployed(env) as record:
        context = await step.execute(run_context(record))
        assert context.metadata["wc3_match"]["scenario"]["seed"] == 4
        assert context.metadata["wc3_match"]["scenario"]["time_limit_seconds"] == 90
        result = await client.invoke_extension(record.environment_url, record.environment_card, IDLE_EXTENSION,
                                               {"seconds": 300})
        assert result["played_seconds"] == 90 and result["result"] == "time_limit"
        smoke = await RTSGradeTaskStep(id="grade", version=None, env_id="wc3", rubric="smoke").execute(
            run_context(record))
        [verification] = smoke.metadata["verifications"].values()
        assert verification["score"] == 1 and all(r["result"] for r in verification["results"]), verification
        # The agents' rubric sees a game the harness played, and gives it nothing.
        melee = await RTSGradeTaskStep(id="grade", version=None, env_id="wc3").execute(run_context(record))
        [verification] = melee.metadata["verifications"].values()
        gate = next(r for r in verification["results"] if r["name"] == "agent_played")
        assert gate["result"] is False and verification["score"] == 0


async def test_a_match_without_activation_files_plays_the_fake_game_and_the_real_one_refuses(
        env_vars, tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("WC3_LICENSE_DIR", str(tmp_path / "nowhere"))
    step = WC3MatchTaskStep(id="match", version=None, env_id="wc3", time_limit_seconds=120)
    async with deployed(WC3Env()) as record:
        context = await step.execute(run_context(record))
    assert context.metadata["wc3_match"]["scenario"]["time_limit_seconds"] == 120
    assert context.metadata["wc3_match"]["live_url"].endswith("/live")
    assert "no Warcraft III activation files" in caplog.text
    real = WC3Env()
    real.fake, real.game_dir, real.license_mount = False, tmp_path / "game", tmp_path / "no-mount"
    (tmp_path / "game").mkdir()
    async with deployed(real) as record:
        with pytest.raises(Exception, match="activation files"):
            await step.execute(run_context(record))


def test_read_license(license_dir):
    assert read_license(license_dir) == {"roc.w3k": base64.b64encode(b"roc key").decode(),
                                         "tft.w3k": base64.b64encode(b"tft key").decode()}
    (license_dir / "tft.w3k").write_bytes(b"")
    assert read_license(license_dir) is None


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
        await WC3MatchTaskStep(id="match", version=None, env_id="wc3", time_limit_seconds=60).execute(
            run_context(record))
        await client.invoke_extension(record.environment_url, record.environment_card, IDLE_EXTENSION,
                                      {"seconds": 60})
        context = await SaveWC3ReplayTaskStep(id="replay", version=None, env_id="wc3").execute(run_context(record))
    assert context.metadata["replays"]["replay"] == []
    assert any("save_wc3_replay: no replay" in m for m in caplog.messages)


@pytest.mark.parametrize("task", sorted(p.stem for p in (BUNDLE / "tasks").glob("*.json")))
def test_every_bundle_task_loads_is_graded_and_saves_its_replay_and_recording(local_stores, task):
    import json

    steps = json.loads((BUNDLE / "tasks" / f"{task}.json").read_text())
    registry = get_task_step_registry()
    if task.startswith("drill-") and "rts_grade" not in registry:
        pytest.skip("drills grade with rts_grade, which is being built in parallel (feat/rts-grade) and is not "
                    "registered on this branch yet")
    for s in steps:
        assert registry[s["type"]].from_dict(s).to_dict()["id"] == s["id"]
    by_type = {s["type"]: s for s in steps}
    assert by_type["save_rts_recording"]["depends_on"] == by_type["save_wc3_replay"]["depends_on"]
    grade = by_type["rts_grade"]
    assert grade["id"] == "grade" and (grade["rubric"], grade["verifier_id"]) == (
        ("smoke", "smoke") if task == "smoke" else ("melee", "wc3"))
    if task.startswith("macro-micro"):
        agent = by_type["deploy_agent"]
        assert agent["a2a_agent_id"] == "wc3-macro-micro" and agent["env_vars"]["WC3_MICRO_MODEL"]
        assert by_type["wc3_match"]["mode"] == ("realtime" if task.endswith("realtime") else "stepping")
