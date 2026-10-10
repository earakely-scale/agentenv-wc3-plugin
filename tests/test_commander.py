"""The commander style: wc3agent's interface as MCP tools (commander.py), on wc3env's sample of a real observation;
the entry that picks a style; setup building a style's image; sweeps generating a style's or another agent's tasks."""

import json
from pathlib import Path

import pytest
import wc3env
from agentenv_protocol.agent_env_environment import _discover_tools
from click.testing import CliRunner
from wc3agent.game.catalog import Catalog
from wc3agent.game.mapinfo import MapInfo
from wc3env.data import MAPS, REFERENCE

from agentenv_wc3 import cli, styles, sweep
from agentenv_wc3.commander import Commander, CommanderEnv, page_text
from agentenv_wc3.server import WC3Env

SAMPLE = Path(wc3env.__file__).resolve().parents[2] / "docs" / "examples" / "observation.json"


@pytest.fixture
def obs():
    return json.loads(SAMPLE.read_text())


@pytest.fixture
def commander(obs):
    c = Commander(Catalog.load(REFERENCE), MapInfo.load(MAPS / "(2)EchoIsles.json"), 0, "human")
    c.see(obs)
    return c


def test_a_commander_orders_by_name_through_its_own_tools_not_the_raw_styles():
    tools = sorted(d.name for d, _ in _discover_tools(CommanderEnv(), "wc3"))
    assert tools == ["advance", "choose", "command", "fight", "get_state", "guide", "lookup"]
    assert {"act", "list_units", "resources"} <= {d.name for d, _ in _discover_tools(WC3Env(), "wc3")}


def test_the_page_names_units_says_what_is_not_yet_possible_and_tells_feedback_once(commander, obs):
    guide = commander.guide(obs)
    assert "train townhall1 Peasant" in guide and "IN THIS ENV THERE IS NO MICRO MODEL" in guide
    page = commander.page(obs)
    assert "IDLE: peasant5, peasant4, peasant3, peasant2, peasant1" in page and "NOT YET: train Peasant" in page
    commander.command("build footman9 Castle", obs)
    assert "unknown entity name 'footman9'" in commander.page(obs)
    assert "footman9" not in commander.page(obs)


def test_orders_are_parsed_at_once_and_sent_with_the_tick_with_skill_points_spent(commander, obs):
    actions, notes = commander.command("Plan: grow.\nbuild peasant1 Farm\ngold peasant2\ntrain townhall1 Peasant", obs)
    assert [a["command"] for a in actions] == ["build", "harvest", "train"] and notes == []
    sent, turns = commander.tick(obs)
    assert [a["command"] for a in sent] == ["build", "harvest", "train", "learn"] and turns == [1, 1, 1, None]
    assert commander.tick(obs) == ([], [])


def test_a_groups_fight_is_a_numbered_menu_per_unit_and_picks_become_its_actions(commander, obs):
    commander.command("group army footman1 footman2 archmage1 attack at 4000 2000: clear camp 1", obs)
    commander.tick(obs)
    menu = commander.fight("army", obs)
    assert "FIGHT: group army, objective: clear camp 1" in menu
    assert "  4. Use Potion of Healing in slot 1:" in menu and "  2. Attack-move to army's destination:" in menu
    actions = commander.choose("army", {"archmage1": 4, "footman1": 1}, obs)
    assert [(a["command"], a["arguments"].get("slot")) for a in actions] == [("use_item", 0)]
    assert "army" in commander.macro_memory.fighting
    with pytest.raises(ValueError, match="call fight first"):
        commander.choose("army", {"footman1": 2}, obs)
    commander.fight("army", obs)
    with pytest.raises(ValueError, match="options 1 to 3"):
        commander.choose("army", {"footman1": 9}, obs)
    commander.tick(obs)
    with pytest.raises(ValueError, match="call fight first"):
        commander.choose("army", {"footman1": 2}, obs)
    with pytest.raises(ValueError, match="no group 'navy'"):
        commander.fight("navy", obs)


def test_the_page_says_code_and_attack_move_fight_for_groups_not_a_micro_model():
    text = page_text("GROUPS (micro owns these units until a direct order or reassignment)\n  army: ..., awaiting "
                     "micro reply; told: hold")
    assert "micro" not in text and "on attack-move" in text


def test_the_entry_serves_the_style_its_image_was_built_for(monkeypatch):
    served = []
    for style, cls in (("commander", CommanderEnv), ("", WC3Env)):
        monkeypatch.setenv("WC3_STYLE", style)
        monkeypatch.setattr(cls, "serve", lambda self: served.append(type(self)))
        styles.main()
    assert served == [CommanderEnv, WC3Env]
    monkeypatch.setenv("WC3_STYLE", "zerg")
    with pytest.raises(SystemExit, match="WC3_STYLE must be one of raw, commander"):
        styles.main()


def test_setup_builds_a_styles_image_and_registers_it_as_its_own_env(monkeypatch):
    builds, registered = [], []
    monkeypatch.setattr(cli, "_image_exists", lambda image: True)
    monkeypatch.setattr(cli, "_game_env", lambda root: "https://example.invalid/game-env.tar.gz")
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, **kw: builds.append(cmd) or type("Done", (), {
        "returncode": 0}))
    monkeypatch.setattr("agent_env.artifact.DockerImageArtifact.put", lambda **kw: type("A", (), {
        "image_name": kw["image_name"]})())
    monkeypatch.setattr("agent_env.env.MCPServerEnv.put", lambda **kw: registered.append(kw["id"]) or type(
        "Env", (), {"id": kw["id"], "version": 1})())
    root = Path(__file__).resolve().parents[1]
    result = CliRunner().invoke(cli.wc3, ["setup", "--style", "commander", "--source", str(root)])
    assert result.exit_code == 0, result.output
    assert "STYLE=commander" in builds[0] and f"WC3ENV_COMMIT={cli.WC3ENV_COMMIT}" in builds[0]
    assert builds[0][builds[0].index("-t") + 1] == "mcp-server-wc3-commander" and registered == ["wc3-commander"]


def test_a_sweep_plays_its_tasks_in_a_style_or_with_an_agent_of_its_own(tmp_path):
    def generated(extra: str, template: str = '"drill-hero-in-danger"') -> tuple[list[dict], dict]:
        (tmp_path / "spec.toml").write_text(f'name = "s"\ntemplate = {template}\nmodels = ["m/x"]\nprompt = ""\n'
                                            f'{extra}\n')
        out = tmp_path / "out"
        name = sweep.generate(sweep.Spec.load(tmp_path / "spec.toml"), out)[0]
        steps = json.loads((out / "tasks" / f"{name}.json").read_text())
        return steps, json.loads((out / "sweep.json").read_text())

    steps, manifest = generated('style = "commander"')
    play = next(s for s in steps if s["id"] == "play")
    assert {s["env_id"] for s in steps if "env_id" in s} == {"wc3-commander"} and manifest["style"] == "commander"
    assert "fight with the group's name" in play["prompt"] and "act queues orders" not in play["prompt"]
    steps, manifest = generated('agent = {id = "wc3-macro-micro", env = {WC3_MICRO_MODEL = "anthropic/h"}}')
    agent = next(s for s in steps if s["type"] == "deploy_agent" and s["agent_name"] == "wc3")
    play = next(s for s in steps if s["id"] == "play")
    assert agent["a2a_agent_id"] == "wc3-macro-micro" and agent["env_vars"]["WC3_GOAL"] == "prompt"
    assert agent["env_vars"]["WC3_MICRO_MODEL"] == "anthropic/h" and "get_state" not in play["prompt"]
    steps, _ = generated('agent = {id = "wc3-macro-micro"}\nmaps = ["(2)EchoIsles.w3x"]', '"vs-ai"')
    assert "WC3_GOAL" not in next(s for s in steps if s["type"] == "deploy_agent")["env_vars"]
    for extra, problem in (('style = "zerg"', "style must be one of"), ('agent = {name = "x"}', "an agent is"),
                           ('style = "commander"\nagent = {id = "x"}', "leave style out")):
        (tmp_path / "spec.toml").write_text(f'name = "s"\ntemplate = "vs-ai"\nmodels = ["m/x"]\n{extra}\n')
        with pytest.raises(ValueError, match=problem):
            sweep.Spec.load(tmp_path / "spec.toml")
