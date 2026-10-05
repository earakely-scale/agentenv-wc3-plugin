"""Fast unit-level decisions ("System 1") as choice questions any chat model can answer: the state as JSON and one
question per unit, each a menu of labels, sent to an OpenAI-compatible `/chat/completions` endpoint (agent-env's
LiteLLM proxy, say, with a small fast model), with the answers given back in the shape TypeSafe's Jev answers
choice questions in, so an agent written for Jev runs on any model. Standard library only.

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
MAX_TOKENS = 1500
RETRIES = 2


def chat(base_url: str, api_key: str, model: str, system: str, user: str, *, max_tokens: int = MAX_TOKENS,
         timeout: float = 30.0) -> tuple[str, dict]:
    """One completion from an OpenAI-compatible endpoint: (text, usage)."""
    url = urlparse(base_url.rstrip("/"))
    path = url.path.rstrip("/")
    path = (path if path.endswith("/v1") else path + "/v1") + "/chat/completions"
    body = json.dumps({"model": model, "max_tokens": max_tokens,
                       "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
    connect = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
    for attempt in range(RETRIES + 1):
        connection = connect(url.netloc, timeout=timeout)
        try:
            connection.request("POST", path, body.encode(), {"Authorization": f"Bearer {api_key}",
                                                             "Content-Type": "application/json"})
            response = connection.getresponse()
            raw = response.read().decode(errors="replace")
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
    reply = json.loads(raw)
    message = reply["choices"][0]["message"]
    return message.get("content") or "", reply.get("usage") or {}


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


def jev_shaped(model: str, questions: dict[str, dict], chosen: dict[str, str], usage: dict) -> dict:
    """The answers as Jev's /v1/systemone gives them: a certain choice per question, and token usage."""
    return {
        "model": model,
        "answers": {name: {"type": "choice", "choice": label,
                           "probabilities": {c: 1.0 if c == label else 0.0 for c in questions[name]["criteria"]},
                           "confidence": 1.0}
                    for name, label in chosen.items()},
        "usage": {"input_tokens": int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
                  "output_tokens": int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)},
    }


def answer(payload: dict, *, base_url: str, api_key: str, model: str, timeout: float = 30.0) -> tuple[dict, dict]:
    """Answer a Jev-style payload ({"state", "questions"}) with `model`: (the Jev-shaped response, a record of the
    call: status, elapsed_seconds, fallbacks, raw reply)."""
    questions = {name: {"instructions": q["instructions"], "choices": q["criteria"]}
                 for name, q in payload["questions"].items()}
    user = json.dumps({"state": payload.get("state"), "questions": questions}, separators=(",", ":"))
    started = time.perf_counter()
    text, usage = chat(base_url, api_key, model, SYSTEM, user, timeout=timeout)
    chosen, fallbacks = parse_answers(text, payload["questions"])
    record = {"status": 200, "elapsed_seconds": round(time.perf_counter() - started, 3), "fallbacks": fallbacks,
              "raw_reply": text[:4000], "request_id": None}
    return jev_shaped(model, payload["questions"], chosen, usage), record
