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

**One run is a model flake.** DeepSeek's `drill-hero-in-danger` (0 against 1 raw) wrote its first tool call as text
(`<｜DSML｜ calls>`) instead of a call, so the agent stopped before any order. It is the only commander drill with no
orders.
