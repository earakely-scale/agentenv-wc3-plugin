"""`rts_grade`: judge a finished RTS game from the env's `data/get` summary with a rubric set in the task. Each graded
seat gets its own verification, scored by agent-env's weighted average, so runs, evals and the hub read it as they
read any verifier's; a failed gate outweighs every criterion and zeroes it."""

from __future__ import annotations

import logging
import operator
from dataclasses import dataclass
from typing import ClassVar

from agent_env.entity_refs import EntityRef
from agent_env.task_step.context import TaskStepContext
from agent_env.task_step.task_step import TaskStep
from agent_env.task_step.task_steps.verifiers.scoring import ScoreAggregator, aggregate_score
from agentenv_protocol import client

log = logging.getLogger(__name__)

GATE_WEIGHT = -100
MELEE = {"reached_end": 1, "win": 3, "survive": 1, "outscore": 1}
RUBRICS = {
    "melee": MELEE,
    "dense": {**MELEE, "army_ratio": 1, "kills_ratio": 1, "buildings_destroyed": 1, "tier": 1, "expansions": 1,
              "hero_level": 1},
    "checks": {},
    "smoke": {"ran_to_limit": 1, "idle_played": 1},
}
TARGETS = {"army_ratio": 1.0, "buildings_destroyed": 5, "tier": 3, "expansions": 1, "hero_level": 5}
GATES = ("game_ran", "agent_played")
AT_TIME_LIMIT = ("score", "draw", "loss")
OPS = {">=": operator.ge, "<=": operator.le, "==": operator.eq, ">": operator.gt, "<": operator.lt}
CHECK_KEYS = frozenset(("metric", "op", "value", "weight"))
METRICS = frozenset((
    "total army workers hero_alive hero_level hero_health_percent items_carried structure_health_percent "
    "unspent_skill_points tier expansions units_trained units_killed units_lost workers_lost structures_lost "
    "buildings_destroyed gold_mined lumber_total items_picked_up items_used items_bought researches_done hero_time "
    "expansion_started_time upgrade_started_time enemy_base_seen_time idle_worker_seconds supply_blocked_seconds "
    "average_unspent_gold uprooted_seconds fewest_workers army_kept_percent enemy_army_destroyed_percent "
    "camp_cleared camp_cleared_time seconds structures").split())
BY_TYPE = {"count": 0, "first_time": None, "present_seconds": 0}
PARAMS = ("seats", "rubric", "weights", "targets", "checks", "at_time_limit", "gates", "verifier_id",
          "timeout_seconds")


def known_metric(name: str) -> bool:
    key, _, arg = name.partition(":")
    if not arg:
        return name in METRICS
    if key in BY_TYPE:
        return True
    return key == "camp_cleared" and arg.startswith("camp:") and arg.removeprefix("camp:").isdigit()


def metric(metrics: dict, name: str):
    """A metric by wc3agent's name: `count:hhou`, `first_time:hbar`, `present_seconds:hmil` and
    `camp_cleared:camp:9` read the summary's maps; None when the summary doesn't report it."""
    key, _, arg = name.partition(":")
    if key == "camp_cleared" and arg:
        camps = metrics.get("camps_cleared")
        return None if camps is None else int(arg.removeprefix("camp:")) in camps
    if key in BY_TYPE and arg:
        by_type = metrics.get(key)
        return None if by_type is None else by_type.get(arg, BY_TYPE[key])
    return metrics.get(name)


def seats(summary: dict) -> tuple[list[dict], list[dict]]:
    """Every seat, and the agent seats, which are graded unless the task names its seats. A summary without seats
    (the env before them) is one agent seat against the game's AI, read from its top-level fields."""
    if "seats" in summary:
        return summary["seats"], [s for s in summary["seats"] if not s.get("computer")]
    harness = summary.get("harness") or {}
    mine = {"agent": None, "computer": None, "team": 0, "result": summary.get("result"),
            "orders_sent": harness.get("orders_sent", 0), "metrics": summary.get("score") or {}}
    ai = {"agent": None, "computer": summary.get("ai_difficulty"), "team": 1,
          "metrics": summary.get("opponent_score") or {}}
    return [mine, ai], [mine]


@dataclass
class Game:
    """The game as one graded seat sees it: opponents are the seats on another team."""

    summary: dict
    seat: dict
    enemies: list[dict]
    at_time_limit: str
    targets: dict

    @property
    def result(self) -> str | None:
        return self.seat.get("result")

    def metric(self, name: str, seat: dict | None = None):
        return metric((self.seat if seat is None else seat).get("metrics") or {}, name)


def _total(seat: dict) -> float:
    return (seat.get("metrics") or {}).get("total", 0)


def _clock(g: Game) -> str:
    return f"{g.summary.get('game_time_seconds')} of {g.summary.get('time_limit_seconds')} game seconds"


def _unreported(criterion: str, what: str) -> dict:
    return {"criterion": criterion, "result": False, "score": 0.0, "evidence": f"{what} not reported"}


def reached_end(g: Game) -> dict:
    return {"criterion": "the game reached its end: a result, or the time limit",
            "result": bool(g.summary.get("game_over")), "evidence": f"{g.result or 'no result'} at {_clock(g)}"}


def win(g: Game) -> dict:
    by_score = g.at_time_limit == "score"
    mine, best = _total(g.seat), max(map(_total, g.enemies), default=0)
    tiebreak = by_score and g.result == "time_limit" and mine > best
    criterion = "won: every enemy building destroyed" + (", or the higher score at the time limit" if by_score else "")
    return {"criterion": criterion, "result": g.result == "victory" or tiebreak,
            "evidence": f"{g.result or 'no result'}, score {mine} vs {best}"}


def survive(g: Game) -> dict:
    loss = g.at_time_limit == "loss"
    return {"criterion": "not defeated" + ("; the time limit counts as a defeat" if loss else ""),
            "result": g.result != "defeat" and not (loss and g.result == "time_limit"),
            "evidence": g.result or "no result"}


def outscore(g: Game) -> dict:
    mine, best = _total(g.seat), max(map(_total, g.enemies), default=0)
    who = "the AI" if g.enemies and all(e.get("computer") for e in g.enemies) else "every opponent"
    return {"criterion": f"outscored {who} on the game's score total", "result": mine > best,
            "score": min(1.0, mine / best) if best else float(mine > 0), "evidence": f"score {mine} vs {best}"}


def army_ratio(g: Game) -> dict:
    target = g.targets["army_ratio"]
    criterion = f"an army at least {target:g}x the strongest enemy's"
    mine, theirs = g.metric("army"), [g.metric("army", e) for e in g.enemies]
    if mine is None or None in theirs:
        return _unreported(criterion, "army")
    strongest = max(theirs, default=0)
    score = min(1.0, mine / (strongest * target)) if strongest else float(mine > 0)
    return {"criterion": criterion, "result": score >= 1, "score": score, "evidence": f"army {mine} vs {strongest}"}


def kills_ratio(g: Game) -> dict:
    criterion = "killed more units than it lost"
    killed, lost = g.metric("units_killed"), g.metric("units_lost")
    if killed is None or lost is None:
        return _unreported(criterion, "units killed or lost")
    return {"criterion": criterion, "result": killed > lost,
            "score": killed / (killed + lost) if killed + lost else 0.0, "evidence": f"killed {killed}, lost {lost}"}


def buildings_destroyed(g: Game) -> dict:
    """The share of the enemy's buildings when the enemies report the ones standing, else toward a target."""
    criterion = "destroyed the enemy's buildings"
    destroyed, standing = g.metric("buildings_destroyed"), [g.metric("structures", e) for e in g.enemies]
    if destroyed is None:
        return _unreported(criterion, "buildings destroyed")
    if g.enemies and None not in standing:
        of = destroyed + sum(standing)
        score, evidence = (destroyed / of if of else 0.0), f"destroyed {destroyed} of {of}"
    else:
        target = g.targets["buildings_destroyed"]
        score, evidence = min(1.0, destroyed / target), f"destroyed {destroyed}, full credit at {target}"
    return {"criterion": criterion, "result": score >= 1, "score": score, "evidence": evidence}


def _toward(g: Game, name: str, criterion: str) -> dict:
    value, target = g.metric(name), g.targets[name]
    if value is None:
        return _unreported(criterion, name)
    score = min(1.0, value / target)
    return {"criterion": criterion, "result": score >= 1, "score": score, "evidence": f"{name} {value} of {target}"}


def tier(g: Game) -> dict:
    return _toward(g, "tier", f"the tier reached, full credit at {g.targets['tier']}")


def expansions(g: Game) -> dict:
    return _toward(g, "expansions", f"the expansions taken, full credit at {g.targets['expansions']}")


def hero_level(g: Game) -> dict:
    return _toward(g, "hero_level", f"the highest hero level, full credit at {g.targets['hero_level']}")


def ran_to_limit(g: Game) -> dict:
    return {"criterion": "the game ran to its time limit", "result": g.result == "time_limit", "evidence": _clock(g)}


def idle_played(g: Game) -> dict:
    idle = (g.summary.get("harness") or {}).get("idle_seconds") or 0
    return {"criterion": "it was played by the harness's idle extension", "result": idle > 0,
            "evidence": f"the harness let {idle} game seconds pass"}


def game_ran(g: Game) -> dict:
    return {"criterion": "the game kept running", "result": not g.summary.get("engine_failed"),
            "evidence": g.summary.get("error") or "no engine failure"}


def agent_played(g: Game) -> dict:
    orders, idle = g.seat.get("orders_sent") or 0, (g.summary.get("harness") or {}).get("idle_seconds") or 0
    return {"criterion": "the agent played: it gave orders, and the harness let no game time pass for it",
            "result": orders > 0 and not idle, "evidence": f"{orders} orders; the harness let {idle} game seconds pass"}


def settled(finish: dict, seat: dict) -> dict:
    """How the game came to its end once the agents stopped (rts_finish), for the record: no weight."""
    stopped = any(x.get("slot") == seat.get("slot") for x in finish.get("open_seats") or ())
    last = seat.get("last_move_seconds")
    return {"name": "finish", "criterion": "how the game ended once the agents stopped (information only)",
            "result": True, "weight": 0,
            "evidence": (f"this seat stopped before the end (last move at {last} s); " if stopped else "")
            + f"rts_finish {finish.get('rule')}: from {finish.get('from_seconds')} to {finish.get('to_seconds')} "
              f"game seconds" + (", a forfeit" if seat.get("forfeit") else "")}


def check(g: Game, c: dict) -> dict:
    """A metric against a value; one without an op only reports the metric, as wc3agent's do."""
    measured = g.metric(c["metric"])
    if "op" not in c:
        return {"name": c["metric"], "criterion": c["metric"], "result": None, "weight": 0,
                "evidence": f"measured {measured}"}
    return {"name": c["metric"], "criterion": f"{c['metric']} {c['op']} {c['value']}",
            "result": measured is not None and OPS[c["op"]](measured, c["value"]), "weight": c.get("weight", 1),
            "evidence": f"measured {measured}"}


CRITERIA = {f.__name__: f for f in (reached_end, win, survive, outscore, army_ratio, kills_ratio, buildings_destroyed,
                                    tier, expansions, hero_level, ran_to_limit, idle_played)}
GATE_CHECKS = {"game_ran": game_ran, "agent_played": agent_played}


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class RTSGradeTaskStep(TaskStep):
    """Grade the seats of a deployed RTS env's finished game: `rubric` picks the preset criteria, `weights` reweighs
    them (0 drops one, a weight adds one from another preset), `targets` sets full credit, `checks` adds metric
    checks, `at_time_limit` decides an undecided game (the higher score wins it, a draw, or a loss) and `gates`
    the failures that zero a seat."""

    type: ClassVar[str] = "rts_grade"
    entity_refs = (EntityRef.env("env_id"),)

    def __init__(self, id: str, version: int | None, env_id: str, seats: list[str] | None = None,
                 rubric: str = "melee", weights: dict | None = None, targets: dict | None = None,
                 checks: list[dict] | None = None, at_time_limit: str = "draw", gates: list[str] | None = None,
                 verifier_id: str | None = None, timeout_seconds: int = 300, depends_on: list | None = None,
                 fail_task_on_error: bool = True):
        super().__init__(id, version, depends_on=depends_on, fail_task_on_error=fail_task_on_error)
        self.env_id, self.rubric, self.at_time_limit = env_id, rubric, at_time_limit
        self.seats = list(seats) if seats is not None else None
        self.weights, self.targets = dict(weights or {}), dict(targets or {})
        self.checks = [dict(c) for c in checks or []]
        self.gates = list(gates) if gates is not None else list(GATES)
        self.verifier_id, self.timeout_seconds = verifier_id or id, timeout_seconds
        if rubric not in RUBRICS:
            raise ValueError(f"rts_grade rubric must be one of {', '.join(RUBRICS)}, got {rubric!r}")
        if at_time_limit not in AT_TIME_LIMIT:
            raise ValueError(f"rts_grade at_time_limit must be one of {', '.join(AT_TIME_LIMIT)}, got "
                             f"{at_time_limit!r}")
        if unknown := sorted(set(self.gates) - set(GATES)):
            raise ValueError(f"rts_grade gates must be among {', '.join(GATES)}, got {unknown}")
        if seats is not None and (not isinstance(seats, list) or not all(isinstance(s, str) and s for s in seats)):
            raise ValueError(f"rts_grade seats are agent names, got {seats!r}")
        if unknown := sorted(set(self.weights) - set(CRITERIA)):
            raise ValueError(f"rts_grade weights are for the criteria {', '.join(CRITERIA)}, got {unknown}")
        if bad := sorted(k for k, w in self.weights.items() if not _number(w)):
            raise ValueError(f"rts_grade weights must be numbers, got {bad}")
        if unknown := sorted(set(self.targets) - set(TARGETS)):
            raise ValueError(f"rts_grade targets are for {', '.join(TARGETS)}, got {unknown}")
        if bad := sorted(k for k, t in self.targets.items() if not _number(t) or t <= 0):
            raise ValueError(f"rts_grade targets must be positive numbers, got {bad}")
        for c in self.checks:
            if not isinstance(c.get("metric"), str) or (unknown := sorted(set(c) - CHECK_KEYS)):
                raise ValueError(f"rts_grade checks are {{metric, op, value, weight}}, got {c!r}")
            if not known_metric(c["metric"]):
                raise ValueError(f"rts_grade check metric {c['metric']!r} is not one the summary reports")
            if "op" in c and (c["op"] not in OPS or "value" not in c):
                raise ValueError(f"rts_grade check {c!r} needs an op among {', '.join(OPS)} and a value")
            if "weight" in c and not _number(c["weight"]):
                raise ValueError(f"rts_grade check weight must be a number, got {c!r}")
        if rubric == "checks" and not self.checks:
            raise ValueError("rts_grade rubric checks needs checks")

    def to_dict(self) -> dict:
        return {**super().to_dict(), "env_id": self.env_id, **{k: getattr(self, k) for k in PARAMS}}

    @classmethod
    def from_dict(cls, data: dict) -> RTSGradeTaskStep:
        return cls(**cls._base_from_dict(data), env_id=data["env_id"], **{k: data[k] for k in PARAMS if k in data})

    def criteria(self) -> dict[str, float]:
        return {k: w for k, w in {**RUBRICS[self.rubric], **self.weights}.items() if w}

    def grade(self, summary: dict) -> dict[str, list[dict]]:
        """The rows of each graded seat's verification, by its key: `<verifier_id>:<agent>`, or `verifier_id`
        alone for a seat no agent name identifies (a summary without seats)."""
        everyone, agents = seats(summary)
        if self.seats is None:
            graded = [(s.get("agent"), s) for s in agents]
        else:
            graded = [(name, next((s for s in everyone if s.get("agent") == name), None)) for name in self.seats]
        targets = {**TARGETS, **self.targets}
        gates = [k for k in self.gates if not (self.rubric == "smoke" and k == "agent_played")]
        out = {}
        for name, seat in graded:
            key = self.verifier_id if name is None else f"{self.verifier_id}:{name}"
            if seat is None:
                out[key] = [{"name": "seat", "criterion": f"the game has a seat for {name}", "result": False,
                             "weight": GATE_WEIGHT, "evidence": f"seats {[s.get('agent') for s in everyone]}"}]
                continue
            g = Game(summary, seat, [s for s in everyone if s.get("team") != seat.get("team")], self.at_time_limit,
                     targets)
            out[key] = [{"name": k, **CRITERIA[k](g), "weight": w} for k, w in self.criteria().items()]
            out[key] += [check(g, c) for c in self.checks]
            out[key] += [{"name": k, **GATE_CHECKS[k](g), "weight": GATE_WEIGHT} for k in gates]
            if finish := summary.get("finish"):
                out[key].append(settled(finish, seat))
        return out

    async def execute(self, context: TaskStepContext) -> TaskStepContext:
        step = self.from_dict({**self.to_dict(), **self.step_param_overrides(context)})
        deployed = next((d for d in context.deployed_envs if d.env_id == step.env_id), None)
        if deployed is None:
            raise RuntimeError(f"env {step.env_id!r} is not deployed in this run")
        try:
            summary = (await client.get_data(deployed.mcp_url.removesuffix("/mcp"),
                                             timeout=step.timeout_seconds)).parts[0].data
        except Exception as e:
            log.warning("rts_grade: env %r did not answer data/get: %r", step.env_id, e)
            keys = [step.verifier_id] if step.seats is None else [f"{step.verifier_id}:{n}" for n in step.seats]
            graded = {k: [{"name": "data_get", "criterion": "the env answered data/get", "result": False,
                           "weight": GATE_WEIGHT, "evidence": repr(e)}] for k in keys}
        else:
            graded = step.grade(summary)
        verifications = context.metadata.setdefault("verifications", {})
        for key, rows in graded.items():
            verifications[key] = {"results": rows, "score": aggregate_score(rows, ScoreAggregator.WEIGHTED_AVERAGE)}
            log.info("rts_grade %s: %d criteria, score %.3f", key, len(rows), verifications[key]["score"])
        return context
