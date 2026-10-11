---
license: apache-2.0
pretty_name: Warcraft III (wc3env) on AgentEnv
language:
- en
task_categories:
- reinforcement-learning
- text-generation
size_categories:
- n<1K
tags:
- agentenv
- rl-environment
- agents
- games
- real-time-strategy
- warcraft-iii
- mcp
configs:
- config_name: ladder_tasks
  default: true
  data_files:
  - split: eval
    path: tasks/ladder.parquet
- config_name: ladder_episodes
  data_files:
  - split: eval
    path: episodes/ladder.parquet
- config_name: drills_tasks
  data_files:
  - split: eval
    path: tasks/drills.parquet
- config_name: drills_episodes
  data_files:
  - split: eval
    path: episodes/drills.parquet
- config_name: duels_tasks
  data_files:
  - split: eval
    path: tasks/duels.parquet
- config_name: duels_episodes
  data_files:
  - split: eval
    path: episodes/duels.parquet
- config_name: duels_references
  data_files:
  - split: eval
    path: references/duels.parquet
agentenv:
  default: wc3-v1-drills
---

# Warcraft III (wc3env) on AgentEnv

[![DeepSeek V4.1 Flash's Undead army meets an identical one played by Warcraft's own attack-move, its plan written over the fight](https://huggingface.co/datasets/earakely-scale/wc3env-AgentEnv/resolve/v0.5.0/assets/hero.webp)](https://huggingface.co/spaces/earakely-scale/wc3env-AgentEnv)

*DeepSeek V4.1 Flash's Undead army meets an identical one played by Warcraft's own attack-move. The plan it wrote at
that moment is over the fight. It wins in 54 seconds, keeping 79% of its army. This is the game's own picture,
rendered from the run's replay. Every run here plays like this in the
[Space](https://huggingface.co/spaces/earakely-scale/wc3env-AgentEnv), with the env's map beside it.*

LLM agents play the real Warcraft III: The Frozen Throne (1.29) through
[wc3env](https://github.com/pwang724/wc3env), as an [AgentEnv](https://github.com/scaleapi/agentenv-framework)
environment: the [agentenv-wc3](https://github.com/earakely-scale/agentenv-wc3-plugin) plugin. Each game is graded
per player. A task is played in one of three **styles**, each its own env or agent:
- **raw** (env `wc3`): one model orders units by id through general tools: it reads the game with `get_state`,
  `list_units` and `lookup`, gives orders with `act` and moves game time on with `advance`.
- **commander** (env `wc3-commander`): one model plays through wc3agent's interface, from wc3agent's own code: named
  units (`peasant1`), a turn page that says what it can and can't do yet, wc3agent's order language (`command`), its
  code reflexes, and its unit menus (`fight`, `choose`).
- **wc3agent** (agent `wc3-macro-micro` on env `wc3`): wc3env's own agent, as Peter Wang built it: a macro model, and
  a micro model answering each unit group's menu.

This dataset holds three v1 task sets and every run of them from 2026-10-09 and 2026-10-10, played on the real game.
Each set is a bundle per style, and a task is the same game in every style:

| Set | Tasks | What the agent does | Graded on | Runs |
|---|---:|---|---|---:|
| drills | 25 | one skill in a staged scene of 1.5 to 10 game minutes: economy, map control, defence, combat, creeping, hero and items, a full game | the share of its checks met; passed when all are | 150 |
| ladder | 12 | a 30-minute game as Human against the game's easy, normal or insane Orc AI, on Echo Isles or Terenas Stand, seed 1 or 2 | the outcome: a win 1, a draw at the time limit 0.5, a loss 0 | 74 |
| duels | 16 | a mirror duel: two identical late-game armies of one race, against Warcraft's own attack-move, seeds 1 to 4 | the outcome, decided once one army is down to 40% of the other's strength | 72 |

| Set | raw | commander | wc3agent |
|---|---|---|---|
| drills | `wc3-v1-drills`: 75 runs | `wc3-v1-drills-commander`: 75 runs | |
| ladder | `wc3-v1-ladder`: 38 runs | `wc3-v1-ladder-commander`: 36 runs | |
| duels | `wc3-v1-duels`: 26 runs | `wc3-v1-duels-commander`: 40 runs | `wc3-v1-duels-wc3agent`: 6 runs |

Every run comes with:
- its video in the game's own picture (all but the six `drill-shopping` runs and one commander duel, see
  [How the videos were made](#how-the-videos-were-made));
- its replay in the browser, with the video beside the env's map;
- its timeline;
- the game's own `.w3g` replay;
- the agent's own record: from env v34, `wc3-llm`'s untrimmed transcript; for wc3agent's seed-2 duels, its report
  and session.

 **[Watch them in the Space](https://huggingface.co/spaces/earakely-scale/wc3env-AgentEnv).** The dataset and the Space make up the
collection [wc3env on AgentEnv](https://huggingface.co/collections/earakely-scale/wc3env-on-agentenv-6ac924a8810f02942599a5a4).

**Bring your own game.** Warcraft III is a trademark of Blizzard Entertainment, and the videos show its picture.
This dataset holds no game files or activation files, and isn't affiliated with or endorsed by Blizzard. The `.w3g`
replays need your own game to watch. To play a task you need your own Warcraft
III: Reforged (it includes the classic game) and an x86-64 Linux host; see [Play a task](#play-a-task).

## Results

The same three low-cost models played every set in the raw and commander styles, and wc3agent played six of the
duels. The plugin's [styles](https://github.com/earakely-scale/agentenv-wc3-plugin/blob/v0.5.0/docs/evals/styles.md)
reads the styles side by side.

**The drills** (passed, of 25):

| Model | Raw | Commander | Checks met, raw | Checks met, commander | Cost a drill, raw | Cost a drill, commander |
|---|---:|---:|---:|---:|---:|---:|
| DeepSeek V4.1 Flash | 11 | 11 | 82% | 79% | $0.02 | $0.03 |
| Claude Haiku 4.5 | 3 | 9 | 62% | 74% | $0.11 | $0.13 |
| GPT-5.4 mini | 2 | 7 | 49% | 60% | $0.03 | $0.05 |

The commander style lifts the two weaker models. Its page says what can be built now and what is still short, and
its order language builds, trains and gathers in a line each. It does worse in the combat drills (a mean reward of
0.23 against 0.44): it has no micro model, and the models seldom take a fight over. On the raw tools the cheapest
model plays the drills best, and the checks most models miss are `idle_worker_seconds` (a worker idle for more than
30 seconds) and `average_unspent_gold`.

**The ladder** (Human against the Orc AI, 30-minute games; each model played all 12 games in each style):

| Model | Won / drawn / lost, raw | Won / drawn / lost, commander | Score share, raw | Score share, commander | Cost a game, raw | Cost a game, commander |
|---|---|---|---:|---:|---:|---:|
| DeepSeek V4.1 Flash | 0 / 1 / 11 | 0 / 2 / 10 | 26% | 31% | $0.10 | $0.12 |
| Claude Haiku 4.5 | 0 / 0 / 12 | 0 / 0 / 12 | 21% | 24% | $0.34 | $0.44 |
| GPT-5.4 mini | 0 / 0 / 12 | 0 / 1 / 11 | 19% | 18% | $0.12 | $0.12 |

No model beats any level in either style, not even the easy AI. On the raw tools the low-cost models bank their
money, rarely cut lumber, and send armies of four to seven Footmen at the AI's base. In the commander style they last
longer and kill more: DeepSeek 28 units a game against 12, and a draw against the normal AI. Claude Sonnet 5.5 drew
and Claude Opus 5.5 lost at 27.5 minutes, each in one raw game against the normal AI on Echo Isles ($1.23 and
$3.14): they spend what they mine and act on the env's feedback, but fall behind after 12 to 18 minutes.

**The duels:**

| Player 0 | Style | Duels | Points | Won / drawn / lost | Cost a duel |
|---|---|---:|---:|---|---:|
| DeepSeek V4.1 Flash | raw | 9 | 0.33 | 3 / 0 / 6 | $0.07 |
| DeepSeek V4.1 Flash | commander | 14 | 0.36 | 5 / 0 / 9 | $0.13 |
| DeepSeek V4.1 Flash, Claude Haiku 4.5 micro | wc3agent | 6 | 0.67 | 4 / 0 / 2 | $1.85 |
| Claude Haiku 4.5 | raw | 9 | 0.28 | 2 / 1 / 6 | $0.31 |
| Claude Haiku 4.5 | commander | 13 | 0.23 | 3 / 0 / 10 | $0.43 |
| GPT-5.4 mini | raw | 8 | 0.00 | 0 / 0 / 8 | $0.14 |
| GPT-5.4 mini | commander | 13 | 0.46 | 6 / 0 / 7 | $0.10 |
| Warcraft's own attack-move (`duels_references`) | | 24 | 0.63 | 14 / 2 / 8 | $0 |

The budgets played 26 of the 48 raw duels and 40 of the commander ones. On the 26 both played, GPT-5.4 mini won 5
against 0: in the commander style it groups its whole army and attack-moves it at the enemy base, as Warcraft does.
DeepSeek won 3 against 3, and Haiku 1 against 2 and a draw. wc3agent, the only player with a model for the fights,
wins the most, at $1.85 a duel against DeepSeek's $0.07 raw and $0.13 commander. Read the duels with two caveats,
both in [Limitations](#limitations): six wins came from razing the opponent's only building, and 13 commander duels
ended early.

The plugin's [docs/evals](https://github.com/earakely-scale/agentenv-wc3-plugin/tree/v0.5.0/docs/evals) read these
runs game by game: [ladder](https://github.com/earakely-scale/agentenv-wc3-plugin/blob/v0.5.0/docs/evals/ladder.md),
[drills](https://github.com/earakely-scale/agentenv-wc3-plugin/blob/v0.5.0/docs/evals/drills.md),
[duels](https://github.com/earakely-scale/agentenv-wc3-plugin/blob/v0.5.0/docs/evals/duels.md),
[frontier](https://github.com/earakely-scale/agentenv-wc3-plugin/blob/v0.5.0/docs/evals/frontier.md) and
[styles](https://github.com/earakely-scale/agentenv-wc3-plugin/blob/v0.5.0/docs/evals/styles.md).

## Tables

Every config has one split, `eval`. Two kinds of tables share the repo:

- **WC3's own:** what each task stages and checks, and each run's outcome, score and cost.
- **agentenv-hf's,** one pair per bundle, as `agent-env hf publish` writes them: the tasks' steps and prompts, and each
  run's record and chat transcript (`messages`).

A run has the same `episode_id` in both, so you can join them. WC3's tables are one pair per set, with every style's
runs: a task is the same game in every style, so `<set>_tasks` has one row per task, and each `<set>_episodes` row
says who played it: `bundle`, `style` (`raw`, `commander` or `wc3agent`), `agent`, `model` (wc3agent's macro model),
`micro_model` (wc3agent's) and `max_cost_usd`, the cap it played under. Each of WC3's episode rows, and each reference
duel, also names its files: `video`, `replay`, `timeline`, `w3g` and `transcript` (empty when the run has none).
`video_in_sync` says whether the run's playback stayed in step with the game. A playback that drifted isn't kept:
that run has no `video`, and its replay shows the env's map alone.

| Config | Rows | One row is |
|---|---:|---|
| `ladder_tasks` | 12 | a ladder game: map, seed, the AI's level and race, time limit |
| `ladder_episodes` | 74 | a run: who played it, outcome, reward, score against the AI's, units killed, orders and orders refused, turns, game seconds, cost |
| `drills_tasks` | 25 | a drill: its skill, opponent, time limit and checks (`metric`, `op`, `value`) |
| `drills_episodes` | 150 | a run: who played it, each check met or not with what was measured, the share met, passed, turns, cost |
| `duels_tasks` | 16 | a duel: race, seed, time limit, the strength ratio that decides it |
| `duels_episodes` | 72 | a run: who played it, outcome, score share, the share of its army it kept and of the enemy's it destroyed, cost |
| `duels_references` | 24 | a Warcraft-against-Warcraft duel, from player 0's side: outcome, score share, its files (seeds 1 to 6; `task` names the v1 duel it mirrors) |
| `<bundle>_tasks` | 25, 12 or 16 | a bundle task: its steps and prompt (agentenv-hf), for each of the seven bundles |
| `<bundle>_episodes` | its runs | a run: reward, scores, verifications, `messages` (`wc3-llm`'s chat transcript; none for wc3agent), tool calls (agentenv-hf) |

Beside the tables, each run's files are under its `episode_id` (`<bundle>/<run>`, or `duels-references/<run>`):

| Folder | Runs | What a file is |
|---|---:|---|
| `videos/` | 313 | the game's own picture as an MP4 (960×540), rendered from the run's replay: the camera follows the agent's fights and key moments, and its plans show in the game as it wrote them. Ladder games play at 8×, drills at 2×, duels at the game's pace. |
| `replays/` | 320 | the replay in a browser: one HTML file with the whole game in it. It plays the run's video beside the env's map (every unit any player sees), the event feed, the agent's plans and both sides' momentum, scrubbable. It finds the video at `../../videos/`, as in this repo. |
| `timelines/` | 320 | the same game as JSON: every frame's units, events and notes, to analyse or redraw a game without playing it |
| `w3g/` | 320 | the game's own replay (`.w3g`), which plays in Warcraft III 1.29, with its startup options (`.w3g.json`). wc3env saves those without the AI level when it is easy (0), and a playback then fields the normal AI. These files have it back. |
| `transcripts/` | 254 | `wc3-llm`'s untrimmed transcript: every message and tool call, where `messages` holds what the model was sent (from env v34) |
| `reports/` | 2 | wc3agent's own report of a duel (HTML), for its two seed-2 duels |
| `sessions/` | 2 | wc3agent's session of a duel (zip): its model calls, actions, system prompt, pinned goal, transcript and summary |

And:
- `bundles/<bundle>/`: the bundles that `agent-env hf run` plays.
- `raw/<bundle>.jsonl`: each run's record and trajectory, as agentenv-hf writes them.
- `runs/<sweep>/`: each sweep's spec (`sweep.json`), its results (`results.jsonl`) and its report.
- `assets/`: the clip at the top of this card, and the Space's thumbnail.

```python
from datasets import load_dataset

repo, rev = "earakely-scale/wc3env-AgentEnv", "v0.5.0"
episodes = load_dataset(repo, "ladder_episodes", split="eval", revision=rev).to_pandas()
transcripts = load_dataset(repo, "wc3-v1-ladder_episodes", split="eval", revision=rev).to_pandas()
games = episodes.merge(transcripts[["episode_id", "messages"]], on="episode_id")
print(games.groupby("model")[["reward", "score_share", "cost_usd"]].mean())
```

A run's files by the paths in its row:

```python
import json
from huggingface_hub import hf_hub_download

run = games.iloc[0]
timeline = json.load(open(hf_hub_download(repo, run["timeline"], repo_type="dataset", revision=rev)))
replay = hf_hub_download(repo, run["replay"], repo_type="dataset", revision=rev)   # open it in a browser
```

## Play a task

You need:
- **Warcraft III: Reforged** on Battle.net. In the Battle.net app on Windows, choose **Warcraft III - Legacy TFT 1.29**
  in the Game Version dropdown and install it, then copy its folder (about 1.2 GB) to the Linux host.
- **An x86-64 Linux host** with Docker. The game runs under Wine; Apple Silicon can't run it.
- **A model endpoint** for agent-env: `[model]` in `.agentenv/config.toml`, or `LITELLM_BASE_URL` and
  `LITELLM_API_KEY`.

```bash
pip install "agentenv-wc3 @ git+https://github.com/earakely-scale/agentenv-wc3-plugin@v0.5.0"
agent-env wc3 build-worker "<game folder>"     # the worker image from your copy, about five minutes
agent-env wc3 license import "<game folder>"   # your activation files, into agent-env's secret store
agent-env wc3 setup --agent                    # the env as "wc3", and the agents
agent-env hf run earakely-scale/wc3env-AgentEnv@v0.5.0 --task drill-opening --model anthropic/claude-haiku-4-5
```

`agent-env hf run` downloads the bundle at that revision and checks that the plugin is installed and that the env and
agents are set up, before it plays. It plays `wc3-v1-drills` unless `--bundle` names another. A style's bundles need
its env or agent: `agent-env wc3 setup --style commander` builds and registers `wc3-commander`, and `setup --agent`
registers `wc3-macro-micro` with the other agents.

```bash
agent-env hf run earakely-scale/wc3env-AgentEnv@v0.5.0 --bundle wc3-v1-ladder \
    --task ladder-echoisles-vs-easy-orc-s1 --model <your model>
agent-env hf run earakely-scale/wc3env-AgentEnv@v0.5.0 --bundle wc3-v1-duels --eval duels --model <your model>
agent-env hf run earakely-scale/wc3env-AgentEnv@v0.5.0 --bundle wc3-v1-drills-commander --eval drills-economy \
    --model <your model>
agent-env hf run earakely-scale/wc3env-AgentEnv@v0.5.0 --bundle wc3-v1-duels-wc3agent --task mirror-orc-s1
```

Without `--model`, a task plays Claude Haiku 4.5; `wc3-v1-duels-wc3agent`'s play DeepSeek V4.1 Flash as wc3agent's
macro model, as its runs did, with Claude Haiku 4.5 as its micro. `--dry-run` shows what would run, and `--yes`
skips the question.
The plugin's [README](https://github.com/earakely-scale/agentenv-wc3-plugin/tree/v0.5.0#play-the-real-game-x86-64-linux)
has the setup in full. It also covers watching a game live and getting its video and replay, and `agent-env wc3
sweep` for running a task set across models.

## How the runs were made

Each set was played as a sweep, by `wc3-llm`, the plugin's agent (any chat model, with the style's tools), or by
`wc3-macro-micro` (wc3agent). The sweeps ran two or three games at a time on one Linux host, under a spend budget.
`runs/` has each sweep's spec, results and report.

| Sweep | Bundle | Players | Per-game cap |
|---|---|---|---:|
| `drills-final` | `wc3-v1-drills` | DeepSeek V4.1 Flash, Haiku 4.5, GPT-5.4 mini | $0.50 |
| `drills-commander` | `wc3-v1-drills-commander` | the same three | $0.50 |
| `ladder-final` | `wc3-v1-ladder` | the same three | $2 |
| `ladder-commander` | `wc3-v1-ladder-commander` | the same three | $2 |
| `frontier-final` | `wc3-v1-ladder` (one task) | Claude Sonnet 5.5, Claude Opus 5.5 | $5 |
| `duels-final` | `wc3-v1-duels` | DeepSeek V4.1 Flash, Haiku 4.5, GPT-5.4 mini | $0.50 |
| `duels-commander` | `wc3-v1-duels-commander` | the same three | $0.50 |
| `duels-macro-micro` | `wc3-v1-duels-wc3agent` | wc3agent: DeepSeek V4.1 Flash, with Claude Haiku 4.5 as its micro | $4 |
| `duel-baselines-final` | `duels_references` | `wc3-scripted` in both player slots | $0 |

The raw env changed during 2026-10-09 (plugin env versions v32 to v36). The fixes report why the game dropped an
order, keep a dead hero in view, and check each order on its own; each fix reached the games started after it. On
2026-10-10 the commander sweeps played `wc3-commander` v1, and wc3agent's duels the raw env v37 (seed 1) and v38
(seed 2), which play them alike. A sweep's task names carry the model (`ladder-gpt-5.4-mini-echoisles-vs-easy-orc-s1`);
here each run is filed under its v1 task.

## How the videos were made

None of these games was played with the game's picture on, since drawing makes stepping about three times slower.
Each video was rendered afterwards from the run's replay with `agent-env wc3 render`, in the env's own image with the
picture on:
- **The replay plays the game again.** wc3env plays the `.w3g` back: the players' orders and the AI come from the
  recording.
- **Staging is applied again.** A drill's or a duel's setup (its armies, levels and mana) was made with debug
  commands, which a replay doesn't record. The render applies the task's staging again before the playback, step for
  step as the live game did, and leaves the staging's orders (a hero's skills) to the recording.
- **The camera follows the agent** with the env's director, as a live game's picture does: its fights first, then its
  key moments (a hero, a tier, an expansion, a building lost), then its army.
- **The agent's plans show in the game** at the game time the agent wrote them.
- **One frame per fixed slice of game time,** at 20 frames a second: 400 ms a frame for the ladder (8×), 100 ms for
  drills (2×), 50 ms for duels (1×).

**Every published video is checked against the game.** Its playback must end on the score the live game's own
timeline last showed for the run's lead player; both are read from the game's observations, so the check is exact.
313 of the 314 rendered do. A playback that drifted is not published, since it would show a game the agent didn't
play.
- **The AI level, fixed.** Six playbacks first drifted, all of games where the easy AI was set: wc3env saves a
  replay's startup without an AI level of 0, so they played back against the normal AI. With the level restored from
  the task, all six end in sync.
- **One playback drifts, cause unknown.** Commander Haiku's orc duel on seed 3 plays back to the same victory but
  ends half a second early, at 31.65 game seconds against 32.15, with 26,528 points against the live game's 26,535.
  A second render ended the same way. Its replay page shows the env's map alone.
- **The render's own record.** `videos/<run>.mp4.json`, next to each video (and for the drifted run, alone), has the
  render's pace and final scores.

**The six `drill-shopping` runs have no video,** three in each style. Their staging lets the game run 460 seconds
first, a minute at a time, and the game plays those minute-long steps of a replay back short: about 422 seconds
instead of 460. The playback therefore can't reach the drill's start in step with the game that was played, so they
weren't rendered. Their replay pages show the env's map alone.

## Limitations

- **Few runs.** One game per model and task, so a model's ladder record is 12 games, and the frontier models played
  one ladder task each. The raw duels stopped at 26 of 48 and the commander duels at 40, and wc3agent played six
  duels: seed 1 of each race and seed 2 of the human and night elf mirrors.
- **A duel can be won by the base.** On odd seeds each army starts nearer the enemy's base than the enemy army is.
  Six wins (five commander, one raw, all in the orc mirror) came from attack-moving at the opponent's Great Hall, its
  only building: the hall fell, and Warcraft's melee rule defeated the player with no buildings. In five of them the
  enemy army was untouched. The duel's goal asks for the enemy army, but the grade counts the game's result.
- **Some players stopped early.** In 13 commander duels the player stopped giving orders more than 15 game seconds
  before the end, and lost each. Haiku reached the $0.50 cap in 8 of its 13: each commander turn returns the whole
  page. DeepSeek's turns came back empty: `wc3-llm` allowed 4,096 output tokens a turn, DeepSeek's reasoning ran past
  them before it called a tool, and the agent stopped at a game's fourth reply without a tool call. Plugin v0.5.0
  allows 32,768 tokens and stops only at the fourth in a row; these runs were played before.
- **The commander style has no micro model.** A group fights by Warcraft's attack-move unless the model takes the
  fight over with `fight` and `choose`, which the models seldom do.
- **wc3agent's micro is Haiku, not Jev.** wc3agent's own micro model needs a TypeSafe key. With Haiku a duel costs
  about $1.85 (Haiku costs about $0.03 a game second), so a 30-minute ladder game would cost about $50, and wc3agent
  played duels only. Its four seed-1 duels were played before a fix: it kept running after the env decided the duel,
  so their report and session weren't kept.
- **Duels on Echo Isles.** wc3agent ran its duels on a flat arena built from Echo Isles, which needs Windows tools to
  build. Here the armies meet in Echo Isles' middle, with the creeps in sight cleared and odd seeds swapping the
  sides. Warcraft against Warcraft scores 15 of 24 for player 0 in these games, and 59% over the 48 since the
  staging fix.
- **A draw is the time limit.** A ladder game nobody wins in 30 minutes is a draw (0.5), however far behind.
- **Cost caps need a LiteLLM proxy.** `wc3-llm` prices each call from LiteLLM's `x-litellm-response-cost` header.
  Through another endpoint it can't see spend, so its cap never ends a game; set a limit at the endpoint.

## Licence and credits

The tasks, records and tables are Apache-2.0, like the plugin. The transcripts are the models' outputs.

- [wc3env](https://github.com/pwang724/wc3env) (MIT), by Peter Wang: the game under Wine, the hook that observes and
  orders it, and the fake game the plugin's tests use.
- wc3agent (MIT), in wc3env's repository: the scenarios the drills are built from, the mirror duel and the heroes'
  skill builds; the commander style's interface (its pages, order language, reflexes and unit menus), and the agent
  that `wc3-macro-micro` runs.

This dataset and its Space are built by
[`scripts/hub_dataset.py`](https://github.com/earakely-scale/agentenv-wc3-plugin/blob/v0.5.0/scripts/hub_dataset.py)
in the plugin's repository, from the sweeps' folders and the agent-env store they wrote to.
