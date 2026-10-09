# ladder-frontier: Sonnet 5.5 and Opus 5.5 against the game's normal and insane AI

[sweeps/ladder-frontier.toml](../../sweeps/ladder-frontier.toml), played on the real game on 2026-10-09: `vs-ai`
for two frontier models through `wc3-llm`, as Human against the Orc AI on Echo Isles, in 30-minute games, graded on
the outcome (a win 1, a draw at the time limit 0.5, a loss 0).

| Game | Env | Result | Game time | Cost | Turns | Score vs the AI |
|---|---|---|---|---|---|---|
| Sonnet 5.5 vs normal, seed 1 | v28–v29 | loss | 22.8 min | $1.22 | 149 | 37.9k vs 95.3k |
| Opus 5.5 vs normal, seed 1 | v28–v29 | draw | 30 min | $2.36 | 151 | 59.6k vs 101.6k |
| Sonnet 5.5 vs insane, seed 1 | v28–v29 | loss | 20.8 min | $1.26 | 159 | 31.5k vs 104.9k |
| Opus 5.5 vs insane, seed 1 | v28–v29 | loss | 25.9 min | $2.82 | 198 | 37.8k vs 116.1k |
| Sonnet 5.5 vs normal, seed 2 | v36 | draw | 30 min | $1.23 | 141 | 40.5k vs 101.4k |
| Opus 5.5 vs normal, seed 2 | v36 | loss | 27.5 min | $3.14 | 197 | 42.7k vs 95.3k |

Six games, $12.02: four overnight, before the env's fixes, and two on the final env. **Neither model beats the
normal AI**: each drew one game of two against it and lost the other, and both lost to insane. They are a league above the low-cost models of the [ladder](ladder.md), though:
- **They spend.** Sonnet held 45 to 70 food of 72 to 84 through the middle of its seed-2 game, with 200 to 400 gold
  in the bank where Haiku banked 7,000.
- **They use the env's feedback.** On env v36 Sonnet was told 9 times why an order never started (mostly gold, once
  that it lacked a Lumber Mill and a Castle) and sent 6 revive orders for the heroes the env listed dead; Opus was
  told 17 times, 15 of them for gold. 3% of their orders were refused.
- **They lose the late game.** Opus's army matched the AI's in value until 18 minutes, Sonnet's trailed from 12;
  then the AI pulled away. Opus was wiped out at 27.5 minutes, and Sonnet was down to 7 units at the limit.

Opus played most of its game through `advance` alone, sending its orders with it (126 `advance` calls, 2 `act`).

## Peter's writeup setup, as near as runs here

`macro-micro-realtime` with wc3agent: Opus 5.5 as macro (reasoning medium), Haiku 4.5 as micro instead of Jev (no
TypeSafe key), Orc against the insane Orc AI, in realtime with the game's own picture, 20 minutes, capped at $5.

It reached the cap at 10:54 of game time, after 917 decisions ($5.03), and the env played the rest of the game out
with no orders; the AI won at 18:38. So it says nothing about the setup's strength, only its cost: about $0.45 a
game minute, $9 or more for a 20-minute game. The client video, the highlights and wc3agent's report and session
were kept with the match.
