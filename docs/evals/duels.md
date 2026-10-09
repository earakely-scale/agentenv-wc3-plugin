# duels: wc3agent's mirror duels, a model against Warcraft's own way of fighting

Played on the real game on 2026-10-09: [sweeps/duel-baselines.toml](../../sweeps/duel-baselines.toml) and
[sweeps/duels.toml](../../sweeps/duels.toml).
- `mirror-<race>`: two identical armies of one race, about 60 food each with every upgrade, heroes at levels 5 and
  3, full mana, staged facing each other across the middle of Echo Isles (wc3agent's duel). The duel is decided once
  one army is down to 40% of the other's strength (wc3agent's), and is a draw after 150 seconds.
- The opponent, `wc3-scripted`, attack-moves its army at the other one every 3 seconds, as wc3agent's duel has
  Warcraft fight. Both player slots cast their spells on their own (`autocast`, wc3env's computer slots).
- `mirror-<race>-baseline` puts the scripted fighter in both player slots: the duel's floor, Warcraft against
  Warcraft. A model's duels read against it, played from the same player slot.

## The baselines first: a fair duel

A duel is only a measure if Warcraft against Warcraft ends about even. The first baselines didn't: player 0 scored
33% over 32 games, whichever ground it stood on. Reversing the staging order flipped it. On 16 Human seeds player 0
scored 3 staged first and 11 staged second, so the side staged second had the edge. The duels now stage both sides
together: the armies are made unit by unit, alternating which of a mirror pair comes first, and both learn their
skills, switch on autocast and fill their mana in the same steps.

| Baselines | Human | Orc | Undead | Night Elf | Player 0 in all |
|---|---|---|---|---|---|
| before (env v29, 4 seeds a race) | 0 of 4 | 1 of 4 | 1.5 of 4 | 2 of 4 | 4.5 of 16, 28% |
| staged together (env v31, 6 seeds a race) | 3.5 of 6 | 2 of 6 | 4 of 6 | 4 of 6 | 13.5 of 24, 56% |
| and wc3agent's hero builds (env v33) | 4 of 6 | 2 of 6 | 4 of 6 | 5 of 6 | 15 of 24, 62% |

Since the fix player 0 has scored 28.5 of 48, 59%: within the noise of even (one standard deviation is 7%). A
baseline duel lasts 80 seconds of game time on average and costs nothing.

## The models

Sweep `duels` (env v33 and v34, which play these duels alike): three models, the four races, seeds 1 to 4; the $5
budget ran out after 26 of the 48 duels (seeds 1 and 2 and part of 3).

| Model | Duels | Points (mean ± sd) | Won / drawn / lost | Score share | Cost a duel | Turns a duel |
|---|---|---|---|---|---|---|
| fireworks_ai/deepseek-v4p1-flash | 9 | 0.33 ± 0.50 | 3 / 0 / 6 | 49% | $0.074 | 61 |
| anthropic/claude-haiku-4-5 | 9 | 0.28 ± 0.44 | 2 / 1 / 6 | 48% | $0.307 | 81 |
| openai/gpt-5.4-mini | 8 | 0.00 ± 0.00 | 0 / 0 / 8 | 45% | $0.141 | 52 |
| Warcraft (the baseline, player 0) | 48 | 0.59 | | | $0 | |

**Each model fights worse than Warcraft's own attack-move** from the same player slot: DeepSeek 33% and Haiku 28%
of the points against the baseline's 59%, GPT-5.4 mini none. A duel takes 50 to 140 seconds of game time; a model
plays it in 15 to 120 turns, a few game seconds each.

Not done: wc3agent ran its duels on a flat arena built from Echo Isles (`tools/prepare/arena.py`), so terrain can't
favour a side. Building it needs StormLib, which wc3env's map tools load from a Windows DLL. Here the creeps in
sight are cleared and odd seeds swap the armies' ground.
