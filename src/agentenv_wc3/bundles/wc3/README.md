Warcraft III (wc3env): agents play melee games on Echo Isles against the game's own AI or each other, short
drills, and mirror duels, through the env's MCP tools or raw wc3env orders. Each game is graded per player, and its replay and recording
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

`rts_grade` (rubric `outcome`) grades a game on its outcome: a win 1, a draw 0.5, a loss 0, where a game nobody has
won by the time limit is a draw. The game's score, army and kills are reported beside it with no weight. A gate zeroes
the grade when the agent gave no orders, and a match with no outcome (cancelled, failed, or its engine down) is void:
the grade step fails rather than reading as a loss. A full game's play step tolerates a failed agent, so its game is
still played out and graded. `smoke` is graded by the `smoke` rubric.

- `macro-micro-quick`, `macro-micro` and `macro-micro-realtime`: the `wc3-macro-micro` agent (wc3env's wc3agent:
  a macro model plans, a micro model controls the army) plays through the env's `urn:rts` session, Haiku 4.5 for both
  models against the easy AI for 5 minutes, then Sonnet 5.5 macro with Haiku 4.5 micro against the normal AI, for 20
  minutes stepped or 10 in realtime. `WC3_MICRO_MODEL` in the `deploy_agent` step picks the micro model (`jev` for
  TypeSafe's Jev); the `prompt_agent` model is the macro model. `agent-env wc3 setup --agent` registers the agent.

Every task plays its match out before grading it (`finish_match`: the game runs to its end once its agents have
stopped). Each player is a player slot of the env's lobby: `open_lobby` opens it, an `add_player_slot` step fills a
player slot per player (an agent, or the game's AI), and `close_lobby` creates the game and its match.
`broadcast-smoke` plays two `wc3-scripted` agents against each other for two minutes, in the game's own picture,
with a recorded broadcast (agentenv-game-env's `start_broadcast`, live before the first move, and `save_broadcast`),
at no model cost.

Every task also saves the match's files (`save_match_files`): the native replay, an MP4 of the map, a self-contained
HTML replay and the timeline as JSON; `macro-micro-realtime` adds the game's own video, with chapters, and a highlight
reel cut from it.
`agent-env wc3 setup --fake` builds the env on wc3env's fake game, so every task runs end to end without
Warcraft III (units move and stop, nothing else).

## Drills

The `drill-*` tasks are wc3agent's 25 scenarios as ordinary tasks, with no scenario concept: player slots in a short stepped
match on Echo Isles (seed 1), a stage step (`apply_server_config` with `urn:wc3:stage/v1`) that spawns the drill's
units at named places (`home`, `toward:nearest_camp:900`, `camp:9`, ...) under handles (`army`, `hero`, `enemy`, which
the summary's `army_kept_percent` and `enemy_army_destroyed_percent` read), the drill's goal as the prompt, and
`rts_grade` with the scenario's own checks (`rubric: checks`). The opponent is the game's AI, paused by the stage in
all but the full game, or `wc3-scripted`, an agent that attack-moves at your army (`attack`) or at your workers,
else your hall (`raid`). `wc3-llm` plays your side with Haiku 4.5, the drill's goal followed by how to play through
the tools (as the full games' prompt has it, for the drill's race).

Each drill tests one of seven skills, and the bundle has an eval per skill (`evals/drills-<skill>.toml`):
`agent-env run wc3 --eval drills-combat` plays that skill's drills. The evals share no drill, so `agent-env run wc3`
with neither a task nor an eval plays every drill once. To compare models on them, sweep them
(`sweeps/drills.toml` in the repo).

| Skill | Drills |
|---|---|
| economy | `build-supply` (and its `-orc`, `-undead`, `-nightelf`), `build-production`, `spend-and-tech`, `opening`, `build-repair` |
| map-control | `expansion`, `scouting` |
| defence | `defend-base` (and its `-human`, `-orc`, `-undead`, `-nightelf`), `build-towers` |
| combat | `fight-even`, `fight-outnumbered`, `hero-in-danger` |
| creeping | `creep-easy`, `creep-hard` |
| hero-and-items | `hero-revive`, `loot`, `shopping` |
| full-game | `full-game-easy` |

| Task | What it asks | Minutes | Opponent | Ends early |
|---|---|---|---|---|
| `drill-build-production` | Second Barracks and a Blacksmith | 4 | computer, paused | |
| `drill-build-repair` | Repairing a battered base | 2.5 | computer, paused | |
| `drill-build-supply` | Farms before the food cap (Human) | 3 | computer, paused | |
| `drill-build-supply-nightelf` | Moon Wells before the food cap (Night Elf) | 3 | computer, paused | |
| `drill-build-supply-orc` | Burrows before the food cap (Orc) | 3 | computer, paused | |
| `drill-build-supply-undead` | Ziggurats before the food cap (Undead) | 3 | computer, paused | |
| `drill-build-towers` | Towers before the raid | 4.5 | `wc3-scripted`: raid after 150 s | |
| `drill-creep-easy` | Creeping the nearest weak camp | 2.5 | computer, paused | at `camp_cleared`, then 15 s |
| `drill-creep-hard` | Creeping a strong camp | 3.5 | computer, paused | at `camp_cleared:camp:9`, then 15 s |
| `drill-defend-base` | Defending the base from a raid | 2.5 | `wc3-scripted`: attack | |
| `drill-defend-base-human` | Defending with Militia (Human) | 2.5 | `wc3-scripted`: raid | |
| `drill-defend-base-nightelf` | Defending with Ancients (Night Elf) | 2.5 | `wc3-scripted`: raid | |
| `drill-defend-base-orc` | Defending with Burrows (Orc) | 2.5 | `wc3-scripted`: raid | |
| `drill-defend-base-undead` | Defending under Spirit Towers (Undead) | 2.5 | `wc3-scripted`: raid | |
| `drill-expansion` | Taking a second gold mine | 5 | computer, paused | at `expansion_started_time` |
| `drill-fight-even` | Even fight | 2.5 | `wc3-scripted`: attack | |
| `drill-fight-outnumbered` | Outnumbered: save the army | 2 | `wc3-scripted`: attack | |
| `drill-full-game-easy` | Ten minutes against the easy computer | 10 | easy computer | |
| `drill-hero-in-danger` | A hero about to die | 1.5 | `wc3-scripted`: attack | at `hero_alive == false` |
| `drill-hero-revive` | Reviving a fallen hero | 3 | computer, paused | |
| `drill-loot` | Picking up items and tomes | 1.5 | computer, paused | at `items_picked_up >= 4`, then 15 s |
| `drill-opening` | Opening build | 4 | computer, paused | |
| `drill-scouting` | Finding the enemy base | 3 | computer, paused | at `enemy_base_seen_time` |
| `drill-shopping` | Buying items | 2, after a 460 s warm-up | computer, paused | at `items_bought >= 3` |
| `drill-spend-and-tech` | Spending a big bank and teching | 4 | computer, paused | |

Run one with `agent-env run wc3 --task drill-fight-even`. Every drill needs `wc3-llm` (`agent-env wc3 setup
--agent`); the ones against `wc3-scripted` need that agent registered too. A drill's time limit is its minutes plus
any warm-up: `drill-shopping` lets 460 game seconds pass first so the shops stock up.

**The early finish.** wc3agent ends a scenario once its `finish` condition holds (and, with `finish_after_seconds`,
a little later, so loot can drop). A drill does the same: the condition is its match's `finish`, which ends the match
there, every agent's result `finished`, so its checks read the game as wc3agent's did. wc3agent's `seconds` is the
time its finish condition first held, so `drill-creep-easy`'s `seconds <= 90` reads `camp_cleared_time`, when the camp
was first cleared. Dropped: wc3agent pointed the game's camera at the staged party (the stage has no camera op), and
left the opponent's race to the map (the drills give it Orc).

## Mirror duels

The `mirror-*` tasks are wc3agent's duels (its `duel.py`): two identical armies of one race, about 60 food with two
heroes (levels 5 and 3), melee, ranged, casters and siege, staged as mirror images either side of the middle of the
starts (the stage's `formation`). Both players own every upgrade of their race, start with full mana, learn the same
skills (`learn`) and cast on their own (the player slots' `autocast`, and the stage's `autocast`); the creeps in sight
are removed (`clear`). The opponent fights the way Warcraft does: `wc3-scripted` attack-moves its army at the other
one every 3 seconds. The match is decided once one army is down to 40% of the other's strength (`decide_ratio`), a
win and a loss; after 150 seconds it is a draw. `rts_grade` grades the outcome, and reports how much of each army
was kept.

| Task | Your side | Opponent |
|---|---|---|
| `mirror-human`, `mirror-orc`, `mirror-undead`, `mirror-nightelf` | `wc3-llm` with Haiku 4.5 (any model, by `--model` or a sweep) | `wc3-scripted`, attack |
| `mirror-<race>-baseline` | `wc3-scripted`, attack: Warcraft against Warcraft | `wc3-scripted`, attack |

The `duels` eval plays the four races with a model; `sweeps/duels.toml` in the repo crosses them over models and
seeds (a seed nudges every unit and shuffles the starts, and both armies stay mirror images). `agentenv_wc3/duels.py`
writes the tasks, and `tests/test_drills.py` checks that they are what it writes.

`agent-env wc3 drills import [--wc3env PATH]` regenerates the files from wc3env's pinned commit
(`agentenv_wc3/drills.py`), and `tests/test_drills.py` checks that they are what it writes. In `drill-build-repair`
each damaged building has a handle of its own (`base`, `base-2`, ...), since an `hp` op sets every unit under its
handle.
