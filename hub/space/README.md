---
title: Warcraft III on AgentEnv
emoji: ⚔️
colorFrom: yellow
colorTo: gray
sdk: static
pinned: true
license: apache-2.0
short_description: Every Warcraft III run in wc3env-AgentEnv, replayed
thumbnail: https://huggingface.co/spaces/earakely-scale/wc3env-AgentEnv/resolve/main/thumbnail.jpg
datasets:
- earakely-scale/wc3env-AgentEnv
tags:
- agentenv
- rl-environment
- warcraft-iii
- real-time-strategy
- replay
---

# Warcraft III on AgentEnv

LLM agents play the real Warcraft III: The Frozen Throne through [wc3env](https://github.com/pwang724/wc3env), on
[AgentEnv](https://github.com/scaleapi/agentenv-framework) with the
[agentenv-wc3](https://github.com/earakely-scale/agentenv-wc3-plugin) plugin. This Space replays every run of the
dataset [earakely-scale/wc3env-AgentEnv](https://huggingface.co/datasets/earakely-scale/wc3env-AgentEnv) in the
browser:
- **The ladder:** 30-minute games against the game's easy, normal and insane AI.
- **The drills:** 25 staged scenes, one skill each.
- **The mirror duels:** a model against Warcraft's own attack-move.
- **The Warcraft-against-Warcraft duels** they read against.

A replay plays the game's own picture, rendered from the run's .w3g replay:
- the camera follows the agent's fights and key moments;
- the plans the agent wrote show in the game as it wrote them;
- beside it are the env's map, the event feed and both sides' momentum.

**How it runs:** a static page.
- `runs.json` lists each run with its outcome, score and cost.
- A replay and its video are loaded from the dataset at the revision `runs.json` pins.
- The plugin's `scripts/hub_dataset.py space` writes both from the dataset folder.

Part of the collection [wc3env on AgentEnv](https://huggingface.co/collections/earakely-scale/wc3env-on-agentenv-6ac924a8810f02942599a5a4).

Warcraft III is a trademark of Blizzard Entertainment, and the videos show its picture. This Space holds no game
files and isn't affiliated with or endorsed by Blizzard.
