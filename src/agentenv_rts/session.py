"""The `urn:rts:*` session contract: how a program plays an RTS env through the env's extensions, as a gym-style
session, rather than through MCP tools an LLM reads. The env keeps the game; the client steps it.

- `urn:rts:observe/v1` {} → {"observations": {slot: obs}, "done", "scenario", "setup"}
- `urn:rts:step/v1` {"actions": {slot: [action]}, "ms"} → {"observations", "done", "rejected": {slot: [...]},
  "placements": {slot: [...]}, "elapsed_ms"}
- `urn:rts:debug/v1` {"op", "args"} → the game's debug op result; an env may refuse ops it doesn't allow
- `urn:rts:note/v1` {"slot", "kind", "text", "data"} → {} : what a player tells the spectators. `plan` is its current
  plan in a sentence or two, `player` names it (text: "Claude Sonnet 5.5 + Haiku 4.5"), `stats` gives its running
  costs (data: {"cost_usd", "decisions", "tokens"})

Observations and actions are the game's own (for Warcraft III, wc3env's JSON). JSON keys slots as strings; the
client gives them back as ints. `RemoteSession` is synchronous and uses only the standard library, so it runs in any
agent image and in agents whose game loops are threaded rather than async.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

OBSERVE = "urn:rts:observe/v1"
STEP = "urn:rts:step/v1"
DEBUG = "urn:rts:debug/v1"
NOTE = "urn:rts:note/v1"
NOTE_KINDS = ("plan", "player", "stats")
CARD_PATH = "/.well-known/agent-env.json"


class SessionError(RuntimeError):
    """The env refused a call or could not be reached; `status` is the HTTP status, or None."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def error_message(body: bytes | str) -> str:
    """What an env's error reply says: the protocol's {"error": {"code", "message"}}, else the body itself."""
    text = body.decode(errors="replace") if isinstance(body, bytes) else body
    try:
        error = json.loads(text).get("error") or {}
        return f"{error['code']}: {error['message']}" if error.get("message") else text
    except (ValueError, AttributeError, KeyError):
        return text


def by_slot(value: dict | None) -> dict[int, object]:
    """{slot: x} with int slots, as JSON gives them back with string keys."""
    return {int(k): v for k, v in (value or {}).items()}


class RemoteSession:
    """One env's game, played over its `urn:rts:*` extensions: `base_url` is the env's root (its MCP URL without
    `/mcp`). The endpoints come from the env's card."""

    def __init__(self, base_url: str, *, timeout: float = 120, headers: dict[str, str] | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.headers = dict(headers or {})
        self._endpoints: dict[str, str] | None = None

    def observe(self) -> dict:
        result = self.call(OBSERVE, {})
        return {**result, "observations": by_slot(result.get("observations"))}

    def step(self, actions: dict[int, list], ms: int | None = None) -> dict:
        params = {"actions": {str(slot): list(batch) for slot, batch in actions.items()}}
        if ms is not None:
            params["ms"] = ms
        result = self.call(STEP, params)
        return {**result, "observations": by_slot(result.get("observations")),
                "rejected": by_slot(result.get("rejected")), "placements": by_slot(result.get("placements"))}

    def debug(self, op: str, **args) -> dict:
        return self.call(DEBUG, {"op": op, "args": args})

    def note(self, kind: str, text: str = "", slot: int = 0, data: dict | None = None) -> dict:
        return self.call(NOTE, {"slot": slot, "kind": kind, "text": text, "data": data or {}})

    def call(self, uri: str, params: dict) -> dict:
        endpoint = self.endpoints().get(uri)
        if endpoint is None:
            raise SessionError(f"the env at {self.base_url} does not offer {uri}")
        return self._request("POST", endpoint, params)

    def endpoints(self) -> dict[str, str]:
        """Each extension's endpoint, from the env's card (fetched once)."""
        if self._endpoints is None:
            card = self._request("GET", CARD_PATH, None)
            self._endpoints = {e["uri"]: (e.get("params") or {}).get("endpoint")
                               for e in (card.get("capabilities") or {}).get("extensions") or []
                               if (e.get("params") or {}).get("endpoint")}
        return self._endpoints

    def _request(self, method: str, path: str, body: dict | None) -> dict:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(f"{self.base_url}{path}", data=data, method=method, headers={
            **self.headers, "Accept": "application/json", **({"Content-Type": "application/json"} if data else {})})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as e:
            raise SessionError(f"{method} {path}: HTTP {e.code}: {error_message(e.read(4000))}", e.code) from e
        except (OSError, ValueError) as e:
            raise SessionError(f"{method} {path}: {e}") from e
