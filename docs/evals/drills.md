# drills: wc3agent's 25 scenarios, one skill each, for three low-cost models

[sweeps/drills.toml](../../sweeps/drills.toml), played on the real game on 2026-10-09 (env v32 to v36; plugin main
`dd1be7a` to `c1d8db7`):
- every `drill-*` task (25), each played by `wc3-llm` with three models: 75 drills;
- a drill is a staged scene of 1.5 to 10 minutes of game time against the AI or the scripted `wc3-scripted`, graded
  on the share of its checks met; it is passed when all are met. Drills end where wc3agent's scenarios end (`finish`);
- a per-drill cap of $0.50 (none reached it), two at a time, 3.3 minutes of wall time a drill and 4.2 hours in all.

Sweep `drills`: 75 drills, $4.08 of model spend.

| Model | Drills | Passed, checks met | Cost a drill | Turns a drill |
|---|---|---|---|---|
| fireworks_ai/deepseek-v4p1-flash | 25 | 11 of 25, 82% | $0.023 | 35 |
| anthropic/claude-haiku-4-5 | 25 | 3 of 25, 62% | $0.107 | 49 |
| openai/gpt-5.4-mini | 25 | 2 of 25, 49% | $0.033 | 21 |

By skill: drills passed, and the share of checks met:

| Model | economy | map control | defence | combat | creeping | hero and items | full game |
|---|---|---|---|---|---|---|---|
| DeepSeek V4.1 Flash | 1 of 8, 76% | 1 of 2, 75% | 4 of 6, 93% | 1 of 3, 64% | 1 of 2, 92% | 3 of 3, 100% | 0 of 1, 50% |
| Haiku 4.5 | 0 of 8, 61% | 1 of 2, 75% | 0 of 6, 66% | 0 of 3, 22% | 0 of 2, 67% | 2 of 3, 89% | 0 of 1, 50% |
| GPT-5.4 mini | 0 of 8, 46% | 1 of 2, 75% | 0 of 6, 50% | 0 of 3, 45% | 0 of 2, 67% | 1 of 3, 33% | 0 of 1, 33% |

| Drill | Skill | DeepSeek V4.1 Flash | Haiku 4.5 | GPT-5.4 mini |
|---|---|---|---|---|
| build-production | economy | 0.67 | 0.67 | 0.33 |
| build-repair | economy | 0.67 | 0.67 | 0.33 |
| build-supply | economy | 0.75 | 0.75 | 0.50 |
| build-supply-nightelf | economy | 0.75 | 0.75 | 0.25 |
| build-supply-orc | economy | 0.75 | 0.50 | 0.75 |
| build-supply-undead | economy | 1.00 | 0.67 | 0.67 |
| build-towers | defence | 1.00 | 0.20 | 0.00 |
| creep-easy | creeping | 0.83 | 0.50 | 0.50 |
| creep-hard | creeping | 1.00 | 0.83 | 0.83 |
| defend-base | defence | 1.00 | 0.75 | 0.75 |
| defend-base-human | defence | 0.83 | 0.67 | 0.50 |
| defend-base-nightelf | defence | 1.00 | 0.80 | 0.80 |
| defend-base-orc | defence | 1.00 | 0.80 | 0.20 |
| defend-base-undead | defence | 0.75 | 0.75 | 0.75 |
| expansion | map control | 1.00 | 1.00 | 1.00 |
| fight-even | combat | 0.60 | 0.40 | 0.60 |
| fight-outnumbered | combat | 0.33 | 0.00 | 0.00 |
| full-game-easy | full game | 0.50 | 0.50 | 0.33 |
| hero-in-danger | combat | 1.00 | 0.25 | 0.75 |
| hero-revive | hero and items | 1.00 | 1.00 | 1.00 |
| loot | hero and items | 1.00 | 0.67 | 0.00 |
| opening | economy | 0.67 | 0.56 | 0.33 |
| scouting | map control | 0.50 | 0.50 | 0.50 |
| shopping | hero and items | 1.00 | 1.00 | 0.00 |
| spend-and-tech | economy | 0.83 | 0.33 | 0.50 |

**The cheapest model plays the drills best.** DeepSeek V4.1 Flash passes 11 drills at $0.02 each, among them four of
the six defence drills; Haiku 4.5 passes 3, GPT-5.4 mini 2. The ranking matches the [ladder](ladder.md), where
DeepSeek lasted longest and killed the most.

**The checks most models miss:**
- `idle_worker_seconds`: a worker standing idle for more than 30 seconds of the drill (90 in the full game). Even
  DeepSeek leaves one idle for 60 to 170 seconds in the build drills.
- `average_unspent_gold`: money banked rather than spent.
- `units_lost` in `defend-base-undead`: all three lose their three Ghouls to the raiders under the Spirit Towers.
- In `scouting` every model finds the enemy base, and the drill ends there, before it has trained the two workers
  the drill also asks for.

**Env bugs these drills found, fixed on 2026-10-09** (in [FD-3806](https://linear.app/scale-epd/issue/FD-3806)):
- `uprooted_seconds` read 0 for every model in `defend-base-nightelf`: the game keeps an uprooted ancient a
  structure. It now counts an ancient while it walks, and DeepSeek passes the drill.
- `drill-shopping` failed to stage: its 460 s warmup went to wc3env as one step, past the 60 s it takes. The warmup
  now runs in steps, and the drill was played again.
- `hero-revive` needs the dead hero's id, which the game's observation drops; `get_state` now lists a dead hero
  with its revive order, and all three models pass.

The overnight run of the same spec (env v28 to v30, before the fixes above) reached 48 drills: DeepSeek met 85% of
their checks, Haiku 68%, GPT-5.4 mini 49%.
