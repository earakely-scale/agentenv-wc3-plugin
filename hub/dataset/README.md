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

[![DeepSeek V4.1 Flash (red) wins an Undead mirror duel against Warcraft's own attack-move (blue) in 54 seconds, keeping 79% of its army](https://huggingface.co/datasets/earakely-scale/wc3env-AgentEnv/resolve/v0.3.0/assets/hero.webp)](https://huggingface.co/spaces/earakely-scale/wc3env-AgentEnv)

*DeepSeek V4.1 Flash (red) wins an Undead mirror duel against Warcraft's own attack-move (blue) in 54 seconds,
keeping 79% of its army: the env's own drawing of the game, from its replay. Every run here plays like this in the
[Space](https://huggingface.co/spaces/earakely-scale/wc3env-AgentEnv), with the event feed and the agent's plans
beside the map.*

LLM agents play the real Warcraft III: The Frozen Throne (1.29) through
[wc3env](https://github.com/pwang724/wc3env), as an [AgentEnv](https://github.com/scaleapi/agentenv-framework)
environment: the [agentenv-wc3](https://github.com/earakely-scale/agentenv-wc3-plugin) plugin. The agent reads the
game through MCP tools (`get_state`, `list_units`, `lookup`), gives orders with `act` and moves game time on with
`advance`. Each game is graded per player.

This dataset holds three v1 task sets and every run of them from 2026-10-09, played by five models on the real game:

| Bundle | Tasks | What the agent does | Graded on | Runs |
|---|---:|---|---|---:|
| `wc3-v1-drills` | 25 | one skill in a staged scene of 1.5 to 10 game minutes: economy, map control, defence, combat, creeping, hero and items, a full game | the share of its checks met; passed when all are | 75 |
| `wc3-v1-ladder` | 12 | a 30-minute game as Human against the game's easy, normal or insane Orc AI, on Echo Isles or Terenas Stand, seed 1 or 2 | the outcome: a win 1, a draw at the time limit 0.5, a loss 0 | 38 |
| `wc3-v1-duels` | 16 | a mirror duel: two identical late-game armies of one race, against Warcraft's own attack-move, seeds 1 to 4 | the outcome, decided once one army is down to 40% of the other's strength | 26 |

Every run comes with its replay in the browser, its timeline, the game's own `.w3g` replay and, from env v34, the
agent's untrimmed transcript. **[Watch them in the Space](https://huggingface.co/spaces/earakely-scale/wc3env-AgentEnv).** The dataset and the Space make up the
collection [wc3env on AgentEnv](https://huggingface.co/collections/earakely-scale/wc3env-on-agentenv-6ac924a8810f02942599a5a4).

**Bring your own game.** Warcraft III is a trademark of Blizzard Entertainment. This dataset holds no game files,
activation files or game footage, and isn't affiliated with or endorsed by Blizzard. The replays in the browser are
the env's own drawing, and the `.w3g` replays need your own game to watch. To play a task you need your own Warcraft
III: Reforged (it includes the classic game) and an x86-64 Linux host; see [Play a task](#play-a-task).

## Results

**The ladder** (Human against the Orc AI, 30-minute games; DeepSeek, Haiku and GPT-5.4 mini each played all 12
games):

| Model | Won / drawn / lost | Score share | Cost a game |
|---|---|---:|---:|
| DeepSeek V4.1 Flash | 0 / 1 / 11 | 26% | $0.10 |
| Claude Haiku 4.5 | 0 / 0 / 12 | 21% | $0.34 |
| GPT-5.4 mini | 0 / 0 / 12 | 19% | $0.12 |
| Claude Sonnet 5.5 (normal AI, Echo Isles, seed 2) | draw | 29% | $1.23 |
| Claude Opus 5.5 (normal AI, Echo Isles, seed 2) | loss at 27.5 minutes | 31% | $3.14 |

No model beats any level, not even the easy AI. The low-cost models bank their money, rarely cut lumber, and send
armies of four to seven Footmen at the AI's base. Sonnet and Opus spend what they mine and act on the env's
feedback, but fall behind after 12 to 18 minutes.

**The drills:**

| Model | Passed | Checks met | Cost a drill |
|---|---:|---:|---:|
| DeepSeek V4.1 Flash | 11 of 25 | 82% | $0.02 |
| Claude Haiku 4.5 | 3 of 25 | 62% | $0.11 |
| GPT-5.4 mini | 2 of 25 | 49% | $0.03 |

The cheapest model plays the drills best. The checks most models miss are `idle_worker_seconds` (a worker idle for
more than 30 seconds) and `average_unspent_gold`.

**The duels** (26 of the 48 planned: seeds 1 and 2 and part of 3, before the run's budget ran out):

| Player 0 | Duels | Points | Won / drawn / lost | Cost a duel |
|---|---:|---:|---|---:|
| DeepSeek V4.1 Flash | 9 | 0.33 | 3 / 0 / 6 | $0.07 |
| Claude Haiku 4.5 | 9 | 0.28 | 2 / 1 / 6 | $0.31 |
| GPT-5.4 mini | 8 | 0.00 | 0 / 0 / 8 | $0.14 |
| Warcraft's own attack-move (`duels_references`) | 24 | 0.63 | 14 / 2 / 8 | $0 |

Each model fights worse than Warcraft's attack-move from the same player slot.

The plugin's [docs/evals](https://github.com/earakely-scale/agentenv-wc3-plugin/tree/v0.3.0/docs/evals) read these
runs game by game: [ladder](https://github.com/earakely-scale/agentenv-wc3-plugin/blob/v0.3.0/docs/evals/ladder.md),
[drills](https://github.com/earakely-scale/agentenv-wc3-plugin/blob/v0.3.0/docs/evals/drills.md),
[duels](https://github.com/earakely-scale/agentenv-wc3-plugin/blob/v0.3.0/docs/evals/duels.md) and
[frontier](https://github.com/earakely-scale/agentenv-wc3-plugin/blob/v0.3.0/docs/evals/frontier.md).

## Tables

Every config has one split, `eval`. Two kinds of tables share the repo:

- **WC3's own:** what each task stages and checks, and each run's outcome, score and cost.
- **agentenv-hf's,** one pair per bundle, as `agent-env hf publish` writes them: the tasks' steps and prompts, and each
  run's record and chat transcript (`messages`).

A run has the same `episode_id` in both, so you can join them. Each of WC3's episode rows, and each reference duel,
also names its files: `replay`, `timeline`, `w3g` and `transcript` (empty when the run has none).

| Config | Rows | One row is |
|---|---:|---|
| `ladder_tasks` | 12 | a ladder game: map, seed, the AI's level and race, time limit, cost cap |
| `ladder_episodes` | 38 | a run: model, outcome, reward, score against the AI's, units killed, orders and orders refused, turns, game seconds, cost |
| `drills_tasks` | 25 | a drill: its skill, opponent, time limit and checks (`metric`, `op`, `value`) |
| `drills_episodes` | 75 | a run: each check met or not with what was measured, the share met, passed, turns, cost |
| `duels_tasks` | 16 | a duel: race, seed, time limit, the strength ratio that decides it |
| `duels_episodes` | 26 | a run: outcome, score share, the share of its army it kept and of the enemy's it destroyed, cost |
| `duels_references` | 24 | a Warcraft-against-Warcraft duel, from player 0's side: outcome, score share, its files (seeds 1 to 6; `task` names the v1 duel it mirrors) |
| `wc3-v1-*_tasks` | 25, 12, 16 | a bundle task: its steps and prompt (agentenv-hf) |
| `wc3-v1-*_episodes` | 75, 38, 26 | a run: reward, scores, verifications, `messages` (the chat transcript), tool calls (agentenv-hf) |

Beside the tables, each run's files are under its `episode_id` (`<bundle>/<run>`, or `duels-references/<run>`):

| Folder | Runs | What a file is |
|---|---:|---|
| `replays/` | 163 | the replay in a browser: one HTML file, the spectator page with the whole game in it. The map, every unit any player sees, the event feed, the agent's plans and both sides' momentum, scrubbable. About 0.5 MB. |
| `timelines/` | 163 | the same game as JSON: every frame's units, events and notes, to analyse or redraw a game without playing it |
| `w3g/` | 163 | the game's own replay (`.w3g`), which plays in Warcraft III 1.29, with its startup options (`.w3g.json`) |
| `transcripts/` | 103 | `wc3-llm`'s untrimmed transcript: every message and tool call, where `messages` holds what the model was sent (from env v34) |

And:
- `bundles/<bundle>/`: the bundles that `agent-env hf run` plays.
- `raw/<bundle>.jsonl`: each run's record and trajectory, as agentenv-hf writes them.
- `runs/<sweep>/`: each sweep's spec (`sweep.json`), its results (`results.jsonl`) and its report.
- `assets/`: the clip at the top of this card, and the map video it is cut from.

```python
from datasets import load_dataset

repo, rev = "earakely-scale/wc3env-AgentEnv", "v0.3.0"
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
pip install "agentenv-wc3 @ git+https://github.com/earakely-scale/agentenv-wc3-plugin@v0.3.0"
agent-env wc3 build-worker "<game folder>"     # the worker image from your copy, about five minutes
agent-env wc3 license import "<game folder>"   # your activation files, into agent-env's secret store
agent-env wc3 setup --agent                    # the env as "wc3", and the agents
agent-env hf run earakely-scale/wc3env-AgentEnv@v0.3.0 --task drill-opening --model anthropic/claude-haiku-4-5
```

`agent-env hf run` downloads the bundle at that revision and checks that the plugin is installed and that the env and
agents are set up, before it plays. It plays `wc3-v1-drills` unless `--bundle` names another:

```bash
agent-env hf run earakely-scale/wc3env-AgentEnv@v0.3.0 --bundle wc3-v1-ladder \
    --task ladder-echoisles-vs-easy-orc-s1 --model <your model>
agent-env hf run earakely-scale/wc3env-AgentEnv@v0.3.0 --bundle wc3-v1-duels --eval duels --model <your model>
```

Without `--model`, a task plays Claude Haiku 4.5. `--dry-run` shows what would run, and `--yes` skips the question.
The plugin's [README](https://github.com/earakely-scale/agentenv-wc3-plugin/tree/v0.3.0#play-the-real-game-x86-64-linux)
has the setup in full. It also covers watching a game live and getting its video and replay, and `agent-env wc3
sweep` for running a task set across models.

## How the runs were made

Each set was played as a sweep by `wc3-llm`, the plugin's agent: any chat model, with the env's tools. The sweeps ran
two or three games at a time on one Linux host, under a spend budget. `runs/` has each sweep's spec and results.

| Sweep | Bundle | Models | Per-game cap |
|---|---|---|---:|
| `drills-final` | `wc3-v1-drills` | DeepSeek V4.1 Flash, Haiku 4.5, GPT-5.4 mini | $0.50 |
| `ladder-final` | `wc3-v1-ladder` | the same three | $2 |
| `frontier-final` | `wc3-v1-ladder` (one task) | Claude Sonnet 5.5, Claude Opus 5.5 | $5 |
| `duels-final` | `wc3-v1-duels` | DeepSeek V4.1 Flash, Haiku 4.5, GPT-5.4 mini | $0.50 |
| `duel-baselines-final` | `duels_references` | `wc3-scripted` in both player slots | $0 |

The env changed during the day (plugin env versions v32 to v36). The fixes report why the game dropped an order, keep
a dead hero in view, and check each order on its own; each fix reached the games started after it. A sweep's task
names carry the model (`ladder-gpt-5.4-mini-echoisles-vs-easy-orc-s1`); here each run is filed under its v1 task.

## Limitations

- **Few runs.** One game per model and task, so a model's ladder record is 12 games, and the frontier models played
  one ladder task each. The duels stopped at 26 of 48.
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
  skill builds.

This dataset and its Space are built by
[`scripts/hub_dataset.py`](https://github.com/earakely-scale/agentenv-wc3-plugin/blob/v0.3.0/scripts/hub_dataset.py)
in the plugin's repository, from the sweeps' folders and the agent-env store they wrote to.
