# The worker protocol

`agentenv_wc3.server` plays the game through one worker process, `src/agentenv_wc3/worker.py`. In the env image the
worker is Windows Python under Wine (`wine C:\Python311\python.exe Z:\...\worker.py`), next to the game; it imports
only the standard library and wc3env. With `--fake` it plays wc3env's fake game (`wc3env.fake_server`) in any
Python, for tests.

One JSON request per line on stdin, one reply per line on stdout, in order. Everything else (wc3env's and the game's
output) goes to stderr. The first line out is the ready line:

```json
{"id": 0, "ok": true, "result": {"ready": true, "version": "0.1.0", "fake": false}}
```

Requests are `{"id", "cmd", "args"}`. A reply is `{"id", "ok": true, "result"}`, or
`{"id", "ok": false, "error": {"code", "message"}}`.

| Command | Args | Result |
|---|---|---|
| `start` | `map`, `players` (`[{"slot", "race", "control": "agent" \| "computer"}]`), `step_ms`, `seed`, `randomize_starts`, `ai_difficulty` (0 easy, 1 normal, 2 insane), `render` and `visible` (draw the game in a window on the display, for its picture), `mode` (`stepping`, or `realtime`: the game runs on its own clock and `step` only sends orders and observes) | `{"observations": {slot: observation}, "setup"}`: closes any game before, launches the game, and returns every player's first observation (wc3env's JSON observations) |
| `validate` | `slot`, `actions` | `{"valid": n}`, or `bad_actions`: wc3env's own host checks against the slot's newest observation, without sending anything |
| `step` | `actions` (`{slot: [action]}`; a slot left out sends none), `ms` (a multiple of 25, 25-60000) | `{"observations", "done", "rejected": {slot: [{"index", "reason"}]}, "placements": {slot: [{"index", "x", "y"}]}, "elapsed_ms"}` |
| `observe` | | `{"observations", "done"}` |
| `replay` | | `{"name": "game.w3g", "base64"}`: the episode's native replay; recording stops |
| `debug` | `op`, `args` | wc3env's `GameSession.debug`; with `--fake`, `end` ends the game with `args.result` |
| `close` | | `{"closed": true}` |

Error codes: `bad_config`, `bad_args`, `bad_actions`, `no_game`, `no_replay`, `unknown_command`, `bad_request`, and
`game_failed` for anything the game or wc3env raised (a step that failed part way leaves no game: start a new one).
The env adds `worker_down` and `timeout` when the worker exits or stops answering.
