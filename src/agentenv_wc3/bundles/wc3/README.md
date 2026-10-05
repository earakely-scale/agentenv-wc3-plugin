Warcraft III (wc3env): an agent plays one side of a melee game on Echo Isles against the game's own AI, through raw
wc3env orders, and the game is graded and its replay saved.

Every task deploys the env registered as `wc3` (run `agent-env wc3 setup` once) and starts its game with the
plugin's `wc3_match` step, which also sends your activation files (`roc.w3k`, `tft.w3k`) from your license
directory: `license_dir` in `[plugins.agentenv-wc3]` of `.agentenv/config.toml`, `$WC3_LICENSE_DIR`, or
`~/.wc3-license`.

- `smoke` needs no model and no agent: the harness lets a 2-minute game run with no orders (`urn:wc3:idle/v1`)
  and checks that it launched, ran to its time limit and kept running, then saves the replay. It checks the image,
  Wine, the license files, the worker and the extensions end to end.
- `vs-ai-quick`: your default agent plays Human against the easy Orc AI for 5 minutes of game time. A check that an
  agent can drive the tools; expect 15-60 minutes of wall time.
- `vs-ai`: the same against the normal AI, for 20 minutes of game time. Allow a few hours: every order and every
  step is a tool call.

The `deploy_agent` steps name no agent, so agent-env deploys your configured default (`[agents]
default_a2a_agent_id` in `.agentenv/config.toml`); it must take the env's MCP server
(`urn:agentenv:mcp-config/v1`).

`wc3-verifier` grades a game: a win counts three times as much as not being defeated or outscoring the AI on the
game's own score total, and two gates zero the grade: the game stopped working, or the agent gave no orders (or the
harness played part of its game).

- `macro-micro-quick`, `macro-micro` and `macro-micro-realtime`: the `wc3-macro-micro` agent (wc3env's wc3agent:
  a macro model plans, a micro model controls the army) plays through the env's `urn:rts` session, Haiku 4.5 for both
  models against the easy AI for 5 minutes, then Sonnet 5.5 macro with Haiku 4.5 micro against the normal AI, for 20
  minutes stepped or 10 in realtime. `WC3_MICRO_MODEL` in the `deploy_agent` step picks the micro model (`jev` for
  TypeSafe's Jev); the `prompt_agent` model is the macro model. `agent-env wc3 setup --agent` registers the agent.

Every task also saves the spectator recording (`save_rts_recording`): an MP4 of the map and a self-contained HTML
replay; `macro-micro-realtime` adds the game's own video, with chapters, and a highlight reel cut from it.
`agent-env wc3 setup --fake` builds the env on wc3env's fake game, so every task runs end to end without
Warcraft III (units move and stop, nothing else).
