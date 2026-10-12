"""Fast unit-level decisions ("System 1") as choice questions any chat model can answer: the state as JSON and one
question per unit, each a menu of labels, sent to an OpenAI-compatible `/chat/completions` endpoint (agent-env's
LiteLLM proxy, say, with a small fast model), with the answers given back in the shape TypeSafe's Jev answers
choice questions in, so an agent written for Jev runs on any model. A model named `decisions/<model>`
(`decisions/gpt-6-luna`) is asked through OpenAI's Decisions API instead, Jev's kind of model: it answers typed
questions in one pass with a probability for each label and writes no text. A LiteLLM proxy reaches it through its
pass-through to OpenAI, `/openai/v1/decisions`, which reports no cost. Standard library only.

A question is `{"type": "choice", "instructions", "criteria": {label: meaning}}`. An answer the model left out or
made up falls back to the question's label meaning "keep doing what you're doing", when it has one, else its first
label; `fallbacks` counts them.
"""

from __future__ import annotations

import http.client
import json
import time
from urllib.parse import urlparse

SYSTEM = (
    "You are the fast unit-control system of a real-time strategy game agent. You get the battlefield as JSON and "
    "one multiple-choice question per unit you control. Answer every question with exactly one of its labels, "
    "copied exactly. Reply with JSON only, no prose: {\"answers\": {\"<question id>\": \"<label>\"}}."
)
KEEP_WORDS = ("continue", "keep", "stay idle", "wait")
DECISIONS = "decisions/"
DECISIONS_PRICE = 0.10   # dollars a million input tokens; output is free
MAX_TOKENS = 1500
RETRIES = 2


def chat(base_url: str, api_key: str, model: str, system: str, user: str, *, max_tokens: int = MAX_TOKENS,
         timeout: float = 30.0) -> tuple[str, dict]:
    """One completion from an OpenAI-compatible endpoint: (text, usage), the usage with `litellm_cost`, the call's
    dollars, when the endpoint is LiteLLM's and says."""
    path = urlparse(base_url.rstrip("/")).path.rstrip("/")
    path = (path if path.endswith("/v1") else path + "/v1") + "/chat/completions"
    reply, cost = post(base_url, path, api_key, {
        "model": model, "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}, timeout)
    message = reply["choices"][0]["message"]
    usage = dict(reply.get("usage") or {})
    if cost:
        usage["litellm_cost"] = float(cost)
    return message.get("content") or "", usage


def decide(base_url: str, api_key: str, model: str, payload: dict, *, timeout: float = 30.0) -> tuple[dict, dict]:
    """A Jev-style payload asked of OpenAI's Decisions API, through the LiteLLM proxy at `base_url`: (its answers by
    question name, each {"choice", "probabilities": [{"value", "probability"}], "confidence"}; usage)."""
    path = urlparse(base_url.rstrip("/")).path.rstrip("/").removesuffix("/v1") + "/openai/v1/decisions"
    reply, _ = post(base_url, path, api_key, {
        "model": model, "input": json.dumps(payload.get("state"), separators=(",", ":")),
        "questions": [{"name": name, "type": "choice", "instructions": q["instructions"],
                       "choices": [{"value": label, "description": meaning}
                                   for label, meaning in q["criteria"].items()]}
                      for name, q in payload["questions"].items()]}, timeout)
    answers = {a["name"]: a for a in reply.get("answers") or () if a.get("type") == "choice"}
    return answers, dict(reply.get("usage") or {})


def post(base_url: str, path: str, api_key: str, body: dict, timeout: float) -> tuple[dict, str | None]:
    """POST `body` to `path` at `base_url`'s host, retried on a dropped connection or a busy server: (the reply, the
    call's cost as LiteLLM reports it, when it does)."""
    url = urlparse(base_url.rstrip("/"))
    connect = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
    data = json.dumps(body).encode()
    for attempt in range(RETRIES + 1):
        connection = connect(url.netloc, timeout=timeout)
        try:
            connection.request("POST", path, data, {"Authorization": f"Bearer {api_key}",
                                                             "Content-Type": "application/json"})
            response = connection.getresponse()
            raw = response.read().decode(errors="replace")
            cost = response.getheader("x-litellm-response-cost")
        except (OSError, http.client.HTTPException):
            if attempt == RETRIES:
                raise
            time.sleep(1.0 + attempt)
            continue
        finally:
            connection.close()
        if response.status in (429, 500, 502, 503, 529) and attempt < RETRIES:
            time.sleep(1.0 + attempt)
            continue
        break
    if response.status != 200:
        raise RuntimeError(f"HTTP {response.status}: {raw.replace(api_key, '[REDACTED]')[:300]}")
    return json.loads(raw), cost


def cost_of(model: str, usage: dict) -> float:
    """A call's dollars: as LiteLLM reported them, else, for the Decisions API (whose pass-through reports none), its
    input tokens at DECISIONS_PRICE."""
    if "litellm_cost" in usage:
        return float(usage["litellm_cost"])
    return usage.get("input_tokens", 0) * DECISIONS_PRICE / 1e6 if model.startswith(DECISIONS) else 0.0


def fallback(criteria: dict[str, str]) -> str:
    for label, meaning in criteria.items():
        if any(word in f"{label} {meaning}".lower() for word in KEEP_WORDS):
            return label
    return next(iter(criteria))


def parse_answers(text: str, questions: dict[str, dict]) -> tuple[dict[str, str], int]:
    """The model's label for each question, a fallback where it gave none or one not on the menu; and how many
    fell back."""
    answers: dict = {}
    start = text.find("{")
    if start >= 0:
        try:
            reply, _ = json.JSONDecoder().raw_decode(text, start)
            answers = reply.get("answers", reply) if isinstance(reply, dict) else {}
        except ValueError:
            answers = {}
    chosen, fallbacks = {}, 0
    for name, question in questions.items():
        label = answers.get(name) if isinstance(answers, dict) else None
        if isinstance(label, dict):
            label = label.get("choice") or label.get("label")
        if label not in question["criteria"]:
            label, fallbacks = fallback(question["criteria"]), fallbacks + 1
        chosen[name] = label
    return chosen, fallbacks


def jev_shaped(model: str, questions: dict[str, dict], chosen: dict[str, str], usage: dict,
               decided: dict[str, dict] | None = None) -> dict:
    """The answers as Jev's /v1/systemone gives them: a choice per question with its labels' probabilities (as the
    Decisions API `decided` them, else certain), and token usage."""
    def weighed(name: str, label: str) -> tuple[dict, float]:
        given = {p["value"]: p["probability"] for p in (decided or {}).get(name, {}).get("probabilities") or ()}
        total = sum(given.get(c, 0.0) for c in questions[name]["criteria"])
        if label not in given or not total:
            return {c: 1.0 if c == label else 0.0 for c in questions[name]["criteria"]}, 1.0
        return ({c: given.get(c, 0.0) / total for c in questions[name]["criteria"]},
                float(decided[name].get("confidence", 1.0)))

    answers = {}
    for name, label in chosen.items():
        probabilities, confidence = weighed(name, label)
        answers[name] = {"type": "choice", "choice": label, "probabilities": probabilities, "confidence": confidence}
    return {
        "model": model,
        "answers": answers,
        "usage": {"input_tokens": int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
                  "output_tokens": int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
                  **({"litellm_cost": usage["litellm_cost"]} if "litellm_cost" in usage else {})},
    }


def answer(payload: dict, *, base_url: str, api_key: str, model: str, timeout: float = 30.0) -> tuple[dict, dict]:
    """Answer a Jev-style payload ({"state", "questions"}) with `model`: (the Jev-shaped response, a record of the
    call: status, elapsed_seconds, fallbacks, raw reply)."""
    started = time.perf_counter()
    if model.startswith(DECISIONS):
        decided, usage = decide(base_url, api_key, model.removeprefix(DECISIONS), payload, timeout=timeout)
        text = json.dumps({"answers": {name: a.get("choice") for name, a in decided.items()}})
    else:
        questions = {name: {"instructions": q["instructions"], "choices": q["criteria"]}
                     for name, q in payload["questions"].items()}
        user = json.dumps({"state": payload.get("state"), "questions": questions}, separators=(",", ":"))
        (text, usage), decided = chat(base_url, api_key, model, SYSTEM, user, timeout=timeout), None
    chosen, fallbacks = parse_answers(text, payload["questions"])
    record = {"status": 200, "elapsed_seconds": round(time.perf_counter() - started, 3), "fallbacks": fallbacks,
              "raw_reply": text[:4000], "request_id": None}
    return jev_shaped(model, payload["questions"], chosen, usage, decided), record
