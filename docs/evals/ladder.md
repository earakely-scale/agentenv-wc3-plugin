# ladder: three low-cost models against the game's easy, normal and insane AI

[sweeps/ladder.toml](../../sweeps/ladder.toml), played on the real game on 2026-10-09 (env v32 to v34, which play
these games alike; plugin main `dd1be7a` to `5063ee4`):
- `vs-ai` crossed over three models, the easy, normal and insane Orc AI, two 1v1 maps (Echo Isles, Terenas Stand)
  and seeds 1 and 2: 36 games, 12 a model;
- each game is `wc3-llm` as Human, for up to 30 minutes of game time; a game won or lost ends early;
- graded on the outcome: a win 1, a draw (the time limit) 0.5, a loss 0. A level is beaten when at least three of
  its four games are won;
- a per-game cap of $2 (no game came near it), three games at a time, 3.5 hours of game wall time in all.

Sweep `ladder`: 36 games, $6.70 of model spend.

| Model | Games | Points (mean ± sd) | Won / drawn / lost | Score share | Score vs opponent | Cost a game | Turns a game |
|---|---|---|---|---|---|---|---|
| fireworks_ai/deepseek-v4p1-flash | 12 | 0.04 ± 0.14 | 0 / 1 / 11 | 26% | 33.2k vs 93.7k | $0.100 | 130 |
| anthropic/claude-haiku-4-5 | 12 | 0.00 ± 0.00 | 0 / 0 / 12 | 21% | 16.0k vs 58.9k | $0.341 | 122 |
| openai/gpt-5.4-mini | 12 | 0.00 ± 0.00 | 0 / 0 / 12 | 19% | 9.8k vs 40.8k | $0.117 | 55 |

| Model | easy | normal | insane | Highest level beaten |
|---|---|---|---|---|
| fireworks_ai/deepseek-v4p1-flash | 0-1-3 | 0-0-4 | 0-0-4 | none |
| anthropic/claude-haiku-4-5 | 0-0-4 | 0-0-4 | 0-0-4 | none |
| openai/gpt-5.4-mini | 0-0-4 | 0-0-4 | 0-0-4 | none |

How long each model lasted, and what it killed (means over its four games at a level):

| Model | easy | normal | insane |
|---|---|---|---|
| DeepSeek V4.1 Flash | 24.8 min, 19 kills | 24.4 min, 10 kills | 18.8 min, 8 kills |
| Haiku 4.5 | 22.4 min, 2 kills | 14.6 min, 1 kill | 13.1 min, 0 kills |
| GPT-5.4 mini | 22.8 min, 0 kills | 10.8 min, 0 kills | 9.2 min, 0 kills |

**No model beats any level.** The easy AI beat DeepSeek in three of four games (the fourth a draw at the limit) and
the other two models in every game. The overnight ladder on the same spec (env v28 to v32, before the fixes below
reached every game) came out the same: 0 wins in 36, DeepSeek's one draw.

**What the games show** (the timelines, and from env v34 the models' own transcripts):
- **Money unspent.** Haiku ended games with 7,000 to 8,000 gold and 2,500 lumber in the bank at a 30-food cap;
  GPT-5.4 mini with 10,000 to 12,000 gold and no lumber at all, its workers all on gold.
- **No lumber.** In one game GPT-5.4 mini sent 92 harvest orders to the gold mine and 5 anywhere else, so its Farms
  and Barracks after the first few were refused for lumber.
- **Few, small armies.** Armies of 4 to 7 Footmen walked into the AI's base and died; GPT-5.4 mini killed one unit
  in 12 games.

**Env bugs these games found, fixed on 2026-10-09** (each in [FD-3806](https://linear.app/scale-epd/issue/FD-3806)):
- The game drops an order it can't pay for without a word; `advance` now names it and what it lacked. In one of GPT-5.4
  mini's transcripts it is told "not enough lumber" four times while its workers stay on gold.
- A dead hero dropped out of the agent's view. Haiku spent 15 minutes ordering Archmages and Paladins the game
  dropped, because a dead hero is revived, not retrained; `get_state` now lists it with its revive order.
- One order for a worker inside the gold mine sank its whole `act` call: 10 of GPT-5.4 mini's 11 tool errors in a
  game. From env v35 each order is checked on its own, and an order for a unit inside waits for it.

Next: the ladder's top rungs for frontier models ([ladder-frontier](frontier.md)).
