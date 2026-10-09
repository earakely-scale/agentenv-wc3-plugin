# first-eval: four low-cost models against the easy AI

> **Graded before outcome grading.** The grades below are from the first full-game rubric, which blended the outcome
> with the game's score, so a game nobody won scored 0.33 to 0.50. On outcomes (a win 1, a draw 0.5, a loss 0), all 48
> games of both runs were draws at the 5-minute limit: every model 0 / 6 / 0, 0.50 points. The ranking below is then
> the score share's, the tiebreak.

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

## What held the models back

From the 24 stored conversations, summaries and timelines (no model calls):

| Model | Orders a game | Refused by the game | Refused Barracks a game | Advances a game (median s) | Workers at 2 / 5 min | First army unit | Gold left unspent (mean) |
|---|---|---|---|---|---|---|---|
| Gemini 3.8 Flash | 60 | 1% | 0.7 | 24 (7 s) | 8 / 8 | 90–126 s | 440 |
| DeepSeek V4.1 Flash | 47 | 10% | 3.8 | 16 (20 s) | 8 / 14 | 189–293 s | 740 |
| Haiku 4.5 | 36 | 28% | 7.3 | 24 (10 s) | 8 / 9 | 184–300 s, none in 1 game | 950 |
| GPT-5.4 mini | 42 | 20% | 7.5 | 10 (25 s) | 8 / 13 | none in any game | 880 |

- **A harness bug held back the army.** Of the refused orders, 108 were "Peasant build Barracks" refused as
  `bad_arguments`. The name "Barracks" is both the Human and the Orc building, and the env passed a name that
  matched more than one type to the game unresolved. Seventy-nine unit names are shared that way, among them every
  melee hero and its campaign versions, so "train Archmage" failed too. Gemini mostly avoided it, and it was the one
  model with an army before two minutes. Fixed in 887d7f8: a name resolves to the type the ordering unit makes, and
  an order it can't give is refused at `act`, naming the units that can.
- **Wrong builder:** "Town Hall build Farm", refused as `no_build_site`, came 16 times. It is now refused at `act`
  with "your Peasant can".
- **Smaller tool errors:**
  - `queued` put beside `arguments` instead of inside it (5);
  - `queued` on an order that doesn't take it (8);
  - orders to a unit that isn't the model's (10).

  Each comes back with a clear error the model can read.
- **Play:**
  - Haiku and Gemini stopped at 8–9 workers.
  - GPT-5.4 mini advanced 25–60 s at a time: 10 decisions in a 5-minute game.
  - Gold piled up unspent wherever the Barracks failed.

## Rerun with the fix

The same 24 games on env v18 (887d7f8), 2026-10-06: $4.12. The budget guard held this time. Once a second game at
once could have taken the spend past $5, it played the last five games one at a time.

| Model | Grade (mean ± sd) | Score vs AI | Cost a game | Refused orders a game | First army unit | Attack orders a game |
|---|---|---|---|---|---|---|
| Gemini 3.8 Flash | 0.49 ± 0.02 (was 0.48) | 9.6k vs 9.5k | $0.50, at the cap in all 6 | 0 (was 0.7) | 90–128 s | 24 |
| DeepSeek V4.1 Flash | 0.45 ± 0.02 (was 0.45) | 6.5k vs 9.6k | $0.01 | 0.5 (was 4.5) | 131–206 s (was 189–293) | 1.7 |
| Haiku 4.5 | 0.44 ± 0.01 (was 0.44) | 5.5k vs 8.7k | $0.11 | 0.2 (was 9.8) | 131–196 s (was 184–300, none in 1) | 0.8 |
| GPT-5.4 mini | 0.43 ± 0.01 (was 0.43) | 4.9k vs 8.3k | $0.06 | 0.2 (was 8.2) | 106–240 s in 3 games (was none) | 0.2 |

- **The fix worked.** Refusals fell to under one a game for every model, and armies came 50 to 100 seconds sooner.
  GPT-5.4 mini fielded one in half its games.
- **The grades did not move.** Only Gemini attacks: 24 attack orders a game, where the others give under two. An
  army that stands at home adds little to the score in 5 minutes against the easy AI. So this task now measures
  whether a model fights, more than how well it builds.
- **The ranking repeats.** Both runs put the models in the same order, with each mean within 0.01 of the first run.
  The first run's grades were not distorted by the bug as much as its refusals suggested, because the army was never
  the deciding factor.

## Next

- A longer game, or the `normal` AI, so that games end in wins and losses rather than in score ratios, and an army
  has time to matter.
- Gemini with a higher cap, or with prompt caching, to see its uncapped result.
- More seeds for the top two, where the gap (0.48 against 0.45) is about one sd of Gemini's grades.
