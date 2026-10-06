# vs-normal: three low-cost models against the normal AI

[sweeps/vs-normal.toml](../../sweeps/vs-normal.toml), played on the real game on 2026-10-06 (env v18, main b4e602a):
- `vs-ai-quick` crossed over three models, two 1v1 maps (Echo Isles, Terenas Stand) and seeds 1 and 2: 12 games;
- each game is `wc3-llm` as Human against the game's normal Orc AI, for 12 minutes of game time;
- a per-game cap of $1.00 (no game reached it) and a budget of $5;
- 3.5 to 7 minutes of wall time a game, two at a time.

Gemini 3.8 Flash, first in [first-eval](first-eval.md), is left out. It spent $0.50 in 3 minutes of a 5-minute game,
so 12 minutes would have cost several times the others.

Sweep `vs-normal`: 12 games, $1.45 of model spend.

| Model | Games | Grade (mean ± sd) | Won / limit / lost | Score vs opponent | Score share | Cost a game | At the $1.00 cap | Turns a game |
|---|---|---|---|---|---|---|---|---|
| fireworks_ai/deepseek-v4p1-flash | 4 | 0.41 ± 0.01 | 0 / 4 / 0 | 17.2k vs 39.9k | 30% | $0.045 | 0 | 81 |
| anthropic/claude-haiku-4-5 | 4 | 0.35 ± 0.09 | 0 / 3 / 1 | 11.9k vs 35.6k | 25% | $0.236 | 0 | 98 |
| openai/gpt-5.4-mini | 4 | 0.30 ± 0.10 | 0 / 2 / 2 | 9.2k vs 30.6k | 23% | $0.082 | 0 | 44 |

Grade by map, one per seed:

| Model | echoisles | terenasstand | Spread across seeds (mean sd) |
|---|---|---|---|
| fireworks_ai/deepseek-v4p1-flash | 0.41 0.40 | 0.41 0.39 | 0.01 |
| anthropic/claude-haiku-4-5 | 0.22 0.39 | 0.38 0.40 | 0.07 |
| openai/gpt-5.4-mini | 0.21 0.21 | 0.40 0.38 | 0.01 |

## Reading it

- **Games are decided now.**
  - Three of the twelve ended in a defeat, between 9 and 11 minutes of game time: Haiku once, GPT-5.4 mini twice.
    A defeat costs a model the `survive` criterion, so the grades spread from 0.21 to 0.41, where the 5-minute
    games spread from 0.42 to 0.50.
  - No model won. The normal AI outscored every model two to four times over.
- **DeepSeek V4.1 Flash is first, and it fights.**
  - It survived all four games and gave 140 orders a game, 35 of them attacks; Haiku gave 5 attacks and GPT-5.4 mini 8.
  - It ended each game with the most food in use: 20 to 43, workers included, against at most 12 for Haiku.
  - It is also the cheapest, at under $0.05 a game.
- **Haiku 4.5 and GPT-5.4 mini leave their gold unspent.**
  - They held 1,700 to 1,800 gold on average through the game, twice DeepSeek's.
  - GPT-5.4 mini also played in long jumps: a median advance of 60 seconds, the most allowed, and 14 advances in 12
    minutes. Some of its tool errors were advances of more than 60.
- **Echo Isles is the harder map.** All three defeats came there; on Terenas Stand every model reached the limit.
- **Seed spread is small except where a game turned.** Haiku's two Echo Isles seeds were a defeat and a survival
  (0.22 and 0.39). A 12-minute game against the normal AI is close enough to the edge that one seed can decide it.

## Next

- More seeds on Echo Isles, where the games turn, to rank Haiku against GPT-5.4 mini with confidence.
- A longer game, or the easy AI for 20 minutes, to see whether any model can win rather than only survive.
- Gemini 3.8 Flash with a cap that lets it finish (about $2 a game at its 5-minute rate), as the one model that
  attacked early.
