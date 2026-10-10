# styles: the same tasks through three interfaces

Played on the real game on 2026-10-10, against the raw runs of 2026-10-09 ([drills](drills.md), [duels](duels.md),
[ladder](ladder.md)). The three styles share the game, its stepping clock, staging, grading and recording; only the
player's interface differs (see the README's [Game styles](../../README.md#game-styles-how-much-the-env-does-for-the-player)):
- **raw** (env `wc3`): one model orders units by id through general tools (`get_state`, `act`, `advance`).
- **commander** (env `wc3-commander`): one model through wc3agent's interface, from wc3agent's own code: named units,
  a turn page with what it can and can't do yet, wc3agent's order language, code reflexes, and wc3agent's unit menus
  through `fight` and `choose`. There is no micro model: a group fights by Warcraft's attack-move unless the model
  takes the fight over.
- **wc3agent** (agent `wc3-macro-micro` on env `wc3`): wc3env's own agent as Peter Wang built it, a macro model and a
  micro model (Claude Haiku 4.5 here; wc3agent's own micro, Jev, needs a TypeSafe key).

Sweeps: [drills-commander](../../sweeps/drills-commander.toml), [duels-commander](../../sweeps/duels-commander.toml),
[ladder-commander](../../sweeps/ladder-commander.toml) and [duels-macro-micro](../../sweeps/duels-macro-micro.toml),
each with the raw sweep's models, caps and seeds, except where said.

In short: the commander style lifts the weaker models in the drills (Haiku passes 9 of 25 against 3, GPT-5.4 mini 7
against 2) and keeps every model in a ladder game longer, but it doesn't fight better. In the duels GPT-5.4 mini goes
from 0 of 8 to 5 of 8 by attack-moving as Warcraft does. wc3agent, the only style with a model for the fights, wins 4
of its 6 duels at about 20 times the cost of a commander duel.

## Drills

Sweep `drills-commander`: the 25 drills for the same three models, 75 drills, $5.14 of model spend (raw: $4.08).

| Model | Passed, commander | Passed, raw | Checks met, commander | Checks met, raw | Cost a drill, commander | Cost a drill, raw | Turns, commander | Turns, raw |
|---|---|---|---|---|---|---|---|---|
| DeepSeek V4.1 Flash | 11 of 25 | 11 of 25 | 79% | 81% | $0.027 | $0.023 | 22 | 35 |
| Claude Haiku 4.5 | 9 of 25 | 3 of 25 | 70% | 59% | $0.125 | $0.107 | 31 | 49 |
| GPT-5.4 mini | 7 of 25 | 2 of 25 | 64% | 48% | $0.054 | $0.033 | 21 | 21 |

**The commander style lifts the two weaker models a lot and leaves the strongest where it was.** Haiku passes three
times as many drills and GPT-5.4 mini three and a half times as many; DeepSeek, the best raw player, passes the
same 11. A turn does more: Haiku and DeepSeek take a third fewer turns a drill.

By skill, the mean reward over the three models:

| Skill | Drills | Raw | Commander |
|---|---:|---:|---:|
| economy | 8 | 0.61 | 0.73 |
| defence | 6 | 0.70 | 0.83 |
| creeping | 2 | 0.75 | 0.89 |
| map control | 2 | 0.75 | 0.83 |
| hero and items | 3 | 0.74 | 0.78 |
| full game | 1 | 0.44 | 0.44 |
| combat | 3 | 0.44 | 0.23 |

**What helps is the economy and the base.** The page lists what can be built now and what is still short ("NOT
YET: 70 more gold"), names every unit, and the order language builds, trains and gathers in one line each. GPT-5.4
mini's `drill-build-towers` goes from 0 to 1 and `drill-loot` from 0 to 1; Haiku's `drill-build-towers` from 0.2 to 1.

**What hurts is fighting.** The commander has no micro model, and the models seldom take a fight over: `fight` was
called 73 times by Haiku, 28 by DeepSeek and 13 by GPT-5.4 mini over all their drills, and in 4 of the 9 combat
drills at all. Their groups fight by attack-move, as wc3agent's groups would without its micro model, and the
combat drills fall from 0.44 to 0.23. The hero drills show the other side of a macro page: models open with build
orders. GPT-5.4 mini's `drill-hero-revive` built a Farm, a Barracks and an Altar before moving its Archmage, which
died in the first seven seconds; its revive orders then came too early (Warcraft refuses a revive for about 20
seconds after the death) and at an Altar still finishing. On the raw tools it walked the hero home and kept it.

**One run ended on the agent's token limit.** DeepSeek's `drill-hero-in-danger` (0 against 1 raw) gave no order.
Three of its turns ran out of tokens while it reasoned and came back empty, and a fourth wrote its tool call as text
(`<｜DSML｜ calls>`) instead of a call, so the agent stopped (see [Duels](#duels)). It is the only commander drill
with no orders.

## Duels

Sweep `duels-commander`: the 16 mirror duels for the same three models at the raw sweep's $0.50 cap a duel. Its
budget played 40 of the 48 duels, $8.70 of model spend; the raw sweep's played 26, and the table pairs those 26.

| Model | Won, commander | Won, raw | Cost a duel, commander | Cost a duel, raw | Turns, commander | Turns, raw | Orders, commander | Orders, raw |
|---|---|---|---|---|---|---|---|---|
| GPT-5.4 mini | 5 of 8 | 0 of 8 | $0.11 | $0.14 | 17 | 52 | 54 | 130 |
| DeepSeek V4.1 Flash | 3 of 9 | 3 of 9 | $0.12 | $0.07 | 24 | 61 | 80 | 180 |
| Claude Haiku 4.5 | 1 of 9 | 2 of 9, 1 drawn | $0.46 | $0.31 | 30 | 81 | 97 | 179 |

Over all 40 commander duels GPT-5.4 mini won 6 of 13, DeepSeek 5 of 14 and Haiku 3 of 13.

**GPT-5.4 mini plays the duel as Warcraft does.** In all 13 of its commander duels its first order put the whole
army in one group and attack-moved it at the enemy base; then it mostly advanced, 17 turns a duel. That is the scripted
side's own play, and it won 5 of 8, 4 of them by fighting: close to the 59% of the points that Warcraft's attack-move
scores from the same player slot ([the baseline](duels.md#the-baselines-first-a-fair-duel)). On the raw tools it
ordered units one by one, 130 orders a duel, and lost all 8.

**Two kinds of ending come from elsewhere, and they account for most of DeepSeek's and Haiku's losses:**
- **The base.** On odd seeds each army starts on the far side of the middle, nearer the enemy's base than the enemy
  army is. Three commander wins (DeepSeek's orc seeds 1 and 3, GPT-5.4 mini's orc seed 1) and one raw win (Haiku's
  orc seed 1) came from attack-moving at the opponent's Great Hall, its only building. The armies passed each other,
  the hall fell at about 30 seconds, and Warcraft's melee rule defeated the player with no buildings and removed its
  army; no unit was killed. The duel's goal asks for the enemy army, but the game's result is a win and the grade
  counts it as one. Without these wins GPT-5.4 mini has 4 of 8 against 0, DeepSeek 2 of 9 against 3, and Haiku 1
  of 9 against 1 and a draw.
- **Stopping early.** In 13 commander duels the player stopped more than 15 game seconds before the end, and lost
  all 13 (4 of the 26 raw duels). Haiku reached the $0.50 cap in 8 of its 13 commander duels (1 of 9 raw): every
  commander turn returns the whole page, and its turns cost $0.015 against $0.004 on the raw tools. DeepSeek's turns
  came back empty often enough to end its play in 10 of its 14 commander duels (2 of its 7 raw duels with a
  transcript): the agent stops at a game's fourth reply without a tool call. Its reasoning had run past the agent's
  limit of 4,096 output tokens before it called a tool. Replayed, one such turn ran out at 4,096 tokens of
  reasoning; at 16,384 it reasoned for 14,324 tokens and gave orders. `wc3-llm` now allows 32,768. The runs here
  were played before that fix.

## Ladder

Sweep `ladder-commander`: the 36 ladder games at the raw sweep's $2 cap: three models against Warcraft's easy, normal
and insane Orc AI on Echo Isles and Terenas Stand, seeds 1 and 2. $8.13 of model spend (raw: $6.70).

| Model | Won / drawn / lost, commander | Won / drawn / lost, raw | Lasted, commander | Lasted, raw | Units killed, commander | Units killed, raw | Score vs the AI's, commander | Score vs the AI's, raw | Cost a game, commander | Cost a game, raw |
|---|---|---|---|---|---|---|---|---|---|---|
| DeepSeek V4.1 Flash | 0 / 2 / 10 | 0 / 1 / 11 | 23.1 min | 22.7 min | 28.1 | 12.3 | 44.4k vs 90.9k | 33.2k vs 93.7k | $0.12 | $0.10 |
| Claude Haiku 4.5 | 0 / 0 / 12 | 0 / 0 / 12 | 20.0 min | 16.7 min | 5.8 | 1.1 | 23.8k vs 71.2k | 16.0k vs 58.9k | $0.44 | $0.34 |
| GPT-5.4 mini | 0 / 1 / 11 | 0 / 0 / 12 | 15.7 min | 14.2 min | 2.2 | 0.1 | 12.4k vs 50.8k | 9.8k vs 40.8k | $0.12 | $0.12 |

**No model beats Warcraft's AI in either style, but in the commander style each lasts longer and fights back.**
DeepSeek kills more than twice as many units: 41 a game against the normal AI, where it killed 10 on the raw tools.
It also holds the normal AI to a draw on Terenas Stand, which no raw game did. Haiku kills five times as many units
as on the raw tools, and GPT-5.4 mini's game against the easy AI on Terenas Stand lasts the full 30 minutes. What the
drills showed carries over: the economy and the base keep a player in the game longer, while its fights are still
Warcraft's attack-move. No ladder game reached its cost cap or ended on DeepSeek's empty turns.

## wc3agent

Sweep `duels-macro-micro`: wc3agent, with DeepSeek V4.1 Flash as its macro model and Claude Haiku 4.5 as its micro, in
seed 1 of each mirror duel and seed 2 of the human and night elf mirrors. 6 duels at a $4 cap each, $11.08 of model
spend.

| Duel | wc3agent | Commander (DeepSeek) | Raw (DeepSeek) | wc3agent's cost |
|---|---|---|---|---|
| human, seed 1 | lost | lost | won | $1.77 |
| orc, seed 1 | won | won (the Great Hall) | lost | $2.12 |
| undead, seed 1 | won | lost | won | $2.14 |
| night elf, seed 1 | won | won | lost | $2.04 |
| human, seed 2 | lost | lost | lost | $1.48 |
| night elf, seed 2 | won | lost | won | $1.53 |
| **won** | **4 of 6** | **2 of 6** | **3 of 6** | **$1.85 a duel** |

**wc3agent wins the most duels, at about 20 times the cost.** Its micro model answers a menu for each group every
game second, 300 to 450 model calls a duel; these are the fights the commander style leaves to attack-move. Its own
micro model, Jev, is a trained model that needs a TypeSafe key; Haiku stands in for it here. At $1.50 a duel it
stopped 50 seconds in, before the armies were decided, so the cap is $4. Six duels are too few to rank the three:
one won or lost duel moves a column by 17 points.

The four seed-1 duels were played before a fix to the agent. wc3agent then kept running after the env decided a duel,
until the task's 30-minute timeout, so their reports and transcripts were not kept. The env decides the duel, so
their grades stand. The two seed-2 duels have both.
