"""What an agent playing through the wc3 tools is told about them, shared by the sweep's full-game prompt and the
drills' prompts; braces are doubled for str.format."""

RACES = {"human": "Human", "orc": "Orc", "undead": "Undead", "night_elf": "Night Elf"}
WORKER = {"human": "Peasant", "orc": "Peon", "undead": "Acolyte", "night_elf": "Wisp"}
SUPPLY = {"human": "Farm", "orc": "Orc Burrow", "undead": "Ziggurat", "night_elf": "Moon Well"}
HOW_TO_PLAY = (
    "Game time is frozen until you call advance, so think as long as you like. The loop: get_state shows your "
    "resources, units, structures, idle workers and what happened; act queues orders for your units by id; advance "
    "sends them and lets 1-60 game seconds pass (a few seconds in fights, longer while the economy runs). list_units "
    "shows ids, positions and orders; list_units with details=true shows what a unit can train, build, research and "
    "cast; resources lists gold mines and trees; lookup gives any type's cost, requirements and stats. Coordinates are "
    "the game's world units: copy them from the tools' output. Types can be given by name: train "
    "{{\"type_id\": \"{worker}\"}}, build {{\"type_id\": \"{supply}\", \"x\": ..., \"y\": ..., \"auto_place\": true}}. "
    "When the game refuses an order, advance says why; don't repeat it unchanged.")
PLAY_TO_THE_END = "Keep playing until advance reports GAME OVER, then reply with the result."


def drill_prompt(goal: str, race: str) -> str:
    """A drill's goal, then how to play as `race` (a faction, such as night_elf)."""
    return (f"{goal}\n\n{HOW_TO_PLAY.format(worker=WORKER[race], supply=SUPPLY[race])}\n\nYou act only through the wc3 "
            f"tools; nobody will answer questions, so never ask or wait for confirmation. {PLAY_TO_THE_END}")
