"""The wc3-scripted agent (agents/wc3-scripted): its orders on synthetic observations, and its loop against a
stand-in session at its player slot."""

import importlib.util
import sys
from pathlib import Path

import pytest
from agentenv_protocol.a2a_agent import AgentConfig, TaskOutcome, TaskRequest, TextPart

pytestmark = pytest.mark.anyio
AGENT = Path(__file__).resolve().parents[1] / "agents/wc3-scripted/agent.py"


def scripted():
    spec = importlib.util.spec_from_file_location("wc3_scripted_agent", AGENT)
    module = sys.modules[spec.name] = importlib.util.module_from_spec(spec)   # its dataclass looks itself up there
    spec.loader.exec_module(module)
    return module


def unit(uid, type_id, x, y, structure=False):
    return {"unit_id": uid, "type_id": type_id, "x": x, "y": y, "structure": structure, "hp": 100}


MINE = [unit(10, "ogru", 0, 0), unit(11, "ohun", 10, 0), unit(12, "opeo", 0, 0), unit(13, "ogre", 0, 0, True)]
THEIRS = [unit(20, "hfoo", 100, 200), unit(21, "hrif", 300, 400), unit(22, "hpea", -1000, -1000),
          unit(23, "hpea", -3000, -1000), unit(24, "htow", -5000, 3000, True), unit(25, "hbar", 0, 3000, True)]
SLOTS = [{"player_id": "0", "player_kind": "agent", "player_name": "wc3", "faction": "human", "team": 1},
         {"player_id": "1", "player_kind": "agent", "player_name": "attacker", "faction": "orc", "team": 2}]


def state(t, mine=MINE, theirs=THEIRS, **extra):
    """What the attacker's omniscient player slot (player 1) observes at game time `t`."""
    return {"observations": {0: {"game_time_seconds": t, "units": theirs}, 1: {"game_time_seconds": t, "units": mine}},
            "player_id": "1", "player_slots": SLOTS, "done": False, "result": None, "scenario": {"mode": "stepping"},
            **extra}


def targets(orders):
    return {(o["arguments"]["x"], o["arguments"]["y"]) for o in orders}


def test_attack_moves_the_army_at_the_enemy_armys_centroid():
    orders = scripted().Script("attack").actions(state(0))
    assert [o["unit_id"] for o in orders] == [10, 11]
    assert {o["command"] for o in orders} == {"attack"} and targets(orders) == {(200, 300)}


def test_raid_goes_for_the_workers_else_the_first_building():
    agent = scripted()
    assert targets(agent.Script("raid").actions(state(0))) == {(-2000, -1000)}
    no_workers = [u for u in THEIRS if u["type_id"] != "hpea"]
    assert targets(agent.Script("raid").actions(state(0, theirs=no_workers))) == {(-5000, 3000)}
    assert agent.Script("raid").actions(state(0, theirs=[])) == []
    assert agent.Script("attack").actions(state(0, theirs=no_workers[2:])) == []


def test_idle_gives_no_orders():
    script = scripted().Script("idle")
    assert all(script.actions(state(t)) == [] for t in range(30))


def test_it_waits_after_its_first_observation_then_renews_every_few_seconds():
    script = scripted().Script("attack", after=10, every=5)
    sent = [t for t in range(100, 131) if script.actions(state(t))]
    assert sent == [110, 115, 120, 125, 130]
    assert (script.rounds, script.orders) == (5, 10)


def test_an_order_round_moves_at_most_64_units():
    many = [unit(100 + i, "ogru", i, 0) for i in range(100)]
    orders = scripted().Script("attack").actions(state(0, mine=many))
    assert len(orders) == 64 and [o["unit_id"] for o in orders] == list(range(100, 164))


def test_the_enemy_is_every_player_slot_on_another_team():
    agent = scripted()
    ally = {"player_id": "2", "player_kind": "ai", "player_name": None, "faction": "orc", "team": 2, "ai_level": "easy"}
    three = state(0, player_slots=[*SLOTS, ally])
    three["observations"][2] = {"game_time_seconds": 0, "units": [unit(30, "ogru", 9000, 9000)]}
    assert targets(agent.Script("attack").actions(three)) == {(200, 300)}
    alone = {"observations": {0: {"game_time_seconds": 0, "units": MINE}, 1: {"game_time_seconds": 0, "units": THEIRS}}}
    assert [o["unit_id"] for o in agent.Script("attack").actions(alone)] == [10, 11]


def test_its_settings():
    agent = scripted()
    assert agent.Script.of({}) == agent.Script("attack", 0, 5)
    assert agent.Script.of({"SCRIPT": "raid", "SCRIPT_AFTER_SECONDS": "30", "SCRIPT_EVERY_SECONDS": "2.5"}) == \
        agent.Script("raid", 30, 2.5)
    with pytest.raises(ValueError, match="SCRIPT must be one of"):
        agent.Script.of({"SCRIPT": "rush"})


class Session:
    """A stand-in RemoteSession at the attacker's player slot: a second of game time a step, over at `seconds`."""

    def __init__(self, root, headers=None, mode="stepping", seconds=12):
        self.root, self.mode, self.seconds, self.t, self.steps = root, mode, seconds, 0, []

    def state(self):
        done = self.t >= self.seconds
        return state(self.t, done=done, result="time_limit" if done else None, scenario={"mode": self.mode})

    def observe(self):
        return self.state()

    def step(self, actions, ms=None):
        self.steps.append((actions, ms))
        self.t += ms // 1000
        return {**self.state(), "rejected": {1: []}, "placements": {1: []}, "elapsed_ms": ms}


@pytest.mark.parametrize("mode", ["stepping", "realtime"])
async def test_it_plays_its_player_slot_to_the_end(monkeypatch, mode):
    agent = scripted()
    sessions, sleeps, timeouts = [], [], []

    def connect(root, timeout, headers=None):
        timeouts.append(timeout)
        sessions.append(Session(root, mode=mode))
        return sessions[-1]

    monkeypatch.setattr(agent, "RemoteSession", connect)
    monkeypatch.setattr(agent.time, "sleep", sleeps.append)
    monkeypatch.setenv("SCRIPT", "attack")
    request = TaskRequest(task_id="t", context_id="c", parts=(TextPart(text="Play with the script."),),
                          config=AgentConfig(), mcp_servers={"wc3": {"url": "http://env:18765/players/1/mcp"}})
    result = await agent.WC3Scripted().run(request)
    [session] = sessions
    assert result.outcome is TaskOutcome.SUCCEEDED and session.root == "http://env:18765/players/1"
    assert timeouts == [agent.STEP_WAIT_SECONDS] and agent.STEP_WAIT_SECONDS > 600
    assert len(session.steps) == 12 and {ms for _, ms in session.steps} == {1000}
    assert all(set(actions) == {1} for actions, _ in session.steps)
    assert [i for i, (actions, _) in enumerate(session.steps) if actions[1]] == [0, 5, 10]
    assert result.parts[0].text == "attack: 6 orders in 3 rounds over 12 s of game time (time_limit)."
    assert result.parts[1].data == {"script": "attack", "orders": 6, "rounds": 3, "game_seconds": 12,
                                    "result": "time_limit"}
    assert sleeps == ([1.0] * 12 if mode == "realtime" else [])


async def test_a_bad_script_fails_the_run(monkeypatch):
    agent = scripted()
    monkeypatch.setenv("SCRIPT", "rush")
    request = TaskRequest(task_id="t", context_id="c", parts=(), config=AgentConfig(),
                          mcp_servers={"wc3": {"url": "http://env:18765/players/1/mcp"}})
    result = await agent.WC3Scripted().run(request)
    assert result.outcome is TaskOutcome.FAILED and result.error.code == "bad_script"
