"""`rts_grade` on synthetic summaries (today's, without player slots, and one with them), against the scripts the
bundle's tasks were graded by before it, and as a step reading a served env's data/get."""

import pytest
from agent_env.task_step.registry import get_task_step_registry
from agent_env.task_step.task_steps.verifiers.scoring import ScoreAggregator, aggregate_score
from agentenv_protocol import AgentEnvEnvironment, DataPart, client, environment_card, get_data
from conftest import STEPS
from test_steps import deployed, run_context

from agentenv_rts.grade import RTSGradeTaskStep

pytestmark = pytest.mark.anyio


def wc3_verifier(s: dict) -> list[dict]:
    """wc3-verifier's grade(), as the five agent tasks ran it before rts_grade (its agent_played without the idle
    extension, which finish_match replaced): the reference melee must match."""
    mine, theirs = (s.get("score") or {}).get("total", 0), (s.get("opponent_score") or {}).get("total", 0)
    harness = s.get("harness") or {}
    return [
        {"criterion": "the game reached its end: a result, or the time limit", "result": bool(s.get("game_over"))},
        {"criterion": "won: every enemy building destroyed", "weight": 3, "result": s.get("result") == "victory"},
        {"criterion": "not defeated", "result": s.get("result") != "defeat"},
        {"criterion": "outscored the AI on the game's score total", "result": mine > theirs,
         "score": min(1.0, mine / theirs) if theirs else float(mine > 0)},
        {"criterion": "the game kept running", "weight": -100, "result": not s.get("engine_failed")},
        {"criterion": "the agent played: it gave orders", "weight": -100, "result": harness.get("orders_sent", 0) > 0},
    ]


def smoke_verifier(s: dict) -> list[dict]:
    """smoke-verifier's grade(), as the smoke task ran it before rts_grade, played out by finish_match where the idle
    extension played it then: the reference smoke must match."""
    harness = s.get("harness") or {}
    return [
        {"criterion": "the game ran to its time limit", "result": s.get("result") == "time_limit"},
        {"criterion": "finish_match played it out", "result": harness.get("finish_seconds", 0) > 0},
        {"criterion": "the game kept running", "weight": -100, "result": not s.get("engine_failed")},
    ]


TODAY = {"game_over": True, "result": "time_limit", "game_time_seconds": 300.0, "time_limit_seconds": 300,
         "ai_difficulty": "easy", "score": {"total": 900}, "opponent_score": {"total": 1800}, "engine_failed": False,
         "error": None, "harness": {"orders_sent": 40, "finish_seconds": 0}}
TODAYS = {
    "victory": {**TODAY, "result": "victory"},
    "defeat": {**TODAY, "result": "defeat"},
    "behind at the limit": TODAY,
    "ahead at the limit": {**TODAY, "score": {"total": 2500}},
    "level at the limit": {**TODAY, "score": {"total": 1800}},
    "an AI with no score": {**TODAY, "opponent_score": {}},
    "still running": {**TODAY, "game_over": False, "result": None},
    "the engine failed": {**TODAY, "engine_failed": True, "error": "wine died"},
    "no orders": {**TODAY, "harness": {"orders_sent": 0, "finish_seconds": 0}},
    "played out": {**TODAY, "harness": {"orders_sent": 40, "finish_seconds": 120}},
}


def player(agent=None, computer=None, team=1, result="time_limit", orders=50, **metrics) -> dict:
    return {"player_id": "0", "player_kind": "ai" if computer else "agent", "player_name": agent, "faction": "human",
            "team": team, **({"ai_level": computer} if computer else {}), "result": result,
            "orders_sent": orders, "stalls": 0, "metrics": metrics}


def game(*players, **fields) -> dict:
    return {"game_over": True, "game_time_seconds": 600, "time_limit_seconds": 600, "engine_failed": False,
            "error": None, "harness": {"orders_sent": 0, "finish_seconds": 0, "staged_seconds": 0},
            "player_slots": list(players), **fields}


ALICE = {"total": 3000, "army": 2000, "units_killed": 30, "units_lost": 10, "buildings_destroyed": 2, "tier": 2,
         "expansions": 1, "hero_level": 4}
BOB = {"total": 2000, "army": 1000, "units_killed": 10, "units_lost": 30, "buildings_destroyed": 0, "tier": 3,
       "expansions": 0, "hero_level": 5, "structures": 6}
DUEL = game(player("alice", **ALICE), player("bob", team=2, **BOB))


def step(**fields) -> RTSGradeTaskStep:
    return RTSGradeTaskStep(**{"id": "grade", "version": None, "env_id": "wc3", **fields})


def score(rows: list[dict]) -> float:
    return aggregate_score(rows, ScoreAggregator.WEIGHTED_AVERAGE)


def by_name(rows: list[dict]) -> dict[str, dict]:
    return {r["name"]: r for r in rows}


def judged(rows: list[dict]) -> list[tuple]:
    return [(r["criterion"], r["result"], r.get("weight", 1), r.get("score")) for r in rows]


@pytest.mark.parametrize("case", TODAYS)
def test_melee_is_wc3_verifier_and_smoke_is_smoke_verifier(case):
    summary = TODAYS[case]
    [melee] = step(at_time_limit="draw").grade(summary).values()
    assert judged(melee) == judged(wc3_verifier(summary)) and score(melee) == score(wc3_verifier(summary))
    [smoke] = step(rubric="smoke").grade(summary).values()
    assert judged(smoke) == judged(smoke_verifier(summary)) and score(smoke) == score(smoke_verifier(summary))
    if summary["result"] != "time_limit":
        [default] = step().grade(summary).values()
        assert score(default) == score(wc3_verifier(summary))


def test_todays_half_grades_stay_half():
    assert score(step(at_time_limit="draw").grade(TODAYS["level at the limit"])["grade"]) == 0.5
    assert score(step(at_time_limit="draw").grade(TODAYS["ahead at the limit"])["grade"]) == 0.5


def test_a_win_counts_most():
    grades = {case: score(step().grade(TODAYS[case])["grade"]) for case in ("victory", "behind at the limit",
                                                                              "defeat")}
    assert grades["victory"] > grades["behind at the limit"] > grades["defeat"] > 0
    outscored = by_name(step().grade(TODAY)["grade"])["outscore"]
    assert outscored["score"] == 0.5 and outscored["result"] is False and outscored["evidence"] == "score 900 vs 1800"


@pytest.mark.parametrize(("at_time_limit", "win", "survive", "grade"), [
    ("score", True, True, 1.0), ("draw", False, True, 0.5), ("loss", False, False, 2 / 6)])
def test_the_time_limit_is_the_higher_score_a_draw_or_a_loss(at_time_limit, win, survive, grade):
    rows = step(at_time_limit=at_time_limit).grade(TODAYS["ahead at the limit"])["grade"]
    assert (by_name(rows)["win"]["result"], by_name(rows)["survive"]["result"]) == (win, survive)
    assert score(rows) == pytest.approx(grade)
    defeat = by_name(step(at_time_limit=at_time_limit).grade(TODAYS["defeat"])["grade"])
    assert not defeat["win"]["result"] and not defeat["survive"]["result"]


def test_a_level_score_at_the_limit_is_no_win():
    assert not by_name(step().grade(TODAYS["level at the limit"])["grade"])["win"]["result"]


def test_dense_grades_every_agent_player_slot_against_the_other_team():
    grades = step(rubric="dense", verifier_id="wc3", at_time_limit="score").grade(DUEL)
    assert set(grades) == {"wc3:alice", "wc3:bob"}
    alice, bob = by_name(grades["wc3:alice"]), by_name(grades["wc3:bob"])
    assert list(alice) == ["reached_end", "win", "survive", "outscore", "army_ratio", "kills_ratio",
                           "buildings_destroyed", "tier", "expansions", "hero_level", "game_ran", "agent_played"]
    assert {k: r.get("score", float(r["result"])) for k, r in alice.items()} == pytest.approx({
        "reached_end": 1, "win": 1, "survive": 1, "outscore": 1, "army_ratio": 1, "kills_ratio": 0.75,
        "buildings_destroyed": 0.25, "tier": 2 / 3, "expansions": 1, "hero_level": 0.8, "game_ran": 1,
        "agent_played": 1})
    assert alice["army_ratio"]["evidence"] == "army 2000 vs 1000"
    assert alice["buildings_destroyed"]["evidence"] == "destroyed 2 of 8"
    assert alice["outscore"]["criterion"] == "outscored every opponent on the game's score total"
    assert bob["win"]["result"] is False and bob["outscore"]["score"] == pytest.approx(2 / 3)
    assert bob["buildings_destroyed"]["evidence"] == "destroyed 0, full credit at 5"
    assert score(grades["wc3:alice"]) == pytest.approx((1 + 3 + 1 + 1 + 1 + 0.75 + 0.25 + 2 / 3 + 1 + 0.8) / 12)
    assert score(grades["wc3:alice"]) > score(grades["wc3:bob"]) > 0


def test_allies_are_not_opponents_and_computer_player_slots_are_not_graded():
    summary = game(player("alice", total=1000), player("bob", total=5000), player(computer="easy", team=2, total=800),
                   player(computer="easy", team=2, total=900))
    grades = step(verifier_id="wc3", at_time_limit="score").grade(summary)
    assert set(grades) == {"wc3:alice", "wc3:bob"}
    alice = by_name(grades["wc3:alice"])
    assert alice["outscore"]["evidence"] == "score 1000 vs 900" and alice["win"]["result"] is True
    assert alice["outscore"]["criterion"] == "outscored the AI on the game's score total"


def test_named_player_slots_are_graded_and_a_missing_one_scores_nothing():
    grades = step(player_names=["bob", "zed"]).grade(DUEL)
    assert set(grades) == {"grade:bob", "grade:zed"}
    assert score(grades["grade:zed"]) == 0 and grades["grade:zed"][0]["evidence"] == "player slots ['alice', 'bob']"


def test_a_summary_without_player_slots_is_one_player_slot_under_the_verifier_id():
    assert set(step(verifier_id="wc3").grade(TODAY)) == {"wc3"}
    assert set(step().grade(TODAY)) == {"grade"}


def test_weights_drop_add_and_reweigh_criteria_and_targets_set_full_credit():
    rows = by_name(step(weights={"win": 0, "survive": 2, "army_ratio": 1}).grade(DUEL)["grade:bob"])
    assert list(rows) == ["reached_end", "survive", "outscore", "army_ratio", "game_ran", "agent_played"]
    assert rows["survive"]["weight"] == 2 and rows["army_ratio"]["score"] == 0.5
    targets = {"army_ratio": 2.0, "hero_level": 2, "tier": 2, "expansions": 2, "buildings_destroyed": 1}
    alice = by_name(step(rubric="dense", targets=targets).grade(DUEL)["grade:alice"])
    assert alice["army_ratio"]["score"] == 1
    assert alice["army_ratio"]["criterion"] == "an army at least 2x the strongest enemy's"
    assert (alice["hero_level"]["result"], alice["tier"]["result"], alice["expansions"]["score"]) == (True, True, 0.5)
    bob = by_name(step(rubric="dense", targets=targets).grade(DUEL)["grade:bob"])
    assert bob["army_ratio"]["score"] == 0.25 and bob["buildings_destroyed"]["score"] == 0


def test_dense_gives_no_credit_for_what_the_summary_doesnt_report():
    rows = by_name(step(rubric="dense").grade(TODAY)["grade"])
    assert rows["army_ratio"] == {"name": "army_ratio", "criterion": "an army at least 1x the strongest enemy's",
                                  "result": False, "score": 0.0, "evidence": "army not reported", "weight": 1}
    assert all(rows[k]["score"] == 0 for k in ("kills_ratio", "buildings_destroyed", "tier", "expansions",
                                                "hero_level"))


CHECKS = [
    {"metric": "count:hhou", "op": ">=", "value": 2},
    {"metric": "count:hbar", "op": ">=", "value": 1},
    {"metric": "first_time:halt", "op": "<=", "value": 120},
    {"metric": "first_time:hbla", "op": "<=", "value": 130},
    {"metric": "present_seconds:hmil", "op": ">=", "value": 10},
    {"metric": "present_seconds:hfoo", "op": "==", "value": 0},
    {"metric": "camp_cleared:camp:9", "op": "==", "value": True},
    {"metric": "camp_cleared:camp:4", "op": "==", "value": True},
    {"metric": "hero_alive", "op": "==", "value": True},
    {"metric": "units_lost", "op": "<=", "value": 4, "weight": 2},
    {"metric": "enemy_army_destroyed_percent", "op": ">=", "value": 100},
    {"metric": "army_kept_percent"},
]


def test_checks_resolve_wc3agents_metric_names():
    drill = game(player("wc3", count={"hhou": 3}, first_time={"halt": 95}, present_seconds={"hmil": 12},
                      camps_cleared=[3, 9], units_lost=2, hero_alive=True, army_kept_percent=60),
                 player("attacker", team=2))
    rows = step(rubric="checks", player_names=["wc3"], checks=CHECKS).grade(drill)["grade:wc3"]
    assert [(r["criterion"], r["result"]) for r in rows[:len(CHECKS)]] == [
        ("count:hhou >= 2", True), ("count:hbar >= 1", False), ("first_time:halt <= 120", True),
        ("first_time:hbla <= 130", False), ("present_seconds:hmil >= 10", True), ("present_seconds:hfoo == 0", True),
        ("camp_cleared:camp:9 == True", True), ("camp_cleared:camp:4 == True", False), ("hero_alive == True", True),
        ("units_lost <= 4", True), ("enemy_army_destroyed_percent >= 100", False), ("army_kept_percent", None)]
    assert rows[len(CHECKS) - 1]["weight"] == 0 and rows[len(CHECKS) - 1]["evidence"] == "measured 60"
    assert [r["name"] for r in rows[len(CHECKS):]] == ["game_ran", "agent_played"]
    assert score(rows) == pytest.approx(8 / 12)


def test_checks_add_to_any_rubric_and_fail_on_what_the_summary_lacks():
    rows = by_name(step(checks=[{"metric": "count:hhou", "op": ">=", "value": 0}]).grade(TODAY)["grade"])
    assert rows["count:hhou"]["result"] is False and "win" in rows


def test_a_failed_gate_zeroes_the_player_slot_and_gates_can_be_left_out():
    beaten = game(player("alice", result="victory", orders=0, **ALICE), player("bob", team=2, result="defeat", **BOB))
    assert score(step().grade(beaten)["grade:alice"]) == 0 and score(step().grade(beaten)["grade:bob"]) > 0
    assert score(step(gates=["game_ran"]).grade(beaten)["grade:alice"]) == 1
    assert score(step(gates=[]).grade({**beaten, "engine_failed": True})["grade:alice"]) == 1
    assert score(step(gates=["game_ran"]).grade({**beaten, "engine_failed": True})["grade:alice"]) == 0
    silent = game(player("alice", orders=0, **ALICE), player("bob", team=2, orders=0, **BOB))
    assert all(score(rows) == 0 for rows in step().grade(silent).values())


def test_smoke_never_asks_that_the_agent_played():
    played_by_the_harness = {**TODAY, "harness": {"orders_sent": 0, "finish_seconds": 300}}
    rows = step(rubric="smoke", gates=["game_ran", "agent_played"]).grade(played_by_the_harness)["grade"]
    assert [r["name"] for r in rows] == ["ran_to_limit", "played_out", "game_ran"] and score(rows) == 1


@pytest.mark.parametrize(("fields", "message"), [
    ({"rubric": "blitz"}, "rubric must be one of"),
    ({"at_time_limit": "overtime"}, "at_time_limit must be one of"),
    ({"gates": ["game_ran", "fun"]}, "gates must be among"),
    ({"player_names": "alice"}, "player_names are agent names"),
    ({"player_names": [""]}, "player_names are agent names"),
    ({"weights": {"speed": 1}}, "weights are for the criteria"),
    ({"weights": {"win": "3"}}, "weights must be numbers"),
    ({"weights": {"win": True}}, "weights must be numbers"),
    ({"targets": {"win": 1}}, "targets are for"),
    ({"targets": {"tier": 0}}, "targets must be positive"),
    ({"checks": [{"metric": "food_cap", "op": ">=", "value": 1}]}, "is not one the summary reports"),
    ({"checks": [{"metric": "count", "op": ">=", "value": 1}]}, "is not one the summary reports"),
    ({"checks": [{"metric": "camp_cleared:nearest_camp"}]}, "is not one the summary reports"),
    ({"checks": [{"metric": "tier", "op": ">="}]}, "needs an op"),
    ({"checks": [{"metric": "tier", "op": "!=", "value": 1}]}, "needs an op"),
    ({"checks": [{"metric": "tier", "op": ">=", "value": 1, "weight": "1"}]}, "weight must be a number"),
    ({"checks": [{"metric": "tier", "goal": 3}]}, "checks are"),
    ({"checks": [{"op": ">=", "value": 3}]}, "checks are"),
    ({"rubric": "checks"}, "needs checks"),
])
def test_bad_fields_are_refused_when_the_step_is_made(fields, message):
    with pytest.raises(ValueError, match=message):
        step(**fields)


def test_the_step_round_trips_and_is_registered(local_stores):
    data = {"id": "grade", "type": "rts_grade", "version": None, "env_id": "wc3", "player_names": ["alice"],
            "rubric": "checks", "weights": {"win": 2}, "targets": {"tier": 2},
            "checks": [{"metric": "tier", "op": ">=", "value": 2}], "at_time_limit": "loss", "gates": ["game_ran"],
            "verifier_id": "v", "timeout_seconds": 60, "fail_task_on_error": False,
            "depends_on": [{"task_step_id": "play"}]}
    assert STEPS["rts_grade"] == "agentenv_rts.grade:RTSGradeTaskStep"
    registered = get_task_step_registry()["rts_grade"]
    assert registered is RTSGradeTaskStep and registered.from_dict(data).to_dict() == data
    defaults = RTSGradeTaskStep.from_dict({"id": "grade", "type": "rts_grade", "env_id": "wc3"}).to_dict()
    assert {k: defaults[k] for k in ("player_names", "rubric", "at_time_limit", "gates", "verifier_id",
                                     "fail_task_on_error")} == {
        "player_names": None, "rubric": "melee", "at_time_limit": "draw", "gates": ["game_ran", "agent_played"],
        "verifier_id": "grade", "fail_task_on_error": True}


@environment_card(name="wc3")
class FakeSummary(AgentEnvEnvironment):
    def __init__(self, summary: dict):
        super().__init__()
        self.summary = summary

    @get_data
    async def summary_data(self) -> list[DataPart]:
        return [DataPart(data=self.summary)]


async def test_the_step_grades_what_data_get_reports_with_the_runs_overrides():
    grade = step(verifier_id="wc3")
    async with deployed(FakeSummary(DUEL)) as record:
        context = await grade.execute(run_context(record))
        overridden = run_context(record)
        overridden.metadata["user_overrides"] = {"step_params": {"grade": {"rubric": "dense", "player_names": ["bob"]}}}
        overridden = await grade.execute(overridden)
    verifications = context.metadata["verifications"]
    assert set(verifications) == {"wc3:alice", "wc3:bob"}
    assert verifications["wc3:alice"] == {"results": grade.grade(DUEL)["wc3:alice"],
                                          "score": score(grade.grade(DUEL)["wc3:alice"])}
    assert set(overridden.metadata["verifications"]) == {"wc3:bob"}
    assert "army_ratio" in by_name(overridden.metadata["verifications"]["wc3:bob"]["results"])
    assert grade.rubric == "melee" and grade.player_names is None


async def test_todays_env_is_graded_under_the_verifier_id_alone():
    async with deployed(FakeSummary(TODAYS["victory"])) as record:
        context = await step(verifier_id="wc3", at_time_limit="draw").execute(run_context(record))
    rows = step(at_time_limit="draw").grade(TODAYS["victory"])["grade"]
    assert context.metadata["verifications"] == {"wc3": {"results": rows, "score": score(wc3_verifier(
        TODAYS["victory"]))}}


async def test_an_env_that_does_not_answer_data_get_scores_nothing(monkeypatch):
    async def refuse(base_url, timeout=30, verify=True):
        raise ConnectionError("refused")

    monkeypatch.setattr(client, "get_data", refuse)
    async with deployed(FakeSummary(DUEL)) as record:
        context = await step(verifier_id="wc3").execute(run_context(record))
        named = await step(verifier_id="wc3", player_names=["alice", "bob"]).execute(run_context(record))
    [row] = context.metadata["verifications"]["wc3"]["results"]
    assert row["weight"] == -100 and row["result"] is False and "refused" in row["evidence"]
    assert context.metadata["verifications"]["wc3"]["score"] == 0
    assert set(named.metadata["verifications"]) == {"wc3:alice", "wc3:bob"}
    with pytest.raises(RuntimeError, match="not deployed"):
        await step(env_id="elsewhere").execute(run_context(record))
