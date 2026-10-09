"""`rts_grade` on synthetic summaries (today's, without player slots, and one with them), the smoke rubric against the
script the smoke task was graded by before it, and as a step reading a served env's data/get."""

import pytest
from agent_env.task_step.registry import get_task_step_registry
from agent_env.task_step.task_steps.verifiers.scoring import ScoreAggregator, aggregate_score
from agentenv_protocol import AgentEnvEnvironment, DataPart, client, environment_card, get_data
from conftest import STEPS
from test_steps import deployed, run_context

from agentenv_rts.grade import RTSGradeTaskStep, VoidMatch

pytestmark = pytest.mark.anyio


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
    "the game's own draw": {**TODAY, "result": "draw"},
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


@pytest.mark.parametrize("case", [c for c in TODAYS if TODAYS[c]["result"]])
def test_smoke_is_smoke_verifier(case):
    summary = TODAYS[case]
    [smoke] = step(rubric="smoke").grade(summary).values()
    assert judged(smoke) == judged(smoke_verifier(summary)) and score(smoke) == score(smoke_verifier(summary))


@pytest.mark.parametrize(("case", "points", "evidence"), [
    ("victory", 1, "won: every enemy defeated, or the staged fight decided"), ("defeat", 0, "lost: defeated"),
    ("the game's own draw", 0.5, "a draw: the game's own tie"),
    ("behind at the limit", 0.5, "a draw: nobody won by the time limit"),
    ("ahead at the limit", 0.5, "a draw: nobody won by the time limit"),
    ("level at the limit", 0.5, "a draw: nobody won by the time limit")])
def test_the_grade_is_the_outcome_a_win_a_draw_or_a_loss(case, points, evidence):
    rows = step().grade(TODAYS[case])["grade"]
    assert [r["name"] for r in rows] == ["outcome", "score", "army", "kills", "game_ran", "agent_played"]
    assert score(rows) == points and rows[0]["score"] == points and rows[0]["result"] is (points == 1)
    assert rows[0]["evidence"] == f"{evidence} at 300.0 of 300 game seconds"


def test_the_score_army_and_kills_are_evidence_not_grade():
    rows = by_name(step().grade(TODAYS["ahead at the limit"])["grade"])
    assert rows["score"] == {
        "name": "score", "criterion": "the game's score against the best enemy's (information only)", "result": None,
        "weight": 0, "evidence": "score 2500 vs 1800, 58% of the two"}
    assert by_name(step().grade(DUEL)["grade:alice"])["army"]["evidence"] == "army 2000 vs 1000"
    assert by_name(step().grade(DUEL)["grade:bob"])["kills"]["evidence"] == "killed 10, lost 30"


@pytest.mark.parametrize(("summary", "match", "why"), [
    (TODAYS["still running"], None, "the agent has no result at 300.0 game seconds"),
    (TODAYS["the engine failed"], None, "the game engine failed: wine died"),
    (TODAY, {"status": "cancelled", "status_detail": "the game could not play out"},
     "the match was cancelled: the game could not play out"),
    (TODAY, {"status": "failed"}, "the match failed")])
def test_a_match_without_an_outcome_is_void_not_a_loss(summary, match, why):
    for rubric in ("outcome", "dense"):
        with pytest.raises(VoidMatch, match=f"^grade: no outcome to grade, {why}$"):
            step(rubric=rubric).grade(summary, match)
    assert score(step(rubric="checks", checks=[{"metric": "tier"}]).grade(TODAYS["the engine failed"])["grade"]) == 0


def test_a_draw_without_orders_scores_nothing():
    assert score(step().grade(TODAYS["no orders"])["grade"]) == 0


def test_dense_grades_every_agent_player_slot_against_the_other_team():
    grades = step(rubric="dense", verifier_id="wc3").grade(DUEL)
    assert set(grades) == {"wc3:alice", "wc3:bob"}
    alice, bob = by_name(grades["wc3:alice"]), by_name(grades["wc3:bob"])
    assert list(alice) == ["outcome", "outscore", "army_ratio", "kills_ratio", "buildings_destroyed", "tier",
                           "expansions", "hero_level", "game_ran", "agent_played"]
    assert {k: r.get("score", float(r["result"])) for k, r in alice.items()} == pytest.approx({
        "outcome": 0.5, "outscore": 1, "army_ratio": 1, "kills_ratio": 0.75, "buildings_destroyed": 0.25,
        "tier": 2 / 3, "expansions": 1, "hero_level": 0.8, "game_ran": 1, "agent_played": 1})
    assert alice["army_ratio"]["evidence"] == "army 2000 vs 1000"
    assert alice["buildings_destroyed"]["evidence"] == "destroyed 2 of 8"
    assert alice["outscore"]["criterion"] == "outscored every opponent on the game's score total"
    assert bob["outcome"]["score"] == 0.5 and bob["outscore"]["score"] == pytest.approx(2 / 3)
    assert bob["buildings_destroyed"]["evidence"] == "destroyed 0, full credit at 5"
    assert score(grades["wc3:alice"]) == pytest.approx((0.5 + 1 + 1 + 0.75 + 0.25 + 2 / 3 + 1 + 0.8) / 8)
    assert score(grades["wc3:alice"]) > score(grades["wc3:bob"]) > 0


def test_allies_are_not_opponents_and_computer_player_slots_are_not_graded():
    summary = game(player("alice", total=1000), player("bob", total=5000), player(computer="easy", team=2, total=800),
                   player(computer="easy", team=2, total=900))
    grades = step(verifier_id="wc3").grade(summary)
    assert set(grades) == {"wc3:alice", "wc3:bob"}
    assert by_name(grades["wc3:alice"])["score"]["evidence"] == "score 1000 vs 900, 53% of the two"
    alice = by_name(step(rubric="dense", verifier_id="wc3").grade(summary)["wc3:alice"])
    assert alice["outscore"]["criterion"] == "outscored the AI on the game's score total"


def test_named_player_slots_are_graded_and_a_missing_one_scores_nothing():
    grades = step(player_names=["bob", "zed"]).grade(DUEL)
    assert set(grades) == {"grade:bob", "grade:zed"}
    assert score(grades["grade:zed"]) == 0 and grades["grade:zed"][0]["evidence"] == "player slots ['alice', 'bob']"


def test_a_summary_without_player_slots_is_one_player_slot_under_the_verifier_id():
    assert set(step(verifier_id="wc3").grade(TODAY)) == {"wc3"}
    assert set(step().grade(TODAY)) == {"grade"}


def test_weights_drop_add_and_reweigh_criteria_and_targets_set_full_credit():
    rows = by_name(step(rubric="dense", weights={"outscore": 0, "tier": 0, "expansions": 0, "hero_level": 0,
                                                  "buildings_destroyed": 0, "kills_ratio": 0, "outcome": 2})
                   .grade(DUEL)["grade:bob"])
    assert list(rows) == ["outcome", "army_ratio", "game_ran", "agent_played"]
    assert rows["outcome"]["weight"] == 2 and rows["army_ratio"]["score"] == 0.5
    added = by_name(step(weights={"tier": 1}).grade(DUEL)["grade:bob"])
    assert list(added) == ["outcome", "tier", "score", "army", "kills", "game_ran", "agent_played"]
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
    assert rows["count:hhou"]["result"] is False and "outcome" in rows


def test_a_failed_gate_zeroes_the_player_slot_and_gates_can_be_left_out():
    beaten = game(player("alice", result="victory", orders=0, **ALICE), player("bob", team=2, result="defeat", **BOB))
    assert score(step().grade(beaten)["grade:alice"]) == 0 and score(step().grade(beaten)["grade:bob"]) == 0
    assert score(step(gates=["game_ran"]).grade(beaten)["grade:alice"]) == 1
    tier = {"rubric": "checks", "checks": [{"metric": "tier", "op": ">=", "value": 1}]}
    assert score(step(**tier, gates=[]).grade({**beaten, "engine_failed": True})["grade:alice"]) == 1
    assert score(step(**tier, gates=["game_ran"]).grade({**beaten, "engine_failed": True})["grade:alice"]) == 0
    silent = game(player("alice", orders=0, **ALICE), player("bob", team=2, orders=0, **BOB))
    assert all(score(rows) == 0 for rows in step().grade(silent).values())


def test_smoke_never_asks_that_the_agent_played():
    played_by_the_harness = {**TODAY, "harness": {"orders_sent": 0, "finish_seconds": 300}}
    rows = step(rubric="smoke", gates=["game_ran", "agent_played"]).grade(played_by_the_harness)["grade"]
    assert [r["name"] for r in rows] == ["ran_to_limit", "played_out", "game_ran"] and score(rows) == 1


@pytest.mark.parametrize(("fields", "message"), [
    ({"rubric": "blitz"}, "rubric must be one of"),
    ({"gates": ["game_ran", "fun"]}, "gates must be among"),
    ({"player_names": "alice"}, "player_names are agent names"),
    ({"player_names": [""]}, "player_names are agent names"),
    ({"weights": {"speed": 1}}, "weights are for the criteria"),
    ({"weights": {"win": 3}}, "weights are for the criteria"),
    ({"weights": {"outcome": "3"}}, "weights must be numbers"),
    ({"weights": {"outcome": True}}, "weights must be numbers"),
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
            "rubric": "checks", "weights": {"outcome": 2}, "targets": {"tier": 2},
            "checks": [{"metric": "tier", "op": ">=", "value": 2}], "gates": ["game_ran"],
            "verifier_id": "v", "timeout_seconds": 60, "fail_task_on_error": False,
            "depends_on": [{"task_step_id": "play"}]}
    assert STEPS["rts_grade"] == "agentenv_rts.grade:RTSGradeTaskStep"
    registered = get_task_step_registry()["rts_grade"]
    assert registered is RTSGradeTaskStep and registered.from_dict(data).to_dict() == data
    defaults = RTSGradeTaskStep.from_dict({"id": "grade", "type": "rts_grade", "env_id": "wc3"}).to_dict()
    assert {k: defaults[k] for k in ("player_names", "rubric", "gates", "verifier_id", "fail_task_on_error")} == {
        "player_names": None, "rubric": "outcome", "gates": ["game_ran", "agent_played"], "verifier_id": "grade",
        "fail_task_on_error": True}


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
    assert grade.rubric == "outcome" and grade.player_names is None


async def test_todays_env_is_graded_under_the_verifier_id_alone():
    async with deployed(FakeSummary(TODAYS["victory"])) as record:
        context = await step(verifier_id="wc3").execute(run_context(record))
    rows = step().grade(TODAYS["victory"])["grade"]
    assert context.metadata["verifications"] == {"wc3": {"results": rows, "score": 1.0}}


async def test_a_void_match_fails_the_step_and_keeps_what_it_graded():
    async with deployed(FakeSummary(TODAYS["the engine failed"])) as record:
        context = run_context(record)
        with pytest.raises(VoidMatch, match="the game engine failed"):
            await step(verifier_id="wc3").execute(context)
    assert context.metadata["rts_summary"]["wc3"] == TODAYS["the engine failed"]
    assert "verifications" not in context.metadata


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
