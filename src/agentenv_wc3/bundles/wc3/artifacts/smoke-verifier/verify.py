"""Check a setup: the game launched, ran to its time limit with no orders, and kept running."""

from agentenv_protocol import client

GATE_WEIGHT = -100


def grade(s: dict) -> list[dict]:
    harness = s.get("harness") or {}
    return [
        {"criterion": "the game ran to its time limit", "result": s.get("result") == "time_limit",
         "game_time_seconds": s.get("game_time_seconds"), "time_limit_seconds": s.get("time_limit_seconds")},
        {"criterion": "it was played by the harness's idle extension", "result": harness.get("idle_seconds", 0) > 0,
         "harness": harness},
        {"criterion": "the game kept running", "weight": GATE_WEIGHT, "result": not s.get("engine_failed"),
         "error": s.get("error")},
    ]


async def verify(mcp_url: str) -> list[dict]:
    summary = (await client.get_data(mcp_url.removesuffix("/mcp"))).parts[0].data
    return grade(summary)
