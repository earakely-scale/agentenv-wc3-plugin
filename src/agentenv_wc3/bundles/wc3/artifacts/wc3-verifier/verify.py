"""Grade a Warcraft III game against the game's own AI from the env's data/get summary.

A win (every enemy building destroyed) is what counts most; an undecided game at the time limit earns the part
of the score for outscoring the AI on the game's own score total.
"""

from agentenv_protocol import client

# A failed gate outweighs every other criterion, so the weighted average is 0.
GATE_WEIGHT = -100


def grade(s: dict) -> list[dict]:
    mine, theirs = (s.get("score") or {}).get("total", 0), (s.get("opponent_score") or {}).get("total", 0)
    harness = s.get("harness") or {}
    return [
        {"criterion": "the game reached its end: a result, or the time limit", "result": bool(s.get("game_over")),
         "result_kind": s.get("result"), "game_time_seconds": s.get("game_time_seconds"),
         "time_limit_seconds": s.get("time_limit_seconds")},
        {"criterion": "won: every enemy building destroyed", "weight": 3, "result": s.get("result") == "victory"},
        {"criterion": "not defeated", "result": s.get("result") != "defeat"},
        {"criterion": "outscored the AI on the game's score total", "result": mine > theirs,
         "score": min(1.0, mine / theirs) if theirs else float(mine > 0), "game_score": mine, "ai_score": theirs},
        {"criterion": "the game kept running", "weight": GATE_WEIGHT, "result": not s.get("engine_failed"),
         "error": s.get("error")},
        {"criterion": "the agent played: it gave orders, and the harness let no game time pass for it",
         "weight": GATE_WEIGHT, "result": harness.get("orders_sent", 0) > 0 and not harness.get("idle_seconds"),
         "harness": harness},
    ]


async def verify(mcp_url: str) -> list[dict]:
    base_url = mcp_url.removesuffix("/mcp")
    try:
        summary = (await client.get_data(base_url)).parts[0].data
    except Exception as e:
        return [{"criterion": "the env answered data/get", "weight": GATE_WEIGHT, "result": False, "error": repr(e)}]
    return grade(summary)
