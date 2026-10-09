"""What a check reads and how it compares: the summary's metric names (wc3agent's) and the comparisons, for
rts_grade's checks and an env's own end conditions alike. No agent-env imports, so an env's image can use it."""

from __future__ import annotations

import operator

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
