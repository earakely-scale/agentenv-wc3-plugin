# Task design

How Warcraft III tasks are built. Phases 1 to 3 below are implemented and were run end to end on the real game
(2026-10-05): `smoke` (1.0), `drill-fight-even` (0.6), `drill-creep-easy` (0.67) and `duel-quick` (0.41 and 0.35, one
score per seat); phase 4, the task-set generator, is not built yet.

## Principles

1. **One step, one concern.** The match is the game; the agent steps are the players; a staging step sets up a
   drill; grading is judgement; recording and streaming are for spectators.
2. **The env reports facts, never judgement.** Grading reads the env's end-of-game summary and applies a rubric from
   the task.
3. **Everything is configurable in the task's JSON**, with defaults that keep today's tasks as they are.
4. **No special cases.** A full game, an agent-versus-agent match and a two-minute drill are the same kind of task,
   built from seats, an optional staging step and a rubric. There is no scenario concept: wc3agent's 25 scenarios
   become 25 drill tasks built from these primitives.
5. **Game-agnostic where it can be.** `rts_grade`, `save_rts_recording`, the summary contract and the scripted
   opponent's session live in `agentenv_rts`; `wc3_match`, the staging extension and the replay are Warcraft III's.

## A task's steps

```
deploy_env ─┬─ deploy_agent (one per agent seat, the scripted opponent included) ─┐
            └──────────────────────────────────────────────────────────────────── wc3_match
                                                                                     │
                                         stage (optional: apply_server_config → urn:wc3:stage/v1)
                                                                                     │
                                          prompt_agent (one per agent seat, in parallel)
                                                                                     │
                                                         rts_grade ─┬─ save_wc3_replay
                                                                    └─ save_rts_recording
```

| Step | From | Status |
|---|---|---|
| `deploy_env` | agent-env | as today |
| `deploy_agent` | agent-env | as today; `agent_name` names the seat it plays |
| `wc3_match` | plugin | extended: seats, lockstep, `step_ms` |
| `apply_server_config` with `urn:wc3:stage/v1` | agent-env step, plugin extension | new extension: stage the game before play |
| `prompt_agent` | agent-env | as today; the prompt carries a drill's goal |
| `rts_grade` | plugin (`agentenv_rts`) | done; replaced `env_outcome_verifier` with `wc3-verifier` |
| `save_wc3_replay` | plugin | as today |
| `save_rts_recording` | plugin (`agentenv_rts`) | as today: `mp4`, `html`, `client`, `highlights` |
| broadcast | `agent-env wc3 stream` | as today; an `rts_broadcast` step later, optionally |

## `wc3_match`: the game

| Field | Values | Default | Notes |
|---|---|---|---|
| `env_id` | string | required | |
| `map` | a stock map | `(2)EchoIsles.w3x` | any of the installation's maps plays; 16 have prepared spectator data |
| `seed` | int or `null` | `null`: a new game each run | tasks pin it for reproducibility |
| `randomize_starts` | bool | `false` | |
| `mode` | `stepping`, `realtime` | `stepping` | |
| `step_ms` | 25-60000 | `1000` | game time per program step |
| `time_limit_seconds` | 60-14400 | `1200` | |
| `seats` | list (below) | the shorthand's two | no more than the map's players |
| `client_view` | bool | `false` | the game draws itself for spectators; stepping about 3x slower |
| `lockstep` | `{stall_seconds}` | `{600}` | stepping with several agents: a seat that stalls this long is stepped with no orders, and flagged |
| `allow_debug` | bool | `false` | lets an agent's own session stage the game (research only; staging steps don't need it) |
| `license_dir`, `timeout_seconds` | | as today | operational |
| shorthand `race`, `opponent_race`, `ai_difficulty` | as today | | one agent seat and one computer seat |

**A seat** is `{"agent": "<deploy_agent's agent_name>"}` or `{"computer": "easy" | "normal" | "insane"}`, with:
- `race`: `human`, `orc`, `undead`, `night_elf` or `random` (default `random`);
- `team`: an int (default: every seat its own team, a free-for-all; equal numbers are allies);
- `ai_assist` (agent seats only): the game's own AI keeps playing beside the agent (wc3env's `ai_agents`);
- `slot`: optional; seats are numbered in order.

A scripted opponent is an agent seat like any other (see Drills).

**Seat routing:**
- **One address per agent seat.** The match points each agent's MCP configuration at `<env>/seats/<agent>/mcp`. Under
  that address the env serves the MCP tools and the `urn:rts` session for that seat only, and each seat sees only its
  own observation. Program agents need no seat variable.
- **Lockstep.** In stepping mode with several agent seats, a step from one seat waits until every agent seat has
  stepped, then the game moves on. Realtime needs no barrier.

**The briefing:** `get_state` (and `session_observe`) open with the seat, race, team, allies and enemies, the map
and the time limit, so a prompt need not repeat the matchup.

**Not in the match:** display names (they go with the broadcast; `labels` stays accepted for now), the rubric and the
time-limit tiebreak (`rts_grade`), a drill's staging (the stage step) and its goal (the prompt).

## Staging: `urn:wc3:stage/v1`

A harness extension, called from an `apply_server_config` step after `wc3_match` and before play. It runs a list of
the hook's staging ops in order, with two things the raw ops lack: **named places** and **handles**.

```json
{"warmup_seconds": 0, "ops": [
  {"op": "ai", "player": "opponent", "paused": true},
  {"op": "resources", "player": "wc3", "gold": 1500, "lumber": 800},
  {"op": "spawn", "player": "wc3", "type": "Hamg", "at": "home", "dx": -1300, "as": "hero"},
  {"op": "level", "unit": "hero", "level": 3},
  {"op": "give", "unit": "hero", "type": "phea"},
  {"op": "spawn", "player": "wc3", "type": "hfoo", "n": 5, "at": "home", "dx": -1150, "as": "army"},
  {"op": "spawn", "player": "attacker", "type": "ogru", "n": 4, "at": "toward:nearest_camp:700", "as": "enemy"}]}
```

- **Ops:** the hook's own: `spawn`, `kill`, `remove`, `level`, `research`, `give`, `hp`, `mana`, `item`, `resources`,
  `ai`, `invulnerable`, `alliance`, `destructable`.
- **Players** are seats, named by agent or by position (`opponent` is the other side in a two-seat game).
- **Places** are named the way wc3agent names them, relative to the player's start, so a drill works from either start
  location: `home`, `enemy_home`, `nearest_camp`, `camp:<n>`, `building:<name>`, `toward:<place>:<distance>`, plus
  `dx`/`dy`. The env resolves them from the map's prepared data.
- **Handles:** `"as": "army"` names the units an op made; later ops take them (`"unit": "hero"`), and the summary
  reports metrics on them (the army kept, the enemy destroyed).
- **`warmup_seconds`:** let the game run that long first (wc3agent's `skip_seconds`, so shops stock up).
- **Who may stage:** the harness, through this extension. An agent's own session still can't stage unless the match
  has `allow_debug`. Harness time (staging and warm-up) doesn't count against the "agent played" gate.

## Drills (what wc3agent calls scenarios)

A drill is an ordinary task: a short match, a stage step, an opponent seat, a goal in the prompt, and a `checks`
rubric. wc3agent's four opponent kinds map to seats:

| wc3agent opponent | Seat |
|---|---|
| `computer` | a computer seat |
| `idle` | a computer seat, with `{"op": "ai", "paused": true}` in the staging |
| `attack`, `raid` | an agent seat played by `wc3-scripted` |

**`wc3-scripted`**, the scripted opponent agent (`agents/wc3-scripted`), plays its seat through the `urn:rts` session:

| Setting (`deploy_agent` `env_vars`) | Values | Default |
|---|---|---|
| `SCRIPT` | `attack` (attack-move at the other side's army), `raid` (at its workers, else its hall), `idle` | `attack` |
| `SCRIPT_AFTER_SECONDS` | game seconds before its first order | `0` |
| `SCRIPT_EVERY_SECONDS` | how often it renews its orders | `5` |

It works in any match, not only drills: a raiding opponent beside the game's AI, for example.

**No early finish.** Drills run to their time limit (1.5-5 minutes). The "first time" metrics record when a goal
was met, so nothing is lost.

**The goal** is the prompt. `wc3-macro-micro` passes its prompt to wc3agent as the goal (today it ignores it).

**The 25:** `agent-env wc3 drills import` converts wc3agent's definitions once into 25 task files in the bundle
(`drill-fight-even`, `drill-defend-base-orc`, ...), which we own from then on.

`fight_even` as a drill task is
[`drill-fight-even.json`](../src/agentenv_wc3/bundles/wc3/tasks/drill-fight-even.json):

1. `deploy_env`, then two `deploy_agent`s, both with `"env_ids": []` (the match gives each its seat): the player,
   `wc3-macro-micro` with `WC3_GOAL=prompt`, and the attacker, `wc3-scripted` with `SCRIPT=attack`.
2. `wc3_match`: `seats: [{"agent": "wc3", "race": "human"}, {"agent": "opponent", "race": "orc", "omniscient": true}]`,
   150 game seconds, stepping (so the two play in lockstep).
3. `stage`: an `apply_server_config` step calling `urn:wc3:stage/v1` with the hero, its level and potion, the army
   and the enemy army, by named places and handles.
4. A `prompt_agent` per agent: the player's prompt is the drill's goal; the attacker's is ignored (an A2A agent
   plays only when prompted).
5. `rts_grade` with `rubric: checks` and wc3agent's five checks, then the replay and the recording.

On the real game Haiku 4.5 (macro and micro) destroyed the enemy army and kept its hero alive but lost 7 units and
kept 31% of its army's strength: 3 of the 5 checks, 0.6.

## The env's end-of-game summary (`data/get`, what `rts_grade` reads)

```json
{"game_over": true, "game_time_seconds": 600, "time_limit_seconds": 600, "map": "...", "seed": 7, "mode": "stepping",
 "engine_failed": false, "error": null,
 "harness": {"orders_sent": 412, "idle_seconds": 0, "staged_seconds": 0, "...": "..."},
 "seats": [{"slot": 0, "agent": "wc3", "computer": null, "race": "human", "team": 1, "result": "time_limit",
            "stalls": 0, "metrics": {"total": 30690, "army": 2715, "units_killed": 41, "...": "..."}}],
 "handles": {"army": {"slot": 0, "staged": 8, "alive": 5}, "enemy": {"slot": 1, "staged": 8, "alive": 0}}}
```

Today's top-level fields (`result`, `score`, `opponent_score`, ...) stay for compatibility.

**Metrics** each seat reports (wc3agent's names, so its checks carry over):

| Kind | Metrics |
|---|---|
| State at the end | `total` (the game's score), `army` (its value), `workers`, `count:<type>`, `hero_alive`, `hero_level`, `hero_health_percent`, `items_carried`, `structure_health_percent`, `unspent_skill_points`, `tier`, `expansions` |
| Counts over the game | `units_trained`, `units_killed`, `units_lost`, `workers_lost`, `structures_lost`, `buildings_destroyed`, `gold_mined`, `lumber_total`, `items_picked_up`, `items_used`, `items_bought`, `researches_done` |
| When it first happened (game seconds) | `first_time:<type>`, `hero_time`, `expansion_started_time`, `upgrade_started_time`, `enemy_base_seen_time` |
| Over time | `idle_worker_seconds`, `supply_blocked_seconds`, `average_unspent_gold`, `present_seconds:<type>`, `uprooted_seconds`, `fewest_workers` |
| On handles | `army_kept_percent` (the seat's `army` and `hero`), `enemy_army_destroyed_percent` (`enemy`), `camp_cleared`, `camp_cleared:camp:<n>` |

## `rts_grade`: judgement

| Field | Values | Default |
|---|---|---|
| `env_id` | string | required |
| `seats` | agent names | every agent seat but scripted ones |
| `rubric` | `melee`, `dense`, `checks`, `smoke` | `melee` |
| `weights` | `{criterion: weight}`; 0 drops one | the preset's |
| `targets` | `{criterion: full-credit value}` | the preset's |
| `checks` | `[{metric, op, value, weight}]` | for `checks`; added to any rubric |
| `at_time_limit` | `score`, `draw`, `loss` | `draw`: a win is a conquest; a lead in score earns `outscore` |
| `gates` | `game_ran`, `agent_played` | both |
| `verifier_id` | string | the step's id |

**Criteria:**

| Criterion | Credit | Rubrics | Weight |
|---|---|---|---|
| `reached_end` | the game reached a result or the time limit | melee, dense | 1 |
| `win` | won, or won the tiebreak at the limit (per `at_time_limit`) | melee, dense | 3 |
| `survive` | not defeated | melee, dense | 1 |
| `outscore` | my score / the best opponent's, at most 1 | melee, dense | 1 |
| `army_ratio` | my army / the strongest enemy's, toward a target | dense | 1 |
| `kills_ratio` | units killed / (killed + lost) | dense | 1 |
| `buildings_destroyed` | the share of the enemy's buildings | dense | 1 |
| `tier` | tier reached / 3 | dense | 1 |
| `expansions` | expansions / the target (1) | dense | 1 |
| `hero_level` | the highest hero level / the target (5) | dense | 1 |
| each check | a metric against a value | checks | 1 each |
| `ran_to_limit`, `idle_played` | as `smoke-verifier` | smoke | 1 |
| gates `game_ran`, `agent_played` | failing zeroes the seat's grade | all | -100 |

**Output:** one verification per graded seat, `context.metadata["verifications"]["<verifier_id>:<agent>"] =
{results, score}`, scored with agent-env's `aggregate_score`, so `agent-env run`, evals and the hub read it as they
read `env_outcome_verifier`'s. Every criterion records its evidence (`army 2715 vs 2190`).

## Variety

1. **In the task:** every field above.
2. **Per run:** `wc3_match`, the stage extension and `rts_grade` honour agent-env's
   `user_overrides.step_params.<step id>`, so a runner changes the seed, map or opponent without new tasks.
3. **Generated sets:** `agent-env wc3 tasks --maps ... --races ... --opponents easy,normal --seeds 1-10 --drills all
   --agent wc3-macro-micro --out bundles/wc3-sweep` writes named tasks (`human-vs-orc-normal-turtlerock-s3`,
   `drill-fight-even-orc`) and an eval of them.

## More examples

**Agent versus the game's AI** (today's `macro-micro-quick`):

```json
{"id": "match", "type": "wc3_match", "env_id": "wc3", "seed": 1, "time_limit_seconds": 300,
 "seats": [{"agent": "wc3", "race": "human"}, {"computer": "easy", "race": "orc"}]}
```

**Agent versus agent**, two MCP agents in lockstep:

```json
{"id": "match", "type": "wc3_match", "env_id": "wc3", "map": "(2)TerenasStand.w3x", "seed": 3,
 "time_limit_seconds": 1200, "seats": [{"agent": "claude", "race": "human"}, {"agent": "codex", "race": "orc"}],
 "depends_on": ["agent-claude", "agent-codex"]}
```

with a `prompt_agent` step per agent and `{"type": "rts_grade", "rubric": "dense"}` grading both.

**2v2** on `(4)TurtleRock.w3x`: two agent seats on team 1 against two computer seats on team 2.

## Validation (when a task is checked, before anything runs)

- No more seats than the map has players; every agent seat names a deployed agent, and every deployed agent has one.
- All computer seats share one difficulty (wc3env has one setting for the game).
- A stage step's players, handles and places resolve; its ops are the hook's; it comes after the match.
- `rts_grade`'s seats are agent seats; a check's metric is one the summary reports.
- `client_view` on the fake game warns: it draws nothing.

## Compatibility

Today's six tasks run unchanged: the shorthand fields and `labels` still work. They move to `rts_grade`, whose `melee`
and `smoke` give the grades `wc3-verifier` and `smoke-verifier` give (today's 0.5s stay 0.5), and those two scripts
go.

## Verified, and still open

- **Seat addresses:** agent-env's MCP-configuration extension can only add a server, not replace one, so seated
  agents deploy with `"env_ids": []` and `wc3_match` registers each one's seat address; a seated agent that also has
  the env's own address, in a game with several agent seats, is refused.
- **Teams:** the `alliance` op makes teammates allies (passive, help, shared experience, spells and vision). They show
  as allies from the game's first step on: its very first observation still lists them as enemies, so the metrics read
  each step's relations, never an earlier one's. Shared victory is not verified.
- **Determinism** under a fixed seed, with several agents and with staging: not verified.
- **No auth on harness extensions:** an agent can reach the env's stage and idle extensions over HTTP. Say so, or bind
  them to the harness's own address.

## Phases

| Phase | What | Status |
|---|---|---|
| 1 | Seats (and the shorthand), per-seat addresses, lockstep, the briefing, per-run overrides; `rts_grade` with `melee` and `smoke`; the six tasks moved to it | done; `duel-quick` ran on the real game |
| 2 | The summary's per-seat metrics; `dense`, and `checks` on any rubric | done |
| 3 | Drills: the stage extension, `wc3-scripted`, the goal from the prompt (`WC3_GOAL=prompt`), the gate's harness time, the 25 imported | done; `drill-fight-even` and `drill-creep-easy` ran on the real game |
| 4 | The task-set generator and a sweep eval; optionally `rts_broadcast` | not built |

## Decisions taken

1. The default `seed` stays `null` (a new game each run); tasks pin it.
2. A seat's default `race` is `random`; the shorthand keeps `human` against `orc`.
3. Each seat sees only its own observation; `omniscient` is for harness opponents.
4. MCP prompts can be generic: the briefing opens `get_state`. The existing prompts keep their matchup and opening.
5. `rts_grade`'s `at_time_limit` defaults to `draw`: a win is a conquest, and a lead in score earns `outscore`.
