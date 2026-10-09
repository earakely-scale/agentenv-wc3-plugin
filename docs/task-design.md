# Task design

How Warcraft III tasks are built. Phases 1 to 3 below are implemented and were run end to end on the real game
(2026-10-05): `smoke` (1.0), `drill-fight-even` (0.6), `drill-creep-easy` (0.67) and `duel-quick` (0.41 and 0.35, one
score per player, under the first full-game rubric, which phase 8 replaced). Phase 4, the task-set generator, is `agent-env wc3 sweep` (README, Sweeps).

## Principles

1. **One step, one concern.** The lobby says who plays; the match is the game; the agent steps are the players; a
   staging step sets up a drill; grading is judgement; recording and streaming are for spectators.
2. **The env reports facts, never judgement.** Grading reads the env's end-of-game summary and applies a rubric from
   the task.
3. **Everything is configurable in the task's JSON**, with defaults that keep today's tasks as they are.
4. **No special cases.** A full game, an agent-versus-agent match and a two-minute drill are the same kind of task,
   built from player slots, an optional staging step and a rubric. There is no scenario concept: wc3agent's 25
   scenarios become 25 drill tasks built from these primitives.
5. **Game-agnostic where it can be.** `rts_grade`, the recording, the summary contract and the scripted opponent's
   session live in `agentenv_rts`; the lobby, the match, the spectator view, the match's files, the broadcast and
   their steps (`open_lobby`, `add_player_slot`, `close_lobby`, `finish_match`, `save_match_files`,
   `start_broadcast`, `save_broadcast`) in agentenv-game-env; the license, the staging extension and the replay are
   Warcraft III's.

## A task's steps

```
deploy_env
 ├─ deploy_agent (one per agent, the scripted opponent included)
 ├─ add_license (the activation files, from agent-env's secret store)
 └─ open_lobby (opens the lobby)
     └─ add_player_slot (one per player; an agent's also waits for its deploy_agent)
         └─ close_lobby (after every player slot and the license: the game and its match are created)
             └─ stage (optional: apply_server_config → urn:wc3:stage/v1)
                 └─ prompt_agent (one per agent, in parallel; after start_broadcast if the task has one)
                     └─ finish_match ─ rts_grade ─ save_match_files
```

| Step | From | Status |
|---|---|---|
| `deploy_env` | agent-env | as today |
| `deploy_agent` | agent-env | as today, with `"env_ids": []`: its player slot gives it its address |
| `add_license`, `open_lobby`, `add_player_slot`, `close_lobby` | [agentenv-game-env](https://github.com/earakely-scale/agentenv-game-env) | give the game its license (the env declares its activation files as two `file` parts); open the lobby with the match's settings; fill it one player slot at a time; close it, which creates the game and its match |
| `apply_server_config` with `urn:wc3:stage/v1` | agent-env step, plugin extension | stages the game before play |
| `prompt_agent` | agent-env | as today; the prompt carries a drill's goal |
| `finish_match` | agentenv-game-env | plays the match out to its end once its agents have stopped |
| `rts_grade` | plugin (`agentenv_rts`) | done; replaced `env_outcome_verifier` with `wc3-verifier` |
| `save_match_files` | agentenv-game-env | keeps the match's files: `replay`, `map_video`, `html_replay`, `timeline`, and on request `client_video` and `highlights` |
| `start_broadcast`, `save_broadcast` | agentenv-game-env | streams or records the match's spectator view (`/live?view`) under the overlay: live before the prompts, its video kept after `finish_match` |

## The game's settings: `open_lobby`'s `game_settings`

The env's `MatchSettings` model checks them and publishes them as a JSON Schema in the card's lobby `open` request.

| Setting | Values | Default | Notes |
|---|---|---|---|
| `map` | a stock map | `(2)EchoIsles.w3x` | any of the installation's maps plays; 16 have prepared spectator data |
| `seed` | int or `null` | `null`: a new game each run | tasks pin it for reproducibility |
| `randomize_starts` | bool | `false` | |
| `mode` | `stepping`, `realtime` | `stepping` | |
| `step_ms` | 25-60000 | `1000` | game time per program step |
| `time_limit_seconds` | 60-14400 | `1200` | |
| `client_view` | bool | `false` | the game draws itself for spectators; its clock then runs at the game's own speed (a stepped game without it runs at 2048x) |
| `lockstep` | `{stall_seconds}` | `{600}` | how long the match waits for a silent agent at its start, and, stepping with several agents, at each turn before it steps that agent with no orders, flagged |
| `allow_debug` | bool | `false` | lets an agent's own session stage the game (research only; staging steps don't need it) |
| `finish` | `{metric, op, value, after_seconds}` or `null` | `null` | the match ends once the first agent's metric holds, as wc3agent ends a scenario; every agent `finished`, a draw |
| `decide_ratio` | 0-1 or `null` | `null` | a staged fight ends once one army (handles `army`, `enemy`) is down to this share of the other's strength: a win and a loss |

## Who plays: the lobby's player slots

Who plays is the env's lobby (`urn:game:lobby/v1`, agentenv-game-env): `open_lobby` opens it with the settings above,
an `add_player_slot` step fills each player slot, and `close_lobby` closes it, which creates the game and its match.
A player slot is:

- **`player_id`:** the game's player number, `"0"` to one less than the map's start locations. A match has no more
  player slots than those.
- **`player_kind` and `player_name`:** `agent` with the `deploy_agent`'s `agent_name`, or `ai` for the game's AI.
- **`game_settings`** (the env's `SlotSettings`, published in the card's `fill` request):
  - `faction`: `human`, `orc`, `undead`, `night_elf` or `random` (default `random`);
  - `team`: 1 to 12 (default: every player its own team, a free-for-all; equal numbers are allies);
  - `label`: the player's name for spectators;
  - agents only: `ai_assist` (the game's own AI keeps playing beside the agent, wc3env's `ai_agents`) and
    `omniscient` (it sees every player's units, for harness opponents);
  - the AI only: `ai_level`, `easy`, `normal` or `insane`, one for every AI player slot (the game has one AI level).

The lobby's `player_teams` groups the player slots by `team`. A scripted opponent is an agent like any other (see
Drills). An env whose lobby was never opened (`agent-env wc3 serve`, the env's own tests) plays its default game: an
agent at the env's own address against the game's AI.

**Player addresses:**
- **One address per agent.** `add_player_slot` reads the player slot's env card at `<env>/players/<player_id>` and
  registers its MCP interface, `<env>/players/<player_id>/mcp`, with the agent. The card also lists the `urn:rts`
  session there. Under that address the env serves the MCP tools and the session for that player only, and each sees
  only its own observation. Program agents need no player variable.
- **Lockstep.** In stepping mode with several agents, a step from one agent waits until every agent has stepped, then
  the game moves on. Realtime needs no barrier.

**The briefing:** `get_state` (and `session_observe`) open with the player slot, race, team, allies and enemies, the
map and the time limit, so a prompt need not repeat the matchup.

## The match: `urn:game:match/v1`

The match runs from its start gate to its end, and its `get` says how it is going: its `status` (`not_started`,
`started`, `finished`, `cancelled` or `failed`), the game's words for how it ended, its clock (`game` seconds against
the time limit, with a `rate`), and each player slot's `status` (`not_ready`, `ready`, then `undecided`, `won`, `lost`
or `drawn`) and scores (`score`, `units_killed`, `army`).

- **The start:** each agent's first move makes it ready, and the match starts once every agent is ready, with all
  their opening orders; the game's AI starts ready. An agent with no first move within `lockstep.stall_seconds` is
  started without, and the match's `status_detail` says so.
- **The end:** by the game's rules (every building of the other side destroyed), at the time limit, or by
  `finish_match`, which plays the match out with no more orders from its agents (they wait on the game, then find it
  over). `cancel_match` ends it where it stands; its agents' moves then change nothing.
- **For the record:** `finish_match` keeps the final match in the run's `metadata["game_match"]`, and `rts_grade` adds
  a row of information saying how it ended.

**Not in the match:** the rubric, which counts a game nobody won by its time limit as a draw (`rts_grade`), a drill's
staging (the stage step) and its goal (the prompt).

## Staging: `urn:wc3:stage/v1`

A harness extension, called from an `apply_server_config` step after `close_lobby` and before play. It runs a list of
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
  `ai`, `invulnerable`, `alliance`, `destructable`; and the env's, for wc3agent's duels: `formation` (an army in rows
  facing the other start, mirrored for either player by one seed), `learn` (heroes spend their skill points by one
  rule for every player), `autocast` (every autocast ability on) and `clear` (the creeps any player sees removed).
- **Players** are named by agent, by player number, or by position (`opponent` is the other side in a two-player
  game).
- **Places** are named the way wc3agent names them, relative to the player's start, so a drill works from either start
  location: `home`, `enemy_home`, `middle`, `nearest_camp`, `camp:<n>`, `building:<name>`, `toward:<place>:<distance>`, plus
  `dx`/`dy`. The env resolves them from the map's prepared data.
- **Handles:** `"as": "army"` names the units an op made; later ops take them (`"unit": "hero"`), and the summary
  reports metrics on them (the army kept, the enemy destroyed).
- **`warmup_seconds`:** let the game run that long first (wc3agent's `skip_seconds`, so shops stock up).
- **Who may stage:** the harness, through this extension. An agent's own session still can't stage unless the match
  has `allow_debug`. Harness time (staging and warm-up) doesn't count as the agent's play.

## Drills (what wc3agent calls scenarios)

A drill is an ordinary task: a short match, a stage step, an opponent, a goal in the prompt, and a `checks` rubric.
wc3agent's four opponent kinds map to player slots:

| wc3agent opponent | Player slot |
|---|---|
| `computer` | the game's AI |
| `idle` | the game's AI, with `{"op": "ai", "paused": true}` in the staging |
| `attack`, `raid` | an agent played by `wc3-scripted` |

**`wc3-scripted`**, the scripted opponent agent (`agents/wc3-scripted`), plays its player slot through the `urn:rts`
session:

| Setting (`deploy_agent` `env_vars`) | Values | Default |
|---|---|---|
| `SCRIPT` | `attack` (attack-move at the other side's army), `raid` (at its workers, else its hall), `idle` | `attack` |
| `SCRIPT_AFTER_SECONDS` | game seconds before its first order | `0` |
| `SCRIPT_EVERY_SECONDS` | how often it renews its orders | `5` |

It works in any match, not only drills: a raiding opponent beside the game's AI, for example.

**No early finish.** Drills run to their time limit (1.5-5 minutes), and `finish_match` plays out what the agents
leave. The "first time" metrics record when a goal was met, so nothing is lost.

**The goal** is the prompt: the drill's goal, then how to play through the tools for the drill's race (`prompts.py`).
The drills are played by `wc3-llm`; `wc3-macro-micro` can play one too, with `WC3_GOAL=prompt`, which pins the prompt
as wc3agent's goal.

**The 25:** `agent-env wc3 drills import` converts wc3agent's definitions once into 25 task files in the bundle
(`drill-fight-even`, `drill-defend-base-orc`, ...), which we own from then on.

`fight_even` as a drill task is
[`drill-fight-even.json`](../src/agentenv_wc3/bundles/wc3/tasks/drill-fight-even.json):

1. `deploy_env`, then two `deploy_agent`s, both with `"env_ids": []` (each player slot gives its agent its address):
   the player, `wc3-llm`, and the attacker, `wc3-scripted` with `SCRIPT=attack`.
2. `add_license`, and `open_lobby`: 150 game seconds, stepping (so the two play in lockstep). Then a player slot
   each, `wc3` as `"0"`, `human`, and `opponent` as `"1"`, `orc` with `omniscient`, and `close_lobby`.
3. `stage`: an `apply_server_config` step calling `urn:wc3:stage/v1` with the hero, its level and potion, the army
   and the enemy army, by named places and handles.
4. A `prompt_agent` per agent: the player's prompt is the drill's goal; the attacker's is ignored (an A2A agent
   plays only when prompted). Both tolerate a failure, so a drill whose agent fails is still played out and graded.
5. `finish_match`, then `rts_grade` with `rubric: checks` and wc3agent's five checks, then the replay and the
   recording.

On the real game, played by `wc3-macro-micro` before the drills moved to `wc3-llm`, Haiku 4.5 (macro and micro)
destroyed the enemy army and kept its hero alive but lost 7 units and kept 31% of its army's strength: 3 of the 5
checks, 0.6.

## The env's end-of-game summary (`data/get`, what `rts_grade` reads)

```json
{"game_over": true, "game_time_seconds": 600, "time_limit_seconds": 600, "map": "...", "seed": 7, "mode": "stepping",
 "engine_failed": false, "error": null,
 "harness": {"orders_sent": 412, "finish_seconds": 0, "staged_seconds": 0, "...": "..."},
 "player_slots": [{"player_id": "0", "player_kind": "agent", "player_name": "wc3", "faction": "human", "team": 1,
                   "result": "time_limit", "stalls": 0,
                   "metrics": {"total": 30690, "army": 2715, "units_killed": 41, "...": "..."}}],
 "handles": {"army": {"slot": 0, "staged": 8, "alive": 5}, "enemy": {"slot": 1, "staged": 8, "alive": 0}}}
```

Today's top-level fields (`result`, `score`, `opponent_score`, ...) stay for compatibility.

**Metrics** each player slot reports (wc3agent's names, so its checks carry over):

| Kind | Metrics |
|---|---|
| State at the end | `total` (the game's score), `army` (its value), `workers`, `count:<type>`, `hero_alive`, `hero_level`, `hero_health_percent`, `items_carried`, `structure_health_percent`, `unspent_skill_points`, `tier`, `expansions` |
| Counts over the game | `units_trained`, `units_killed`, `units_lost`, `workers_lost`, `structures_lost`, `buildings_destroyed`, `gold_mined`, `lumber_total`, `items_picked_up`, `items_used`, `items_bought`, `researches_done` |
| When it first happened (game seconds) | `first_time:<type>`, `hero_time`, `expansion_started_time`, `upgrade_started_time`, `enemy_base_seen_time` |
| Over time | `idle_worker_seconds`, `supply_blocked_seconds`, `average_unspent_gold`, `present_seconds:<type>`, `uprooted_seconds`, `fewest_workers` |
| On handles | `army_kept_percent` (the player's `army` and `hero`), `enemy_army_destroyed_percent` (`enemy`), `camp_cleared`, `camp_cleared:camp:<n>` |

## `rts_grade`: judgement

| Field | Values | Default |
|---|---|---|
| `env_id` | string | required |
| `player_names` | agent names | every agent |
| `rubric` | `outcome`, `dense`, `checks`, `smoke` | `outcome` |
| `weights` | `{criterion: weight}`; 0 drops one | the preset's |
| `targets` | `{criterion: full-credit value}` | the preset's |
| `checks` | `[{metric, op, value, weight}]` | for `checks`; added to any rubric |
| `gates` | `game_ran`, `agent_played` | both |
| `verifier_id` | string | the step's id |

**Criteria:**

| Criterion | Credit | Rubrics | Weight |
|---|---|---|---|
| `outcome` | a win 1, a draw 0.5, a loss 0; a game nobody won by the time limit is a draw | outcome, dense | 1 |
| `outscore` | my score / the best opponent's, at most 1 | dense | 1 |
| `army_ratio` | my army / the strongest enemy's, toward a target | dense | 1 |
| `kills_ratio` | units killed / (killed + lost) | dense | 1 |
| `buildings_destroyed` | the share of the enemy's buildings | dense | 1 |
| `tier` | tier reached / 3 | dense | 1 |
| `expansions` | expansions / the target (1) | dense | 1 |
| `hero_level` | the highest hero level / the target (5) | dense | 1 |
| each check | a metric against a value | checks | 1 each |
| `ran_to_limit`, `played_out` | the game ran to its limit; `finish_match` played it out | smoke | 1 |
| gates `game_ran`, `agent_played` | failing zeroes the player's grade | all | -100 |

Under `outcome`, rows with no weight report the score, army and kills against the enemies'. A rubric with `outcome`
raises `VoidMatch` for a match that has none (cancelled, failed, or its engine down): the step fails, and the run
with it, so a void match never reads as a loss. A full game's play steps set `fail_task_on_error: false`, so an agent
that fails mid-game has its game played out and graded.

**Output:** one verification per graded player, `context.metadata["verifications"]["<verifier_id>:<agent>"] =
{results, score}`, scored with agent-env's `aggregate_score`, so `agent-env run`, evals and the hub read it as they
read `env_outcome_verifier`'s. Every criterion records its evidence (`army 2715 vs 2190`), and a row of information
(no weight) says how the match ended.

## Variety

1. **In the task:** every field above.
2. **Per run:** `open_lobby`, the stage extension and `rts_grade` honour agent-env's
   `user_overrides.step_params.<step id>`, so a runner changes the seed, map or opponent without new tasks.
3. **Generated sets:** `agent-env wc3 sweep generate SPEC OUT` crosses a template task over the models, maps, races,
   opponents and seeds a TOML spec names into named tasks (`first-eval-gpt-5.4-mini-terenasstand-s2`) and an eval of
   them; `sweep run` plays them under a spend budget and `sweep report` tabulates them: full games by outcome, AI
   level (the ladder: the highest level beaten, three in four games won) and map, drills by skill. A template may be
   a glob of bundled tasks (`drill-*`), and an axis the spec leaves out keeps each template's own.

## More examples

**Agent versus the game's AI** (today's `macro-micro-quick`):

```json
{"id": "match", "type": "open_lobby", "env_id": "wc3", "game_settings": {"seed": 1, "time_limit_seconds": 300}},
{"id": "slot-wc3", "type": "add_player_slot", "env_id": "wc3", "player_id": "0", "player_kind": "agent",
 "player_name": "wc3", "game_settings": {"faction": "human"}, "depends_on": ["match", "agent"]},
{"id": "slot-ai", "type": "add_player_slot", "env_id": "wc3", "player_id": "1", "player_kind": "ai",
 "game_settings": {"faction": "orc", "ai_level": "easy"}, "depends_on": ["match"]},
{"id": "start", "type": "close_lobby", "env_id": "wc3", "depends_on": ["slot-wc3", "slot-ai"]}
```

**Agent versus agent**, two MCP agents in lockstep:

```json
{"id": "match", "type": "open_lobby", "env_id": "wc3",
 "game_settings": {"map": "(2)TerenasStand.w3x", "seed": 3, "time_limit_seconds": 1200}},
{"id": "slot-claude", "type": "add_player_slot", "env_id": "wc3", "player_id": "0", "player_kind": "agent",
 "player_name": "claude", "game_settings": {"faction": "human"}, "depends_on": ["match", "agent-claude"]},
{"id": "slot-codex", "type": "add_player_slot", "env_id": "wc3", "player_id": "1", "player_kind": "agent",
 "player_name": "codex", "game_settings": {"faction": "orc"}, "depends_on": ["match", "agent-codex"]},
{"id": "start", "type": "close_lobby", "env_id": "wc3", "depends_on": ["slot-claude", "slot-codex"]}
```

with a `prompt_agent` step per agent, after `start`, then `finish_match` and `{"type": "rts_grade", "rubric":
"dense"}` grading both.

**2v2** on `(4)TurtleRock.w3x`: two agents on team 1 against two of the game's AIs on team 2.

## Validation

- **As each player slot fills,** the lobby refuses:
  - a `player_id` the map doesn't have, and more player slots than the map has players;
  - a second player slot for one agent;
  - a faction, team or setting the game doesn't take, or one for the other kind of player;
  - an AI level unlike the other AI player slots' (wc3env has one setting for the game).

  `add_player_slot` refuses an agent's player slot that names no deployed agent.
- **When the lobby closes,** the match needs an agent.
- A stage step's players, handles and places resolve; its ops are the hook's; it comes after `close_lobby`.
- `rts_grade`'s `player_names` are agents; a check's metric is one the summary reports.
- `client_view` on the fake game warns: it draws nothing.

## Compatibility

`rts_grade`'s `smoke` gives the grades `smoke-verifier` gave, with `finish_match` in place of the idle extension.
Its first full-game rubric, `melee`, which gave the grades `wc3-verifier` gave, blended the outcome with the game's
score (a win 3, reaching the end, surviving and outscoring 1 each), so a game nobody won scored 0.33 to 0.50; the
`outcome` rubric replaced it, and `at_time_limit` with it. The players moved from the old `wc3_match` step (`seats`, and its
shorthand `race`, `opponent_race`, `ai_difficulty` and `labels`) to the lobby, and its match settings to
agentenv-game-env's `open_lobby`. Its license is agentenv-game-env's `add_license`, which replaced `wc3_license`. A
one-agent game's verification keeps its name (`<verifier_id>`); several agents' are `<verifier_id>:<agent>`.

## Verified, and still open

- **Player addresses:** agent-env's MCP-configuration extension can only add a server, not replace one. So agents
  deploy with `"env_ids": []`, and `add_player_slot` registers each one's player slot address. An agent that also
  has the env's own address is refused.
- **Teams:** the `alliance` op makes teammates allies (passive, help, shared experience, spells and vision). They show
  as allies from the game's first step on: its very first observation still lists them as enemies, so the metrics read
  each step's relations, never an earlier one's.
- **Shared victory:** the game lets allies win together only with the lobby's allied victory, which alliances made
  after the start don't give, so the env scores a player as a win once every player on the other teams is defeated.
  Verified on the real game, with two agents against two of the game's AIs: one agent's buildings were destroyed, then
  all of team 2's; both agents won and the game ended. A teammate whose buildings are gone plays on while its ally's
  stand, as in melee.
- **Determinism**, verified on the real game in stepping mode. The setup: two agents in lockstep and two of the
  game's AIs, with staged armies that fight, over 91 steps. The same seed gave the same observations at every step and
  the same end state for all four players, both in one container and in a fresh one. Another seed differed from the
  first step. Realtime games are not expected to repeat.
- **The start:** an agent's first move says it is ready, and the match starts once every agent is ready, with all
  their opening orders. An agent with no first move within lockstep's `stall_seconds` doesn't hold the start.
  Stepping games already worked this way, since time passes only as the agents step. A realtime game is now created
  held at its start, using the hook's `hold` and `release` from `patches/wc3env-realtime-hold.patch`; without the
  patch, it starts when created.
  Verified on the real game with two agents and the game's AI:
  - with one agent ready, the clock, the AI and that agent's units stood still for 5 s;
  - when the second moved, the game started at once and both openings ran.

  One agent at the root starts the game with its first move.
- **Free-for-all:** a player that is out (it has a result) is done: its own session says so, its agent stops, and
  lockstep goes on without it; the game is over once every agent has a result. Verified on the real game with three
  agents on three teams. Player 0 (the game's own local player) was knocked out first and the other two played on.
  When the third was knocked out, the second won and the game ended.
- **No auth on harness extensions:** an agent can reach the env's lobby, match and stage extensions over HTTP. Say
  so, or bind them to the harness's own address.

## Phases

| Phase | What | Status |
|---|---|---|
| 1 | Players (and the shorthand), per-player addresses, lockstep, the briefing, per-run overrides; `rts_grade` with `melee` and `smoke`; the six tasks moved to it | done; `duel-quick` ran on the real game |
| 2 | The summary's per-player metrics; `dense`, and `checks` on any rubric | done |
| 3 | Drills: the stage extension, `wc3-scripted`, the goal from the prompt (`WC3_GOAL=prompt`), the gate's harness time, the 25 imported | done; `drill-fight-even` and `drill-creep-easy` ran on the real game |
| 4 | The task-set generator and a sweep eval; `rts_broadcast` | done; `first-eval` ran on the real game |
| 5 | Players as the lobby's slots: `AgentEnvGameEnv` and `urn:game:lobby/v1` from agentenv-game-env, `add_player_slot`; `rts_seat_agents` and the shorthand removed | done |
| 6 | agentenv-game-env 0.3.0: player slots by `player_id`, settings as models, each player slot's env card; the match (`urn:game:match/v1`: its start gate, `finish_match`, each player's outcome and scores) in place of `rts_finish` and the hold, finish and idle extensions; `rts_broadcast` live before the prompts, with `save_rts_broadcast` | done |
| 7 | agentenv-game-env 0.4.0: the spectator view (`/live?view`) and the match's files (`@match_files`) in place of `urn:rts:recording/v1` and `urn:wc3:replay/v1`; `start_broadcast`, `save_broadcast` and `save_match_files` in place of the plugin's streamer, casters and steps | done |
| 8 | Grading on outcomes: the `outcome` rubric in place of `melee`, void matches, the ladder over the AI's levels in sweeps; drills by skill, played by `wc3-llm`, in sweeps | done; run on the real game |
| 9 | Parity with wc3env's runtime and wc3agent: wc3agent priced by LiteLLM, its prompt cache and cost cap, its report and session kept (`urn:rts:file/v1`, `agent_files`); the replay's startup options; the clock at 2048x; `autocast`; heroes' items in `get_state`; wc3agent's strength; drills that end where its scenarios end (`finish`); its mirror duels (`mirror-*`, `formation`, `learn`, `autocast`, `clear`, `decide_ratio`) | done; run on the real game |

## Decisions taken

1. The default `seed` stays `null` (a new game each run); tasks pin it.
2. A player slot's default `faction` is `random`.
3. Each player sees only its own observation; `omniscient` is for harness opponents.
4. MCP prompts can be generic: the briefing opens `get_state`. The existing prompts keep their matchup and opening.
5. A game is graded on its outcome: a win 1, a draw 0.5, a loss 0. A game nobody won by its time limit is a draw (a
   win is a conquest), and the game's score is evidence, not grade. A match with no outcome is void, not a loss.
6. Every match is played out (`finish_match`) before it is graded; a forfeit is not a game event.
