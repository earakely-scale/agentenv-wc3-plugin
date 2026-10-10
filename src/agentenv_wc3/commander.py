"""The commander style (env `wc3-commander`): the game played through wc3agent's macro interface, from wc3agent's own
code (wc3env's agent, MIT), by one model through MCP tools. Where the raw style's tools take each unit's orders by
id, a commander reads wc3agent's turn page (named units, what it can do now and what it can't yet, how its orders
went), writes orders in wc3agent's language (`build peasant1 Farm`, `group army footman1 footman2 attack at X Y:
take camp 3`) and lets code act between its turns: wc3agent's reflexes (creep escape, loot, regrouping stragglers,
idle fighters joining the army, skill points, gold workers), and Warcraft's own attack-move for its groups, unless
it takes a fight over with wc3agent's unit menus (`fight`, `choose`). The game, clock, staging, grading and
recording are the raw style's: only the player's side differs."""

from __future__ import annotations

import threading
from functools import partial
from pathlib import Path
from typing import Annotated

from agentenv_protocol import tool
from pydantic import Field
from wc3agent.agent import Agent
from wc3agent.game.abilities import describe_abilities
from wc3agent.game.catalog import Catalog
from wc3agent.game.featurize import describe, system_prompt
from wc3agent.game.mapinfo import MapInfo
from wc3agent.game.orders import Orders
from wc3agent.macro.memory import MacroMemory
from wc3agent.micro.agent import MicroAgent
from wc3agent.micro.request import Menu, build_request, map_response
from wc3env.data import MAPS, REFERENCE

from . import render
from .bridge import WorkerError
from .server import MAX_ADVANCE_SECONDS, WC3Env, _raise

TICK_MS = 1000   # wc3agent's stepping plays a game second between looks, its reflexes acting on each
RACES = {"human": "human", "orc": "orc", "undead": "undead", "night_elf": "nightelf"}   # ours → wc3agent's
NO_MICRO = """\
IN THIS ENV THERE IS NO MICRO MODEL. Wherever this guide says micro, read: a group's units walk to its `at X Y` (or
attack-move there with `attack at`), Warcraft's own attack-move fights for them, and code acts for them as above
(creep escape below 30% health, loot pickup, stragglers rejoining). To fight a group unit by unit, call fight with
its name: each unit's options right now, numbered; then choose a number for each unit. Your picks hold for a few
seconds, then the group is back on its objective.

THE TOOLS: get_state shows this turn's page; command takes orders, one per line, as above (lines that are not
orders are your plan, and ignored); advance plays the game on, 1 to 60 seconds, and returns the new page; fight and
choose direct a group's fight; lookup gives any type's stats; guide shows this guide again."""
PAGE = {   # wc3agent's page speaks of a micro model; here code and Warcraft fight for the groups
    "GROUPS (micro owns these units until a direct order or reassignment)":
        "GROUPS (on their objectives until a direct order or reassignment; fight takes a group's fight over)",
    "micro replied since last macro turn": "fought by your picks since your last turn",
    "awaiting micro reply": "on attack-move",
}


def page_text(text: str) -> str:
    for theirs, ours in PAGE.items():
        text = text.replace(theirs, ours)
    return text


class Commander:
    """One player slot's wc3agent: its macro memory (which reads every observation, in order), its order parser and
    its micro, which here only runs the code reflexes (no micro model key)."""

    def __init__(self, catalog: Catalog, mapinfo: MapInfo, slot: int, race: str):
        self.slot, self.race = slot, race
        self.macro_memory = MacroMemory(catalog, mapinfo, player=slot)
        self.orders = Orders(self.macro_memory)
        self.micro = MicroAgent(catalog, model="jev", key="", ready=threading.Event(),
                                references=self.macro_memory.references)
        self.pending: list[dict] = []   # the orders `command` parsed, for the next advance
        self.turns: list[int | None] = []
        self.landed = False             # a command since the last advance: skill points and gold workers follow
        self.turn = 0
        self.guided = False
        self.menus: dict[str, tuple[float, list[tuple[int, Menu]]]] = {}   # group → (game time, its menus)

    def see(self, obs: dict) -> None:
        self.macro_memory.update(obs)

    def guide(self, obs: dict) -> str:
        """wc3agent's system prompt (rules, the order language, the race, item and map sheets, guides), then this
        env's difference: no micro model. A staged scene with no hall yet gets its home from the nearest start."""
        memory = self.macro_memory
        if memory.home is None:
            own = obs.get("units") or []
            memory.race = memory.race or self.race
            memory.home = memory.map.home(own[0]) if own else memory.map.starts[0]
        return system_prompt(memory) + "\n\n" + NO_MICRO

    def page(self, obs: dict) -> str:
        """This turn's page; the feedback it shows is told once."""
        text = page_text(describe(self.macro_memory, obs))
        self.macro_memory.consume()
        return text

    def command(self, text: str, obs: dict) -> tuple[list[dict], list[str]]:
        self.turn += 1
        actions, notes = self.orders.parse(text, obs, turn=self.turn)
        self.macro_memory.notes.extend(notes)
        self.pending += actions
        self.turns += [self.turn] * len(actions)
        self.landed = True
        return actions, notes

    def fight(self, name: str, obs: dict) -> str:
        """The group's menus, as wc3agent's micro asks Jev for them (one question per unit, one request per unit type):
        what the fight looks like, then each unit's options, numbered."""
        memory, micro = self.macro_memory, self.micro
        group = memory.control.groups.get(name)
        if group is None:
            raise ValueError(f"no group {name!r}; the page lists your groups, and command makes them")
        by_id = {u["unit_id"]: u for u in obs["units"] if u["hp"] > 0 and not u["structure"]}
        ids = {uid for uid in group["ids"] if uid in by_id}
        if not ids:
            raise ValueError(f"no unit of group {name} is alive and in view")
        center = {axis: sum(by_id[uid][axis] for uid in ids) / len(ids) for axis in ("x", "y")}
        groups = {name: {**group, "ids": ids, "center": center}}
        view = micro.view(obs, groups, memory.hall)
        view["army_groups"] = groups
        view["shops"] = []
        view["tech"] = dict(memory.tech)
        caps = describe_abilities(memory.catalog, view, memory.tech)
        micro.memory.observe_orders(caps)
        micro.memory.remember_forms(caps, {u["unit_id"]: u for u in view["units"]})
        menus, lines, told = [], [], False
        for type_id in sorted({by_id[uid]["type_id"] for uid in ids}):
            controlled = {uid for uid in ids if by_id[uid]["type_id"] == type_id}
            request, menu = build_request(view, controlled, group["instruction"], memory=micro.memory,
                                          names=micro.names, capabilities=caps, catalog=memory.catalog, model="jev")
            menus.append((type_id, menu))
            state = request["state"]
            if not told:
                lines += [f"FIGHT: group {name}, objective: {state['objective']}",
                          f"Situation: {state['situation']}", f"Enemies: {state['enemies']}",
                          f"Your side: {state['your_side']}", ""]
                told = True
            for uid, labels in menu.choice_keys.items():
                question = request["questions"][menu.question_names[uid]]
                unit = by_id[uid]
                lines.append(f"{menu.question_names[uid]} ({unit['hp']}/{unit['max_hp']} hp at "
                             f"({unit['x']:.0f},{unit['y']:.0f})):")
                lines += [f"  {n}. {label}: {question['criteria'][label]}" for n, label in enumerate(labels, 1)]
        self.menus[name] = (obs["game_time_seconds"], menus)
        lines += ["", "choose with this group's name and a number for each unit you direct, e.g. {\"footman1\": 2}. "
                  "Units you leave out stay on the objective. These options hold until your next advance."]
        return "\n".join(lines)

    def choose(self, name: str, picks: dict[str, int], obs: dict) -> list[dict]:
        """The picked options' actions, held a moment as wc3agent's micro holds Jev's choices."""
        made, menus = self.menus.get(name, (None, []))
        if made is None:
            raise ValueError(f"no menu for group {name}: call fight first")
        if made != obs["game_time_seconds"]:
            raise ValueError("the fight has moved on since that menu: call fight again")
        selected = []
        for _, menu in menus:
            sub = Menu()
            for uid, question in menu.question_names.items():
                if question in picks:
                    labels = list(menu.choice_keys[uid])
                    if not 1 <= picks[question] <= len(labels):
                        raise ValueError(f"{question} has options 1 to {len(labels)}")
                    sub.options[uid], sub.question_names[uid] = menu.options[uid], question
                    sub.choice_keys[uid] = menu.choice_keys[uid]
            answers = {q: {"choice": list(sub.choice_keys[uid])[picks[q] - 1]} for uid, q in sub.question_names.items()}
            selected += map_response(answers, sub)[1]
        known = {q for _, menu in menus for q in menu.question_names.values()}
        if unknown := sorted(set(picks) - known):
            raise ValueError(f"{', '.join(unknown)} not in group {name}'s menu")
        actions = [o["action"] for o in selected]
        self.micro.hold(obs, actions, self.macro_memory.control)
        self.macro_memory.fighting.add(name)
        self.pending += actions
        self.turns += [None] * len(actions)
        del self.menus[name]
        return actions

    def tick(self, obs: dict) -> tuple[list[dict], list[int | None]]:
        """One tick's actions, as wc3agent's Agent.act assembles them with its macro turn landed."""
        actions, turns = self.pending, self.turns
        self.pending, self.turns = [], []
        if self.landed:
            more = Agent.spend_skill_points(self, obs, actions) + Agent.cap_gold_workers(self, obs, actions)
            actions, turns = actions + more, turns + [None] * len(more)
            self.landed = False
        self.menus.clear()   # the game moves on: last look's menus no longer hold
        released, notes, released_turns = self.orders.release(obs)
        self.macro_memory.notes.extend(notes)
        Agent.adopt_idle_fighters(self, obs)
        actions, turns = actions + released, turns + list(released_turns)
        micro, _ = self.micro.act(obs, self.macro_memory.control, self.macro_memory.hall, self.macro_memory.tech,
                                  wait=True, start=not actions)
        return actions + micro, turns + [None] * len(micro)

    def submitted(self, actions: list[dict], turns: list, info: dict, at: float, obs: dict) -> None:
        """The step's outcome, as wc3agent's play loop records it: a building's acknowledged site, what the game took,
        then the micro's and outcomes' bookkeeping on the new observation."""
        for site in info.get("placements") or ():
            if 0 <= site.get("index", -1) < len(actions):
                args = actions[site["index"]]["arguments"]
                args.update(near=[args.get("x"), args.get("y")], x=site["x"], y=site["y"])
        rejected = info.get("rejected") or []
        refused = {r.get("index") for r in rejected}
        self.macro_memory.submitted(actions, at, turns, rejected)
        self.micro.memory.record(at, [a for i, a in enumerate(actions) if i not in refused])
        self.micro.finish(obs)
        self.macro_memory.outcomes.finish(obs["game_time_seconds"])
        self.macro_memory.outcomes.drain()


class CommanderEnv(WC3Env):
    """The env with a commander's tools in place of the raw style's unit orders."""

    act = list_units = resources = None   # the raw style's tools: a commander orders by name, through command

    def __init__(self):
        super().__init__()
        self.catalog = Catalog.load(REFERENCE)
        self.commanders: dict[int, Commander] = {}

    async def _new_game(self, scenario: dict) -> None:
        await super()._new_game(scenario)
        mapinfo = MapInfo.load(MAPS / f"{Path(scenario['map']).stem}.json")
        self.commanders = {x["slot"]: Commander(self.catalog, mapinfo, x["slot"], RACES.get(x["race"], x["race"]))
                           for x in self.players if x["computer"] is None}
        for slot, commander in self.commanders.items():
            if slot in self.obs:
                commander.see(self.obs[slot])

    def _observed(self, result: dict) -> None:
        super()._observed(result)
        for slot, commander in getattr(self, "commanders", {}).items():
            if slot in self.obs:
                commander.see(self.obs[slot])

    def _commander(self, slot: int) -> Commander:
        if slot not in self.commanders:
            raise WorkerError("unknown_player", f"player slot {slot} is not an agent in this game")
        return self.commanders[slot]

    @tool()
    async def guide(self):
        """The commander's guide: the rules, the order language command takes, your race's units and buildings, the
        items, the map, and how groups fight in this env. get_state shows it with your first page."""
        async def body():
            slot = self._slot()
            return self._commander(slot).guide(self._me(slot))
        return await self._run(body)

    @tool()
    async def get_state(self):
        """This turn's page: the time, your economy, base, heroes, army and groups, what you can do now (and what you
        can't yet, and why), the enemies you see, and how your last orders went. Your first call also shows the
        guide. Time is frozen until you call advance."""
        async def body():
            slot = self._slot()
            commander = self._commander(slot)
            first, commander.guided = not commander.guided, True
            text = commander.page(self._me(slot))
            return (commander.guide(self._me(slot)) + "\n\n" + text) if first else text
        return await self._run(body)

    @tool()
    async def command(self, orders: Annotated[str, Field(description=(
            "Orders, one per line, in the guide's order language, e.g. 'build peasant1 Farm', 'gold peasant2 "
            "peasant3', 'group army footman1 footman2 attack at 1200 -300: take creep camp 3'. Lines that are not "
            "orders are read as your plan and ignored."))]):
        """Give orders: they go to the game with your next advance. Says at once which lines became orders and which
        could not be understood; how they went in the game shows on the next page."""
        async def body():
            slot = self._slot()
            if self._out(slot):
                raise WorkerError("game_over", f"the game is over ({self._result_of(slot) or 'ended'})")
            actions, notes = self._commander(slot).command(orders, self._me(slot))
            lines = [f"Queued {len(actions)} action{'s' if len(actions) != 1 else ''} for your next advance"
                     + (":" if actions else ".")]
            lines += [f"  {self._describe(a)}" for a in actions]
            if notes:
                lines.append("Not understood:")
                lines += [f"  {n}" for n in notes]
            return "\n".join(lines)
        return await self._run(body)

    @tool()
    async def fight(self, group: Annotated[str, Field(description="A group's name, as the page lists it.")]):
        """A group's fight, unit by unit: what the fight looks like, then each unit's options right now (attacks,
        moves, spells, items), numbered. Answer with choose. Without it, the group fights by attack-move."""
        async def body():
            slot = self._slot()
            return self._commander(slot).fight(group, self._me(slot))
        return await self._run(body)

    @tool()
    async def choose(self, group: Annotated[str, Field(description="The group fight showed.")],
                     picks: Annotated[dict[str, int], Field(description=(
                         'An option number for each unit you direct, by name, e.g. {"footman1": 2}.'))]):
        """Pick options from fight's menu: they go to the game with your next advance, and hold a few seconds before
        the group is back on its objective."""
        async def body():
            slot = self._slot()
            actions = self._commander(slot).choose(group, picks, self._me(slot))
            return (f"Queued {len(actions)} action{'s' if len(actions) != 1 else ''} for your next advance"
                    + (":\n" + "\n".join(f"  {self._describe(a)}" for a in actions) if actions else "."))
        return await self._run(body)

    @tool()
    async def advance(self, seconds: Annotated[int, Field(ge=1, le=MAX_ADVANCE_SECONDS, description=(
            "Game seconds to play, 1-60, a second at a time: code acts for your groups between seconds."))] = 5):
        """Send your orders and let the game run, a second at a time, with code acting for your groups between
        seconds. Returns the new page."""
        async def first():
            slot = self._slot()
            if self._out(slot):
                raise WorkerError("game_over", f"the game is over ({self._result_of(slot) or 'ended'}); get_state "
                                               "shows the end")
            return slot, self._seconds()
        slot, start = await self._run(first)
        commander = self.commanders[slot]
        for _ in range(seconds):
            if self._out(slot):
                break

            async def tick():
                return commander.tick(self._me(slot)), self._seconds()
            (actions, turns), at = await self._run(tick, count=False)
            try:
                info = await self._play(slot, actions, TICK_MS)
            except WorkerError as e:
                return await self._run(partial(_raise, e), count=False)

            async def after(actions=actions, turns=turns, info=info, at=at):
                commander.submitted(actions, turns, info, at, self._me(slot))
            await self._run(after, count=False)

        async def report():
            self.stats["advances"] += 1
            head = f"Advanced {render.clock(start)} → {render.clock(self._seconds())}."
            if result := self._result_of(slot):
                head += f"\nGAME OVER: {render.RESULTS.get(result, result)}. Reply with your result."
            return head + "\n\n" + commander.page(self._me(slot))
        return await self._run(report, count=False)
