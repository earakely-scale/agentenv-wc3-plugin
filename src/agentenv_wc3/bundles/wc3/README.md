Warcraft III (wc3env): agents play melee games on Echo Isles against the game's own AI or each other, and short
drills, through the env's MCP tools or raw wc3env orders. Each game is graded per seat, and its replay and recording
are saved.

Every task deploys the env registered as `wc3` (run `agent-env wc3 setup` once), gives it your activation files
(`roc.w3k`, `tft.w3k`) with agentenv-game-env's `add_license` step from agent-env's secret store (where
`agent-env wc3 license import` puts them), and opens its match's lobby with `open_lobby`.

- `smoke` needs no model and no agent: `finish_match` plays a 2-minute game out with no orders and checks that it
  launched, ran to its time limit and kept running, then saves the replay. It checks the image,
  Wine, the license files, the worker and the extensions end to end.
- `vs-ai-quick`: `wc3-llm` with Haiku 4.5 plays Human through the MCP tools (`get_state`, `act`, `advance`)
  against the easy Orc AI, for 5 minutes of game time.
- `vs-ai`: the same with Sonnet 5.5 against the normal AI, for 20 minutes of game time. Allow a few hours: every
  order and every step is a tool call.

`wc3-llm` plays with any chat model agent-env's model endpoint serves: the `prompt_agent` step's `model`, or
`agent-env run --model`. `agent-env wc3 setup --agent` builds and registers it with the other agents.

`rts_grade` (rubric `melee`) grades a game: a win counts three times as much as not being defeated or outscoring the
AI on the game's own score total (an undecided game is no win: `rts_grade`'s `at_time_limit` is `draw`), and two gates zero the grade:
the game stopped working, or the agent gave no orders (or the harness played part of its game). `smoke` is graded
by the `smoke` rubric.

- `macro-micro-quick`, `macro-micro` and `macro-micro-realtime`: the `wc3-macro-micro` agent (wc3env's wc3agent:
  a macro model plans, a micro model controls the army) plays through the env's `urn:rts` session, Haiku 4.5 for both
  models against the easy AI for 5 minutes, then Sonnet 5.5 macro with Haiku 4.5 micro against the normal AI, for 20
  minutes stepped or 10 in realtime. `WC3_MICRO_MODEL` in the `deploy_agent` step picks the micro model (`jev` for
  TypeSafe's Jev); the `prompt_agent` model is the macro model. `agent-env wc3 setup --agent` registers the agent.

Every task plays its match out before grading it (`finish_match`: the game runs to its end once its agents have
stopped). Each player is a player slot of the env's lobby: `open_lobby` opens it, an `add_player_slot` step fills a
player slot per player (an agent, or the game's AI), and `close_lobby` creates the game and its match.
`broadcast-smoke` plays two `wc3-scripted` agents against each other for two minutes with a recorded broadcast
(`rts_broadcast`, live before the first move, and `save_rts_broadcast`), at no model cost.

Every task also saves the spectator recording (`save_rts_recording`): an MP4 of the map, a self-contained HTML
replay and the timeline as JSON; `macro-micro-realtime` adds the game's own video, with chapters, and a highlight reel
cut from it.
`agent-env wc3 setup --fake` builds the env on wc3env's fake game, so every task runs end to end without
Warcraft III (units move and stop, nothing else).

## Drills

The `drill-*` tasks are wc3agent's 25 scenarios as ordinary tasks, with no scenario concept: player slots in a short stepped
match on Echo Isles (seed 1), a stage step (`apply_server_config` with `urn:wc3:stage/v1`) that spawns the drill's
units at named places (`home`, `toward:nearest_camp:900`, `camp:9`, ...) under handles (`army`, `hero`, `enemy`, which
the summary's `army_kept_percent` and `enemy_army_destroyed_percent` read), the drill's goal as the prompt, and
`rts_grade` with the scenario's own checks (`rubric: checks`). The opponent is the game's AI, paused by the stage in
all but the full game, or `wc3-scripted`, an agent seat that attack-moves at your army (`attack`) or at your workers,
else your hall (`raid`). `wc3-macro-micro` plays your side, Haiku 4.5 for both models.

| Task | Skill | Minutes | Opponent | Dropped |
|---|---|---|---|---|
| `drill-build-production` | Second Barracks and a Blacksmith | 4 | computer, paused | |
| `drill-build-repair` | Repairing a battered base | 2.5 | computer, paused | |
| `drill-build-supply` | Farms before the food cap (Human) | 3 | computer, paused | |
| `drill-build-supply-nightelf` | Moon Wells before the food cap (Night Elf) | 3 | computer, paused | |
| `drill-build-supply-orc` | Burrows before the food cap (Orc) | 3 | computer, paused | |
| `drill-build-supply-undead` | Ziggurats before the food cap (Undead) | 3 | computer, paused | |
| `drill-build-towers` | Towers before the raid | 4.5 | `wc3-scripted`: raid after 150 s | |
| `drill-creep-easy` | Creeping the nearest weak camp | 2.5 | computer, paused | finish at `camp_cleared`, then 15 s |
| `drill-creep-hard` | Creeping a strong camp | 3.5 | computer, paused | finish at `camp_cleared:camp:9`, then 15 s |
| `drill-defend-base` | Defending the base from a raid | 2.5 | `wc3-scripted`: attack | |
| `drill-defend-base-human` | Defending with Militia (Human) | 2.5 | `wc3-scripted`: raid | |
| `drill-defend-base-nightelf` | Defending with Ancients (Night Elf) | 2.5 | `wc3-scripted`: raid | |
| `drill-defend-base-orc` | Defending with Burrows (Orc) | 2.5 | `wc3-scripted`: raid | |
| `drill-defend-base-undead` | Defending under Spirit Towers (Undead) | 2.5 | `wc3-scripted`: raid | |
| `drill-expansion` | Taking a second gold mine | 5 | computer, paused | finish at `expansion_started_time` |
| `drill-fight-even` | Even fight | 2.5 | `wc3-scripted`: attack | |
| `drill-fight-outnumbered` | Outnumbered: save the army | 2 | `wc3-scripted`: attack | |
| `drill-full-game-easy` | Ten minutes against the easy computer | 10 | easy computer | |
| `drill-hero-in-danger` | A hero about to die | 1.5 | `wc3-scripted`: attack | finish at `hero_alive == false` |
| `drill-hero-revive` | Reviving a fallen hero | 3 | computer, paused | |
| `drill-loot` | Picking up items and tomes | 1.5 | computer, paused | finish at `items_picked_up >= 4`, then 15 s |
| `drill-opening` | Opening build | 4 | computer, paused | |
| `drill-scouting` | Finding the enemy base | 3 | computer, paused | finish at `enemy_base_seen_time` |
| `drill-shopping` | Buying items | 2, after a 460 s warm-up | computer, paused | finish at `items_bought >= 3` |
| `drill-spend-and-tech` | Spending a big bank and teching | 4 | computer, paused | |

Run one with `agent-env run wc3 --task drill-fight-even`. Every drill needs `wc3-macro-micro` (`agent-env wc3 setup
--agent`); the ones against `wc3-scripted` need that agent registered too. A drill's time limit is its minutes plus
any warm-up: `drill-shopping` lets 460 game seconds pass first so the shops stock up.

**Dropped: the early finish.** wc3agent ended a scenario once its `finish` condition held (and, with
`finish_after_seconds`, a little later, so loot could drop). Drills play to their time limit instead, one stop rule
for every task, and the summary's first-time metrics record when a goal was met, so the checks lose nothing. wc3agent's
`seconds` was the time its finish condition first held, so `drill-creep-easy`'s `seconds <= 90` reads
`camp_cleared_time`, when the camp was first cleared.
Also dropped: wc3agent pointed the game's camera at the staged party (the stage has no camera op), and left the
opponent's race to the map (the drills give it Orc).

`agent-env wc3 drills import [--wc3env PATH]` regenerates the files from wc3env's pinned commit
(`agentenv_wc3/drills.py`), and `tests/test_drills.py` checks that they are what it writes. In `drill-build-repair`
each damaged building has a handle of its own (`base`, `base-2`, ...), since an `hp` op sets every unit under its
handle.
