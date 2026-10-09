"""The env's tools against wc3env's fake game: a Town Hall, five Peasants, and an enemy hall far away."""


import pytest
from agentenv_game import GameError, LobbyStatus
from conftest import new_game

from agentenv_wc3 import refusals, render

pytestmark = pytest.mark.anyio

PEASANT, HALL, ENEMY_HALL = 1001, 1000, 2000


async def test_get_state_starts_a_game_and_names_types(tools):
    text = await tools("get_state")
    assert "you are player 0, Human" in text
    assert "5 Peasant" in text
    assert "1000 Town Hall (htow)" in text
    assert "IDLE WORKERS: 1001, 1002, 1003, 1004, 1005" in text
    assert "Start locations" in text   # from wc3env's prepared Echo Isles facts
    assert text.endswith("[t 0:00/2:00 · gold 500 · lumber 0 · food 5/12]")


async def test_orders_wait_for_advance(tools, env):
    queued = await tools("act", actions=[{"unit_id": PEASANT, "command": "move",
                                          "arguments": {"x": -4500, "y": 2800}}])
    assert "Queued 1 order: 1001 Peasant move at (-4500,2800)" in queued
    assert "1 queued" in queued
    before = next(u for u in env.obs[0]["units"] if u["unit_id"] == PEASANT)
    report = await tools("advance", seconds=2)
    assert report.startswith("Advanced 0:00 → 0:02.\nSent 1 order.")
    after = next(u for u in env.obs[0]["units"] if u["unit_id"] == PEASANT)
    assert after["x"] > before["x"]
    assert env.queue[0] == []
    assert "IDLE WORKERS:" in report   # the fake reports no orders, so even the mover looks idle


async def test_act_refuses_what_wc3env_would(tools, env):
    error = await tools.error("act", actions=[{"unit_id": ENEMY_HALL, "command": "stop"}])
    assert "bad_actions" in error and "2000" in error
    assert env.queue[0] == []
    error = await tools.error("act", actions=[{"unit_id": PEASANT, "command": "dance"}])
    assert "command" in error   # pydantic's own check of the command list


async def test_type_names_resolve_to_ids(env, tools):
    await tools("get_state")
    action = env._resolve(0, {"unit_id": HALL, "command": "train", "arguments": {"type_id": "Peasant"}})
    assert action == {"unit_id": HALL, "command": "train", "arguments": {"type_id": "hpea"}}
    action = env._resolve(0, {"unit_id": PEASANT, "command": "build",
                           "arguments": {"type_id": "farm", "x": 1, "y": 2, "auto_place": True}})
    assert action["arguments"]["type_id"] == "hhou"
    assert env._resolve(0, {"unit_id": PEASANT, "command": "stop", "arguments": {}}) == {"unit_id": PEASANT,
                                                                                      "command": "stop"}


async def test_a_name_several_types_share_resolves_to_the_one_the_unit_makes(env, tools):
    await tools("get_state")
    env.obs[0]["units"].append({"unit_id": 78, "type_id": "halt", "owner": 0, "x": 0, "y": 0, "hp": 900,
                                "max_hp": 900, "structure": True})

    def made(unit_id, command, type_id):
        return env._resolve(0, {"unit_id": unit_id, "command": command,
                                "arguments": {"type_id": type_id}})["arguments"]["type_id"]

    assert made(PEASANT, "build", "Barracks") == "hbar"   # not the Orc one
    assert (made(78, "train", "Archmage"), made(78, "train", "mountain king")) == ("Hamg", "Hmkg")
    assert made(HALL, "train", "Keep") == "hkee"


async def test_an_order_the_unit_cannot_make_is_refused_with_who_can(tools):
    error = await tools.error("act", actions=[{"unit_id": HALL, "command": "build",
                                               "arguments": {"type_id": "Farm", "auto_place": True, "x": 0, "y": 0}}])
    assert "1000 Town Hall can't build Farm; your Peasant can" in error
    error = await tools.error("act", actions=[{"unit_id": HALL, "command": "train",
                                               "arguments": {"type_id": "Footman"}}])
    assert "can't train Footman; none of your units can yet (Barracks can)" in error


async def test_the_game_ends_at_its_time_limit(tools, env):
    for _ in range(2):
        report = await tools("advance", seconds=60)
    assert "GAME OVER: TIME LIMIT" in report
    assert env._seconds() == 120
    assert "game_over" in await tools.error("advance")
    summary = (await env.data_get())[0].data
    assert summary["game_over"] and summary["result"] == "time_limit"


async def test_a_won_game_reports_victory(tools, env):
    await tools("get_state")
    await env.bridge.call("debug", op="end", args={"result": "victory"})
    report = await tools("advance", seconds=5)
    assert "GAME OVER: VICTORY" in report
    summary = (await env.data_get())[0].data
    assert summary["result"] == "victory"
    assert summary["harness"]["advances"] == 1


async def test_lookup_and_resources(tools):
    text = await tools("lookup", query="Footman")
    assert "Footman (hfoo)" in text and "135 gold" in text
    assert "Nothing named" in await tools("lookup", query="zzzz")
    assert "Gold mines in view" in await tools("resources")


async def test_list_units_filters(tools):
    text = await tools("list_units", type="Peasant", near=HALL, limit=2)
    assert text.count("Peasant (hpea)") == 2 and "and 3 more" in text
    assert "2000" in await tools("list_units", who="enemy")


async def test_data_get_and_new_game(env):
    summary = (await env.data_get())[0].data
    assert summary["game_time_seconds"] == 0 and summary["units"] == 5 and summary["structures"] == 1
    assert summary["opponent_structures"] == 1
    insane = [{"agent": "wc3", "race": "human"}, {"computer": "insane", "race": "orc"}]
    result = await new_game(env, insane, time_limit_seconds=600, seed=7)
    assert result.status is LobbyStatus.CLOSED and env.setup
    assert env.scenario["seed"] == 7 and env.scenario["ai_difficulty"] == "insane" and env.stats["games"] == 2
    with pytest.raises(GameError, match="game_settings.ai_level: Input should be 'easy', 'normal' or 'insane'"):
        await new_game(env, [{"agent": "wc3"}, {"computer": "godlike"}])


async def test_a_cancelled_game_keeps_its_timeline_but_no_replay(env):
    await new_game(env)
    env.cancel_match()
    kept = await env.list_match_files(["replay", "timeline"])
    assert [f["kind"] for f in kept["files"]] == ["timeline"]
    assert kept["notes"] == ["no replay: the game did not reach its end"]


def test_render_events_fold_attacks():
    ref, names = render.Reference({}), render.Names()
    events = [{"kind": "attacked", "unit_id": 5, "attacker_id": 9}] * 3 + [{"kind": "death", "unit_id": 5,
                                                                             "type_id": "hfoo", "owner": 0}]
    assert render.events_text(events, ref, names, 0) == ["attacked: 5 by 9 (x3)", "died: 5 hfoo (yours)"]


async def test_a_dead_worker_fails_the_game_and_data_get_says_so(tools, env):
    await tools("get_state")
    env.bridge.proc.kill()
    await env.bridge.proc.wait()
    error = await tools.error("advance")
    assert "worker_down" in error
    assert "game_failed" in await tools.error("get_state")
    summary = (await env.data_get())[0].data
    assert summary["engine_failed"] and "exited" in summary["error"]
    await new_game(env)   # a new game starts a new worker
    assert "5 Peasant" in await tools("get_state")


def test_license_files_are_linked_not_copied_into_the_game(tmp_path):
    from agentenv_wc3.server import attach_license

    game, store = tmp_path / "game", tmp_path / "store"
    game.mkdir()
    files = {"roc.w3k": b"roc", "tft.w3k": b"tft"}
    attach_license({"roc.w3k": files["roc.w3k"]}, store, game)   # a file at a time
    attach_license({"tft.w3k": files["tft.w3k"]}, store, game)
    assert (game / "roc.w3k").is_symlink() and (game / "roc.w3k").read_bytes() == b"roc"
    assert oct((store / "tft.w3k").stat().st_mode & 0o777) == "0o600"
    attach_license(files, store, game)   # again: the same links
    (game / "roc.w3k").unlink()
    (game / "roc.w3k").write_bytes(b"baked")
    with pytest.raises(ValueError, match="baked in"):
        attach_license(files, store, game)


async def test_cast_and_learn_take_names(env, tools):
    await tools("get_state")
    hero = {"unit_id": 77, "type_id": "Hamg", "owner": 0, "x": 0, "y": 0, "hp": 450, "max_hp": 450,
            "abilities": [{"ability_id": "AHbz", "level": 1}]}
    env.obs[0]["units"].append(hero)
    cast = env._resolve(0, {"unit_id": 77, "command": "cast", "arguments": {"order": "Blizzard", "x": 1, "y": 2}})
    assert cast["arguments"]["order"] == "blizzard"
    learn = env._resolve(0, {"unit_id": 77, "command": "learn", "arguments": {"ability_id": "brilliance aura"}})
    assert learn["arguments"]["ability_id"] == "AHab"


async def test_orders_that_stopped_applying_are_dropped_not_blocking(tools, env):
    await tools("act", actions=[{"unit_id": PEASANT, "command": "stop"},
                                {"unit_id": PEASANT + 1, "command": "move", "arguments": {"x": 0, "y": 0}}])
    env.queue[0][1]["unit_id"] = 4242   # as if the second Peasant had died since: an id the game no longer has
    report = await tools("advance", seconds=1)
    assert "Dropped 1 queued order that no longer applied:\n  4242 move at (0,0)" in report
    assert "Sent 1 order." in report


def test_get_state_lists_each_hero_with_its_items_by_the_slot_use_item_takes():
    ref = render.Reference.load()
    hero = {"unit_id": 7, "type_id": "Hpal", "x": 0, "y": 0, "hp": 650, "max_hp": 650, "mana": 255, "max_mana": 255,
            "hero": True, "level": 2, "order": None}
    obs = {"units": [hero], "player": {}, "inventory": [{"unit_id": 7, "slot": 1, "type_id": "phea", "charges": 1},
                                                        {"unit_id": 7, "slot": 0, "type_id": "stel", "charges": 0}]}
    text = render.state(obs, ref, me=0, race="human", map_name="(2)EchoIsles.w3x", limit=600, queued=0, result="",
                        events=[])
    items = "items: slot 0 Staff of Teleportation (stel), slot 1 Potion of Healing (phea) x1"
    assert "Heroes:\n  7 Paladin (Hpal)" in text and items in text
    listed = render.units_list(obs, ref, who="own", type_filter=None, near=None, radius=None, details=False, limit=9)
    assert items in listed


def test_get_state_shows_a_heros_skills_and_the_points_it_has_to_spend():
    ref = render.Reference.load()
    hero = {"unit_id": 7, "type_id": "Hamg", "x": 0, "y": 0, "hp": 500, "max_hp": 500, "mana": 300, "max_mana": 300,
            "hero": True, "level": 3, "order": None, "abilities": [{"ability_id": "AHbz", "level": 1}]}
    text = render.state({"units": [hero], "player": {}}, ref, me=0, race="human", map_name="(2)EchoIsles.w3x",
                        limit=600, queued=0, result="", events=[])
    assert "skills Blizzard (AHbz) 1 · 2 skill points to spend: learn AHbz or AHab or AHwe" in text
    spent = {**hero, "abilities": [{"ability_id": "AHbz", "level": 2}, {"ability_id": "AHab", "level": 1}]}
    assert render.skills_text(spent, ref) == "skills Blizzard (AHbz) 2, Brilliance Aura (AHab) 1"


def test_an_order_the_game_dropped_says_what_it_lacked_and_one_it_took_says_nothing():
    ref = render.Reference.load()

    def obs(gold, lumber, used, cap, *units):
        return {"player": {"gold": gold, "lumber": lumber, "food_used": used, "food_cap": cap},
                "units": [{"unit_id": 1000, "type_id": "htow", "structure": True},
                          {"unit_id": 78, "type_id": "halt", "structure": True}, *units]}

    def train(unit_id, type_id):
        return {"unit_id": unit_id, "command": "train", "arguments": {"type_id": type_id}}

    peasants = [train(1000, "hpea")] * 7
    assert refusals.unstarted(obs(500, 0, 5, 12), obs(500, 0, 5, 12), peasants, set(), ref, "human") == {
        6: "didn't start: not enough gold (needs 75, you had 50)"}
    assert refusals.unstarted(obs(500, 0, 5, 12), obs(500, 0, 5, 12), peasants, {0}, ref, "human") == {}
    capped = obs(1245, 300, 18, 18)
    assert refusals.unstarted(capped, capped, [train(78, "Hamg")], set(), ref, "human") == {
        0: "didn't start: not enough food (needs 5, 0 of 18 free: build another Farm)"}
    queued = {**capped, "units": [capped["units"][0], {"unit_id": 78, "type_id": "halt", "structure": True,
                                                       "queue": ["Hamg"]}]}
    assert refusals.unstarted(capped, queued, [train(78, "Hamg")], set(), ref, "human") == {}
    archmage = {"unit_id": 5, "type_id": "Hamg", "hero": True}
    second = refusals.unstarted(obs(900, 300, 10, 30, archmage), obs(900, 300, 10, 30, archmage),
                                [train(78, "Hmkg")], set(), ref, "human")
    assert second == {0: "didn't start: a second hero requires Keep"}
    castle = obs(900, 300, 10, 30, archmage, {"unit_id": 6, "type_id": "hcas", "structure": True})
    assert refusals.unstarted(castle, castle, [train(78, "Hmkg")], set(), ref, "human") == {}
    knights = refusals.unstarted(obs(900, 300, 10, 30), obs(900, 300, 10, 30), [train(1000, "hkni")], set(), ref,
                                 "human")
    assert knights == {0: "didn't start: requires Lumber Mill, Castle, Blacksmith"}
