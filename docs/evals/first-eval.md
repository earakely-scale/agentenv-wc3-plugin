# first-eval: four low-cost models against the easy AI

[sweeps/first-eval.toml](../../sweeps/first-eval.toml), played on the real game on 2026-10-06:
- `vs-ai-quick` crossed over four models, two 1v1 maps (Echo Isles, Terenas Stand) and seeds 1 to 3: 24 games;
- each game is `wc3-llm` as Human against the game's easy Orc AI, for 5 minutes of game time;
- a per-game cap of $0.50 (`WC3_MAX_COST_USD`);
- about 4 minutes of wall time per game, one at a time.

It was started as a one-game check (`--budget 0.5`). The runner read $0 for every game, so the budget never
closed and all 24 games played. The cause was a bug, since fixed: it read the wrong seat of a match without
`seats`. The spend, read from the runs afterwards, was $4.24.

Sweep `first-eval`: 24 games, $4.24 of model spend.

| Model | Games | Grade (mean ± sd) | Won / limit / lost | Score vs opponent | Score share | Cost a game | At the $0.50 cap | Turns a game |
|---|---|---|---|---|---|---|---|---|
| gemini/gemini-3.8-flash | 6 | 0.48 ± 0.03 | 0 / 6 / 0 | 8.9k vs 9.8k | 47% | $0.504 | 6 | 75 |
| fireworks_ai/deepseek-v4p1-flash | 6 | 0.45 ± 0.01 | 0 / 6 / 0 | 6.5k vs 9.1k | 42% | $0.019 | 0 | 38 |
| anthropic/claude-haiku-4-5 | 6 | 0.44 ± 0.01 | 0 / 6 / 0 | 5.3k vs 8.6k | 38% | $0.139 | 0 | 72 |
| openai/gpt-5.4-mini | 6 | 0.43 ± 0.01 | 0 / 6 / 0 | 4.6k vs 8.2k | 36% | $0.045 | 0 | 31 |

Grade by map, one per seed:

| Model | echoisles | terenasstand | Spread across seeds (mean sd) |
|---|---|---|---|
| gemini/gemini-3.8-flash | 0.50 0.45 0.50 | 0.50 0.47 0.45 | 0.03 |
| fireworks_ai/deepseek-v4p1-flash | 0.45 0.44 0.45 | 0.46 0.45 0.47 | 0.01 |
| anthropic/claude-haiku-4-5 | 0.43 0.44 0.44 | 0.44 0.43 0.43 | 0.01 |
| openai/gpt-5.4-mini | 0.42 0.43 0.43 | 0.41 0.44 0.43 | 0.01 |

## Reading it

- **Every game reached the time limit.** No model destroyed the AI's base in 5 minutes, and none was destroyed.
  Under `melee`, such a game's grade is (2 + min(1, score / the AI's score)) / 6. The grade is the score ratio,
  squeezed into 0.33 to 0.50, and 0.50 means the model outscored the AI. Gemini did so in 3 of its 6 games; the
  other models never did.
- **The ranking is clear even though the grades are close.** Gemini 3.8 Flash comes first, then DeepSeek V4.1
  Flash, Haiku 4.5 and GPT-5.4 mini. Every Gemini game outscored every Haiku and GPT-5.4 mini game. Gemini was also
  the only model that killed anything: 1 to 7 units a game.
- **Gemini's games were cut short by the cap.** It reached $0.50 in all six games, and stopped giving orders between
  170 and 213 seconds of the 300. `rts_finish` then played the game out with its units idle. It sent 1.0 to 1.8 M
  tokens a game, about what Haiku sent for $0.14 with its prompt cached; whether Gemini's cache took effect was not
  checked. With a higher cap it would cost more and probably score higher.
- **Cost:**
  - DeepSeek V4.1 Flash came second for $0.02 a game, 7× cheaper than Haiku and 26× cheaper than Gemini.
  - GPT-5.4 mini used the fewest turns (31): it advanced in long steps and had no army at the end of any game.
- **Seed variance is small.** On grade, the sd across the three seeds of one map is 0.01 for three models and 0.03
  for Gemini. On score it is wider: Gemini's games ran from 7.0k to 11.2k. The AI's own score moves with how much it
  fought: 8.1k to 8.3k against a model that left it alone, up to 11.9k.

## Next

- A longer game, or the `normal` AI, so that games end in wins and losses rather than in score ratios.
- Gemini with a higher cap, or with prompt caching, to see its uncapped result.
- More seeds for the top two, where the gap (0.48 against 0.45) is about one sd of Gemini's grades.
