"""The RTS casters (agentenv_rts/streamer/caster.py) against a fake env that serves casting.json from a real Timeline
and a fake OpenAI-compatible endpoint that speaks the tool-calling protocol: what they say and when, how they look
the match up and research it, what they serve the stream page, and that failures neither stop them nor print the
key."""

import array
import codecs
import json
import math
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agentenv_rts import live
from agentenv_rts.streamer import caster
from agentenv_rts.timeline import Timeline

KEY = "sk-test-not-for-printing-123"
WAV_SECONDS = 2.0
DESK = "A test RTS: each side builds a base, and destroying every building of the other side wins."
CLAUDE, ORC = "Claude Sonnet 5.5", "Orc AI (normal)"
SPEAKERS = (caster.PBP, caster.COLOR)


def streamed_wav(seconds: float = WAV_SECONDS, rate: int = 8000) -> bytes:
    """Silence as a text-to-speech API streams it: 16-bit mono, the RIFF and data sizes left at 0xFFFFFFFF."""
    fmt = (1).to_bytes(2, "little") + (1).to_bytes(2, "little") + rate.to_bytes(4, "little") + \
        (rate * 2).to_bytes(4, "little") + (2).to_bytes(2, "little") + (16).to_bytes(2, "little")
    unknown = b"\xff\xff\xff\xff"
    return b"RIFF" + unknown + b"WAVE" + b"fmt " + (16).to_bytes(4, "little") + fmt + b"data" + unknown + \
        bytes(int(seconds * rate * 2))


def frame(t: float, *, score=(100, 80), army=(300, 200), events=(), notes=(), result="", agent=None) -> dict:
    players = {str(s): {"gold": 500 - 100 * s, "lumber": 150, "food": [12, 20], "score": score[s], "units": 9 - s,
                        "structures": 4, "army": army[s]} for s in (0, 1)}
    if agent:
        players["0"]["agent"] = agent
    return {"t": t, "players": players, "units": [], "events": list(events), "notes": list(notes), "result": result}


def moment(text: str, side: int | None, kind: str, major: bool = True) -> dict:
    return {"text": text, "side": side, "kind": kind, "major": major}


def plan(text: str, slot: int = 0) -> dict:
    return {"slot": slot, "kind": "plan", "text": text}


def say(text: str, focus=None, ident: str = "call_say") -> dict:
    return calls(("say", {"text": text, "focus": focus}), ident=ident)


def calls(*made, ident: str = "call") -> dict:
    """A reply with tool calls: (name, arguments) each, the arguments an object or the raw JSON text."""
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": f"{ident}_{n}", "type": "function",
         "function": {"name": name, "arguments": args if isinstance(args, str) else json.dumps(args)}}
        for n, (name, args) in enumerate(made)]}


def tools_of(body: dict) -> set[str]:
    return {t["function"]["name"] for t in body.get("tools") or ()}


class Fake:
    """One server for both: the env's casting.json (live.casting over a Timeline) and LiteLLM (chat and speech)."""

    def __init__(self):
        self.timeline: Timeline | None = None
        self.data_down = False
        self.prompts: list[str] = []   # the user prompt of each line's first call (and each research session's)
        self.chats: list[dict] = []
        self.auth: list[str] = []
        self.speech: list[dict] = []
        self.chat_failures = self.speech_failures = 0
        self.reject_tools = self.reject_forced = False
        self.script: list = []         # replies (or functions of the request) to give first, in order
        self.content: str | None = None    # a plain-text reply instead of a say call
        self.said = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def start(self, *frames: dict, game: str = "g-1") -> None:
        self.timeline = Timeline({"game": game, "title": "Test RTS · Arena", "time_limit": 1200, "players": [
            {"slot": 0, "label": CLAUDE, "race": "human", "controller": "agent", "color": "#e0473d"},
            {"slot": 1, "label": ORC, "race": "orc", "controller": "the game's AI", "color": "#3d7fe0"}]})
        self.add(*frames)

    def add(self, *frames: dict) -> None:
        for f in frames:
            self.timeline.add(f)

    def chat(self, body: dict) -> dict:
        self.chats.append(body)
        if len(body["messages"]) == 2:
            self.prompts.append(body["messages"][1]["content"])
        if self.script:
            step = self.script.pop(0)
            return step(body) if callable(step) else step
        if self.content is not None:
            return {"role": "assistant", "content": self.content}
        if "jot" in tools_of(body):
            return {"role": "assistant", "content": "Nothing worth jotting."}
        self.said += 1
        text, focus = f"Line {self.said}: Claude leads.", CLAUDE if self.said % 2 else "Atlantis"
        if "tools" not in body:
            return {"role": "assistant", "content": json.dumps({"text": text, "focus": focus})}
        return say(text, focus)

    def handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                url = urllib.parse.urlsplit(self.path)
                if fake.data_down or url.path != "/live/casting.json":
                    return self.reply(503, b"down", "text/plain")
                since = urllib.parse.parse_qs(url.query).get("since", [None])[0]
                self.reply(200, json.dumps(live.casting(fake.timeline, since, DESK)).encode(), "application/json")

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.auth.append(self.headers["Authorization"])
                if self.path == "/v1/chat/completions":
                    if fake.chat_failures:
                        fake.chat_failures -= 1
                        return self.reply(500, f"bad key {KEY}".encode(), "text/plain")
                    if fake.reject_tools and "tools" in body:
                        fake.chats.append(body)
                        return self.reply(400, b'{"error": "this model does not support tools"}', "text/plain")
                    if fake.reject_forced and "tool_choice" in body:
                        fake.chats.append(body)
                        return self.reply(400, b'{"error": "tool_choice is not supported with thinking"}',
                                          "text/plain")
                    reply = {"choices": [{"message": fake.chat(body), "finish_reason": "stop"}]}
                    return self.reply(200, json.dumps(reply).encode(), "application/json")
                fake.speech.append(body)
                if fake.speech_failures:
                    fake.speech_failures -= 1
                    return self.reply(429, b"slow down", "text/plain")
                self.reply(200, streamed_wav(), "audio/wav")

            def reply(self, status, body, content_type):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        return Handler


@pytest.fixture
def fake():
    f = Fake()
    threading.Thread(target=f.server.serve_forever, daemon=True).start()
    yield f
    f.server.shutdown()


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def new_caster(fake: Fake, clock: Clock, **kw) -> caster.Caster:
    return caster.Caster(f"{fake.url}/live?stream", fake.url + "/v1/", KEY, clock=clock, **kw)


def until_quiet(c: caster.Caster, clock: Clock) -> None:
    """Moves the clock to when the next line is due: less than LEAD_SECONDS of speech left."""
    clock.now = max(clock.now + caster.POLL_SECONDS, c.speaking_until - caster.LEAD_SECONDS + 0.1)


def beat(c: caster.Caster, clock: Clock) -> list[dict]:
    """Runs the beat under way, or the next one, to its end; returns the lines it said."""
    said = len(c.lines)
    for _ in range(caster.MAX_LINES + 1):
        until_quiet(c, clock)
        c.tick()
        if c.beat is not None and c.beat.done and len(c.lines) > said:
            break
    return c.lines[said:]


def now_part(prompt: str) -> str:
    return prompt.split("NOW\n")[1]


def test_the_intro_alternates_speakers_and_each_line_answers_the_last(fake):
    clock = Clock()
    c = new_caster(fake, clock, title="Claude vs the Horde")
    c.tick()
    assert c.lines == [] and fake.prompts == []   # no game yet
    fake.start(frame(0), frame(30))
    intro = beat(c, clock)
    assert [(line["kind"], line["speaker"], line["name"]) for line in intro] == [
        ("intro", "pbp", "Max"), ("intro", "color", "Ada"), ("intro", "pbp", "Max")]
    assert intro[0] == {"id": 1, "speaker": "pbp", "name": "Max", "text": "Line 1: Claude leads.",
                        "audio": "audio/1.wav", "seconds": WAV_SECONDS, "t": 30, "focus": 0, "kind": "intro"}
    assert intro[1]["focus"] is None   # not a player in this game
    first, second, third = fake.prompts[:3]
    assert '"Claude vs the Horde"' in first and f"{CLAUDE} (human), agent; {ORC} (orc), the game's AI" in first
    assert "with a 20-minute time limit" in first and "Clock: 0:30 of 20:00, 19:30 left" in first
    assert (f"1. {CLAUDE} (human), agent: score 100; army 300; 500 gold, 150 lumber; food 12 of 20; 9 units, "
            "4 structures") in first
    assert f"Leader: {CLAUDE} (human), ahead on score since 0:00, by 20" in first
    assert "YOUR LINE: you are Max, line 1 of 3, 6 to 16 words. You open this exchange." in first
    assert 'YOUR LINE: you are Ada, line 2 of 3, at most 22 words. Max just said: "Line 1: Claude leads."' in second
    assert 'Ada just said: "Line 2: Claude leads."\nYours is the last line of this exchange: answer Ada' in third
    assert "RECENT LINES (oldest first)\nMax: Line 1: Claude leads.\nAda: Line 2: Claude leads." in third
    systems = [b["messages"][0]["content"] for b in fake.chats[:3]]
    assert systems[0].startswith("You are Max, the play-by-play caster on the live broadcast of a real-time strategy")
    assert systems[1].startswith("You are Ada, the colour analyst caster") and systems[0] == systems[2]
    assert "two AI casters" in systems[0] and f"THE GAME, as the env describes it: {DESK}" in systems[0]
    assert all(tools_of(b) == {"say"} for b in fake.chats[:3])   # the opening: nothing queued, it must speak
    assert all(b["model"] == caster.MODEL and b["max_tokens"] == 1500 and "temperature" not in b for b in fake.chats)
    assert [(s["voice"], s["instructions"]) for s in fake.speech] == [
        ("ash", caster.CASTERS["pbp"][2]), ("sage", caster.CASTERS["color"][2]), ("ash", caster.CASTERS["pbp"][2])]
    assert {(s["model"], s["response_format"]) for s in fake.speech} == {(caster.TTS_MODEL, "wav")}
    assert fake.speech[0]["input"] == "Line 1: Claude leads." and set(fake.auth) == {f"Bearer {KEY}"}


def test_major_moments_are_news_and_the_rest_is_data(fake):
    clock = Clock()
    c = new_caster(fake, clock)
    fake.start(frame(0), frame(30))
    beat(c, clock)
    fake.add(frame(60, events=[moment(f"{CLAUDE} reached tier 2: Keep", 0, "tier"),
                               moment(f"{CLAUDE} trained a Footman", 0, "train", major=False),
                               moment(f"{ORC} lost a Barracks", 1, "death")]))
    asked = len(fake.prompts)
    called = beat(c, clock)
    said = now_part(fake.prompts[asked])
    assert [line["kind"] for line in called] == ["news"] * 3
    assert said.index(f"- 1:00: {ORC} lost a Barracks") < said.index("- 1:00: Claude Sonnet 5.5 reached tier 2")
    assert "Footman" not in said and f"- 1:00: {CLAUDE} trained a Footman" in fake.prompts[asked]   # in the DATA
    asked = len(fake.prompts)
    assert [line["kind"] for line in beat(c, clock)] == ["color"] * 2   # the moments were called once
    assert "you are Ada, line 1 of 2, at most 22 words" in fake.prompts[asked]
    assert "The angle: the lead" in fake.prompts[asked]


def test_joining_a_game_under_way_calls_only_its_latest_moments(fake):
    clock = Clock()
    c = new_caster(fake, clock)
    fake.start(frame(0), frame(30, events=[moment("an old fight", 0, "fight")]),
               *[frame(t) for t in range(60, 300, 30)],
               frame(300, events=[moment(f"{ORC} expanded: a Great Hall at a gold mine", 1, "expand")]))
    beat(c, clock)
    assert "We join 5:00 in, with the game under way." in fake.prompts[0]
    beat(c, clock)
    said = now_part(fake.prompts[3])
    assert "expanded" in said and "an old fight" not in said


def test_breaking_news_cuts_into_the_talk(fake):
    clock = Clock()
    c = new_caster(fake, clock)
    fake.start(frame(0), frame(30))
    beat(c, clock)
    until_quiet(c, clock)
    c.tick()
    assert c.beat.kind == "color" and len(c.beat.lines) == 1   # the analysis is under way
    fake.add(frame(60, events=[moment(f"{ORC}'s Blademaster has fallen", 1, "death")]))
    asked = len(fake.prompts)
    lines = beat(c, clock)
    assert [(line["kind"], line["speaker"]) for line in lines] == [("news", "pbp"), ("news", "color")]
    assert "You cut in: the desk was on something else, and this news can't wait." in fake.prompts[asked]
    assert "Blademaster has fallen" in now_part(fake.prompts[asked])
    fake.add(frame(90, events=[moment(f"{CLAUDE}'s Archmage reached level 3", 0, "level")]))
    until_quiet(c, clock)
    c.tick()
    until_quiet(c, clock)
    c.tick()   # a level is news, but not news that cuts in
    assert c.beat.kind == "news" and c.beat.lines and not c.beat.cut_in


def test_plans_are_read_out_between_the_analysis(fake):
    clock = Clock()
    c = new_caster(fake, clock)
    fake.start(frame(0), frame(30))
    beat(c, clock)
    fake.add(frame(60, notes=[plan("Mass Footmen, then push at six minutes.")]), frame(90, notes=[plan("Hold.", 1)]))
    kinds, firsts = [], []
    for n in range(3):
        if n == 2:
            fake.add(frame(120, notes=[plan("Expand to the north gold mine.")]))
        firsts.append(len(fake.prompts))
        kinds.append(beat(c, clock)[-1]["kind"])
    assert kinds == ["plans", "color", "plans"]
    said = now_part(fake.prompts[firsts[0]])
    assert f'- {CLAUDE} (human), at 1:00: "Mass Footmen, then push at six minutes."' in said
    assert f'- {ORC} (orc), at 1:30: "Hold."' in said and "relays one, paraphrased" in said
    assert '   plan (1:00): "Mass Footmen, then push at six minutes."' in fake.prompts[firsts[0]]
    assert "Expand to the north gold mine." in now_part(fake.prompts[firsts[2]])


def test_game_over_brings_the_outro_and_then_silence(fake):
    clock = Clock()
    c = new_caster(fake, clock)
    fake.start(frame(0), frame(30))
    beat(c, clock)
    fake.add(frame(60, score=(400, 50), result="victory"))
    clock.now += caster.POLL_SECONDS
    for _ in range(3):
        c.tick()   # at once, line after line: the stream ends soon after the game does
    assert [line["kind"] for line in c.lines[-3:]] == ["outro"] * 3 and c.finished
    assert [line["speaker"] for line in c.lines[-3:]] == ["pbp", "color", "pbp"]
    outro = fake.prompts[-1]
    assert (f"The game is over: {CLAUDE} (human) wins, at 1:00; the final scores: {CLAUDE} (human) 400, "
            f"{ORC} (orc) 50") in outro
    assert "Clock: 1:00 of 20:00, 19:00 left (GAME OVER)" in outro and f"Result: {CLAUDE} (human) wins" in outro
    said = len(c.lines)
    for _ in range(3):
        until_quiet(c, clock)
        c.tick()
    assert len(c.lines) == said   # it stops talking
    c.match.result = "defeat"
    assert c.result().startswith(f"{CLAUDE} (human) is defeated: {ORC} (orc) wins, at 1:00")
    c.match.result = "time_limit"
    assert c.result().startswith("time ran out at the limit, undecided, at 1:00")


def test_the_casters_pace_themselves_to_their_audio(fake):
    clock = Clock()
    c = new_caster(fake, clock)
    fake.start(frame(0), frame(30))
    c.tick()
    assert len(fake.prompts) == 1 and len(c.lines) == 1   # the page plays the first line as the next is written
    assert c.speaking_until == pytest.approx(clock.now + WAV_SECONDS + caster.GAP_SECONDS)
    c.tick()
    assert len(c.lines) == 2   # due at once: less than LEAD_SECONDS of speech left
    c.tick()
    assert len(c.lines) == 2   # plenty is queued
    clock.now = c.speaking_until - caster.LEAD_SECONDS - 0.5
    c.tick()
    assert len(fake.prompts) == 2
    clock.now += 1
    c.tick()
    assert len(fake.prompts) == 3


def test_the_page_gets_the_lines_and_their_audio_from_anywhere(fake):
    clock = Clock()
    c = new_caster(fake, clock)
    fake.start(frame(0), frame(30))
    beat(c, clock)
    server = ThreadingHTTPServer(("127.0.0.1", 0), caster.handler(c))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(f"{base}/cast.json?since=1") as r:
            assert r.headers["Access-Control-Allow-Origin"] == "*"
            body = json.load(r)
        assert [line["id"] for line in body["lines"]] == [2, 3]
        assert body["speaking_until"] == pytest.approx(c.speaking_until, abs=0.01)
        with urllib.request.urlopen(f"{base}/audio/2.wav") as r:
            wav = r.read()
            assert r.headers["Content-Type"] == "audio/wav" and r.headers["Access-Control-Allow-Origin"] == "*"
        assert int.from_bytes(wav[4:8], "little") == len(wav) - 8   # the streamed sizes are filled in
        assert int.from_bytes(wav[40:44], "little") == len(wav) - 44
        with urllib.request.urlopen(f"{base}/health") as r:
            assert r.read() == b"ok" and r.headers["Access-Control-Allow-Origin"] == "*"
        for path, status in (("/audio/9.wav", 404), ("/cast.json?since=x", 400)):
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + path)
            assert e.value.code == status and e.value.headers["Access-Control-Allow-Origin"] == "*"
    finally:
        server.shutdown()


def test_a_caster_looks_the_match_up_before_it_speaks(fake):
    clock = Clock()
    c = new_caster(fake, clock)
    fake.start(*[frame(t, score=(100 + t, 80 + t // 2)) for t in range(0, 390, 30)])
    beat(c, clock)
    c.call_seconds, c.voice_seconds = dict.fromkeys(c.models.values(), 0.5), 0.5
    c.speaking_until += 5   # a long line queued: time for a round of lookups

    def answered(body: dict) -> dict:   # the lookups' answers come back under their ids, in order
        answers = [m for m in body["messages"] if m["role"] == "tool"]
        assert [m["tool_call_id"] for m in answers] == ["look_0", "look_1"]
        assert body["messages"][2]["role"] == "assistant" and body["messages"][2]["tool_calls"][0]["id"] == "look_0"
        assert answers[0]["content"].startswith(f"{CLAUDE} (human), agent: 1 of 2 on score at 6:00\nscore 460;")
        assert "Before: 5:00: score 400, army 300; 3:00: score 280, army 300; 1:00: score 160, army 300" in (
            answers[0]["content"])
        assert answers[1]["content"].startswith(f"score, 0:00 to 6:00:\n{CLAUDE} (human): 0:00: 100, ")
        assert "tool_choice" not in body
        return say("Up three hundred and sixty in six minutes, Max.", CLAUDE)

    fake.script = [calls(("player", {"player": CLAUDE}), ("trend", {"stat": "score"}), ident="look"), answered]
    lines = beat(c, clock)
    assert lines[0]["text"] == "Up three hundred and sixty in six minutes, Max."
    assert lines[0]["focus"] == 0 and lines[0]["speaker"] == "color"
    assert len([b for b in fake.chats if b["messages"][1]["content"] == fake.prompts[3]]) == 2   # one line, 2 calls


def test_the_lookups_answer_from_the_match_and_errors_are_answers(fake):
    clock = Clock()
    c = new_caster(fake, clock)
    assert c.lookup("standings", {}) == "error: the game hasn't started"
    fake.start(frame(0), frame(30, events=[moment(f"Fight at the middle of the map: {CLAUDE} vs {ORC}", 0, "fight")]),
               frame(60, events=[moment(f"Fight at the middle of the map over after 25 s: {ORC} lost 2 Grunt", None,
                                        "fight_end"),
                                 moment(f"{CLAUDE} trained a Footman", 0, "train", major=False)],
                     notes=[plan("Push now.")], agent={"cost_usd": 0.42, "decisions": 12, "tokens": 34567}))
    beat(c, clock)
    assert c.lookup("standings", {"t": 5}) == (
        "(ignored t: standings takes none)\nAt 1:00:\n"
        f"1. {CLAUDE} (human), agent: score 100; army 300; 500 gold, 150 lumber; food 12 of 20; 9 units, 4 structures; "
        '12 decisions, 34,567 tokens, $0.42 so far\n   plan (1:00): "Push now."\n'
        f"2. {ORC} (orc), the game's AI: score 80; army 200; 400 gold, 150 lumber; food 12 of 20; 8 units, "
        "4 structures")
    orc = c.lookup("player", {"player": "orc"})   # by faction, which one player has
    assert orc.startswith(f"{ORC} (orc), the game's AI: 2 of 2 on score at 1:00\n")
    assert "Feed: 1 fight, 1 fight_end\n- 0:30: Fight at the middle of the map: " in orc   # a fight names both sides
    assert "Its AI: 12 decisions, 34,567 tokens, $0.42 so far" in c.lookup("player", {"player": 0})
    assert c.lookup("moments", {"kind": "fight"}) == (
        f"1 moments:\n- 0:30: Fight at the middle of the map: {CLAUDE} vs {ORC} (major)")
    assert c.lookup("moments", {"kind": "dance"}) == "No such moments; the kinds in this game: fight, fight_end, train"
    assert c.lookup("moments", {"side": CLAUDE, "since_t": 45}) == f"1 moments:\n- 1:00: {CLAUDE} trained a Footman"
    assert c.lookup("notes", {}) == f'1 notes:\n- 1:00, {CLAUDE} (human): "Push now."'
    assert c.lookup("notes", {"player": "1"}) == f"No notes from {ORC} (orc) yet"
    assert c.lookup("trend", {"stat": "army", "since_t": 30}) == (
        f"army, 0:30 to 1:00:\n{CLAUDE} (human): 0:30: 300, 1:00: 300 (+0 over these)\n"
        f"{ORC} (orc): 0:30: 200, 1:00: 200 (+0 over these)")
    assert c.lookup("said", {"query": "claude LEADS"}).startswith("3 lines:\n- 1:00, Max: Line 1: Claude leads.")
    assert c.lookup("said", {"query": "Atlantis"}) == "The desk hasn't said that yet"
    errors = {("teleport", "{}"): "error: there is no tool 'teleport'; the tools: standings, player, trend, moments, "
                                  "notes, said, say",
              ("player", "{not json"): "error: the arguments are not JSON: '{not json'",
              ("player", '{"player": "Atlantis"}'): f"error: no player 'Atlantis'; the players: {CLAUDE} (human) "
                                                    f"(slot 0), {ORC} (orc) (slot 1)",
              ("player", '{"name": "x"}'): "error: player needs player; it takes player, got name",
              ("moments", '["kind"]'): "error: the arguments must be an object",
              ("moments", '{"since_t": "soon"}'): "error: moments failed on those arguments (ValueError)",
              ("trend", '{"stat": "gold"}'): "error: no stat 'gold'; the stats: score, army",
              ("trend", '{"stat": "score", "since_t": 999}'): "error: no history since 16:39: the game is at 1:00"}
    for (name, args), error in errors.items():
        assert c.lookup(name, args) == error


def test_a_refused_forced_say_falls_back_to_asking(fake, capsys):
    clock = Clock()
    c = new_caster(fake, clock)
    fake.start(frame(0), frame(30))
    fake.reject_forced = True
    c.tick()   # nothing queued, so the line is forced: refused, then asked with say alone
    assert c.lines[0]["text"] == "Line 1: Claude leads." and c.no_force == {c.models["pbp"]}
    assert "tool_choice" in fake.chats[0] and "tool_choice" not in fake.chats[1] and tools_of(fake.chats[1]) == {"say"}
    assert c.no_tools == set() and c.research_due()   # tools still work, research too
    until_quiet(c, clock)
    c.tick()
    assert len(c.lines) == 2 and not any("tool_choice" in b for b in fake.chats[2:])   # never forced again
    assert capsys.readouterr().out.count("turned a forced say down") == 1


def test_an_endpoint_without_tools_gets_lines_from_the_data(fake, capsys):
    clock = Clock()
    c = new_caster(fake, clock)
    fake.reject_tools = True
    fake.start(frame(0), frame(30))
    beat(c, clock)
    assert [line["text"] for line in c.lines] == ["Line 1: Claude leads.", "Line 2: Claude leads.",
                                                  "Line 3: Claude leads."]
    assert c.no_tools == {caster.MODEL} and not c.research_due()
    assert sum("tools" in b for b in fake.chats) == 2   # asked once with a forced say, once without
    assert fake.chats[2]["messages"][0]["content"].endswith(caster.SAY_JSON) and "tools" not in fake.chats[2]
    printed = capsys.readouterr().out
    assert printed.count("the endpoint turned tools down") == 1 and "does not support tools" in printed


def test_the_analyst_researches_storylines_for_the_desk(fake):
    clock = Clock()
    c = new_caster(fake, clock)
    fake.start(*[frame(t) for t in range(0, 150, 30)])
    assert not c.research_due()   # not before the intro
    beat(c, clock)
    assert c.research_due()

    def jots(body):
        assert tools_of(body) == {*caster.LOOKUP_ARGS, "jot"} and "tool_choice" not in body
        assert body["model"] == c.models["color"] and body["messages"][0]["content"].startswith("You are Ada,")
        assert "jot down talking points" in body["messages"][0]["content"]
        assert "NOTEBOOK\n(empty)" in body["messages"][1]["content"]
        return calls(("jot", {"point": "Claude has led since the opening.", "player": CLAUDE}), ("jot", {"point": " "}),
                     ("jot", {"point": "The Orcs trail by 20.", "player": "nobody"}))

    fake.script = [calls(("standings", {})), jots]
    assert c.research_once() == 2 and not c.research_due()   # once the game has moved on, at most
    assert [(p["t"], p["slot"], p["point"]) for p in c.notebook] == [
        (120, 0, "Claude has led since the opening."), (120, None, "The Orcs trail by 20.")]
    answers = [m["content"] for m in fake.chats[-1]["messages"] if m["role"] == "tool"]
    assert answers[0].startswith("At 2:00:\n1. ")
    assert answers[1:] == ["jotted (1 in the notebook)", "error: jot needs a point", "jotted (2 in the notebook)"]

    asked = len(fake.prompts)
    beat(c, clock)
    assert ("NOTEBOOK (what Ada's research found; use a point when it fits)\n"
            f"- 2:00, {CLAUDE} (human): Claude has led since the opening.\n- 2:00: The Orcs trail by 20.") in (
        fake.prompts[asked])
    beat(c, clock)
    assert "NOTEBOOK (" in fake.prompts[-1]
    beat(c, clock)
    assert "NOTEBOOK (" not in fake.prompts[-1]   # a point is in POINT_BEATS beats, then dropped
    assert c.jot("g-0", 120, {"point": "From another game."}).startswith("error: that game is over")


def test_a_new_game_starts_the_desk_afresh(fake):
    clock = Clock()
    c = new_caster(fake, clock)
    fake.start(frame(0), frame(30))
    beat(c, clock)
    assert c.lookup("said", {"query": "claude"}).startswith("3 lines")
    fake.start(frame(0), game="g-2")
    until_quiet(c, clock)
    c.tick()
    assert c.match.game == "g-2" and c.lines[-1]["kind"] == "intro" and c.lines[-1]["t"] == 0
    assert "RECENT LINES (oldest first)\n(none: this is the opening)" in fake.prompts[-1]   # not the old game's
    assert c.lookup("said", {"query": "Line 2"}) == "The desk hasn't said that yet"


def test_failures_are_survived_and_the_key_is_never_printed(fake, capsys):
    clock = Clock()
    c = new_caster(fake, clock)
    fake.data_down = True
    c.tick()
    clock.now += caster.POLL_SECONDS
    c.tick()
    fake.data_down = False
    fake.start(frame(0), frame(30))
    fake.chat_failures = 1
    until_quiet(c, clock)
    c.tick()
    assert c.lines == []
    clock.now += 1
    c.tick()
    assert len(fake.auth) == 1   # it waits before retrying
    clock.now += caster.RETRY_SECONDS
    fake.speech_failures = 3
    for _ in range(3):
        c.tick()
        until_quiet(c, clock)
    assert [line["audio"] for line in c.lines] == [None, None, None]   # captions without a voice
    assert c.lines[0]["seconds"] == pytest.approx(4 / caster.WORDS_PER_SECOND)

    for junk in ("", '{"mood": "great"}', "Max: Not mine.", '{"lines": [{"speaker": "Ada", "text": "Old shape."}]}'):
        fake.content = junk   # nothing at all, no line, the other's line, the old every-caster shape
        until_quiet(c, clock)
        c.tick()
        clock.now += caster.RETRY_SECONDS
    assert len(c.lines) == 3
    assert c.beat.topic == "economy" and c.topics_used["lead"]   # a beat that keeps failing gives way to the next
    fake.content = None
    fake.script = [say("   "), say("*nods*")]   # a say with nothing to say: the caster hears why and tries again
    until_quiet(c, clock)
    c.tick()
    assert len(c.lines) == 4 and c.lines[3]["audio"] == "audio/4.wav"
    assert [m["content"].split("\n")[0] for m in fake.chats[-1]["messages"] if m["role"] == "tool"] == [
        "error: an empty line; call say with your line as its text"] * 2
    fake.data_down = True   # the env is gone: nothing to talk about
    until_quiet(c, clock)
    c.tick()
    assert len(c.lines) == 4
    out = capsys.readouterr()
    printed = out.out + out.err
    assert printed.count("no match data") == 2   # once each time it goes away
    assert "HTTP 500 bad key <cast key>" in printed and "HTTP 429 slow down" in printed
    assert "the reply has no text (finish_reason stop)" in printed and "no line in the reply" in printed
    assert KEY not in printed


def test_a_key_with_a_control_character_is_refused_not_printed():
    env = {**os.environ, "CAST_BASE_URL": "http://127.0.0.1:9", "CAST_API_KEY": "sk-se\ncret-123"}
    out = subprocess.run([sys.executable, caster.__file__, "--data", "http://127.0.0.1:9/live"], env=env,
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 2 and "control character" in out.stderr and "cret-123" not in out.stdout + out.stderr


def test_the_task_names_the_casters_their_voices_and_models(fake):
    clock = Clock()
    config = {"model": "openai/gpt-6-sol", "play_by_play": {"name": "Rex"},
              "analyst": {"name": "Iris", "voice": "coral", "style": "A dry analyst.", "model": "anthropic/x"}}
    casters = caster.casters_of(config)
    assert casters == {"pbp": ("Rex", "ash", caster.CASTERS["pbp"][2]), "color": ("Iris", "coral", "A dry analyst.")}
    assert caster.casters_of({}) == caster.CASTERS
    assert caster.models_of(config) == {"pbp": "openai/gpt-6-sol", "color": "anthropic/x"}
    assert caster.models_of({}) == {"pbp": caster.MODEL, "color": caster.MODEL}
    c = caster.Caster(f"{fake.url}/live", fake.url, KEY, casters=casters, models=caster.models_of(config), clock=clock)
    fake.start(frame(0), frame(30))
    beat(c, clock)
    assert [line["name"] for line in c.lines] == ["Rex", "Iris", "Rex"]
    pbp, analyst = (b["messages"][0]["content"] for b in fake.chats[:2])
    assert pbp.startswith("You are Rex,") and analyst.startswith("You are Iris,") and "- Iris, colour analyst" in pbp
    assert 'Rex says "Iris", Iris says "Rex"' in pbp and "Max" not in pbp and "Ada" not in analyst
    assert [b["model"] for b in fake.chats] == ["openai/gpt-6-sol", "anthropic/x", "openai/gpt-6-sol"]
    assert [(s["voice"], s["instructions"]) for s in fake.speech[:2]] == [
        ("ash", caster.CASTERS["pbp"][2]), ("coral", "A dry analyst.")]


def test_a_line_is_cleaned_up_for_speech_and_addressed_to_the_other_caster(fake):
    long = " ".join(["word"] * 20) + ". " + " ".join(["more"] * 20) + "."
    assert caster.spoken(long) == " ".join(["word"] * 20) + "."
    assert caster.spoken(" ".join(["x"] * 40)) == " ".join(["x"] * 30) + "…"
    assert caster.spoken("Max: Ada: Max, look at that army.") == "Max, look at that army."
    assert caster.spoken("Max: *leans in* Claude [cheering] strikes!") == "Claude strikes!"
    blocked = codecs.decode("Fuvg, gung'f n OHYYFUVG zbir", "rot13")
    assert caster.spoken(f"*grins* {blocked}; what the f**k, sh*t, s*** **Claude** 5*3") == (
        "bleep, that's a bleep move; what the bleep, bleep, bleep Claude 5*3")
    assert caster.spoken("Scunthorpe, shiitake and a classic assassin") == "Scunthorpe, shiitake and a classic assassin"

    clock = Clock()
    c = new_caster(fake, clock)
    fake.start(frame(0), frame(30))
    c.poll()
    assert c.line(caster.COLOR, "Ada, at your service. Ada thinks Claude is ahead, Ada!", CLAUDE) == {
        "speaker": "color", "text": "Max, at your service. Ada thinks Claude is ahead, Max!", "focus": 0}
    assert c.line(caster.PBP, "I'm Max, and Max has the call: Orcs next, Max?", "1") == {
        "speaker": "pbp", "text": "I'm Max, and Max has the call: Orcs next, Ada?", "focus": 1}
    for nothing in ("none", "None", "null", "", None, ["Claude"], "Atlantis", True):
        assert c.match.slot_named(nothing) is None, nothing
    with pytest.raises(ValueError, match="an empty line"):
        c.line(caster.PBP, "*nods*", None)

    fake.script = [{"role": "assistant", "content": 'Sure! {"text": "Claude, a hundred points.", "focus": "orc"}'},
                   {"role": "assistant", "content": '"Max: Not mine."\nAda: The Orcs have 400 gold.'}]
    for _ in range(2):
        until_quiet(c, clock)
        c.tick()
    assert [(line["speaker"], line["text"], line["focus"]) for line in c.lines] == [
        ("pbp", "Claude, a hundred points.", 1), ("color", "The Orcs have 400 gold.", None)]


def tone(amplitude: int, samples: int = 8000) -> bytes:
    pcm = array.array("h", (round(amplitude * math.sin(i / 5)) for i in range(samples)))
    if sys.byteorder == "big":
        pcm.byteswap()
    return caster.fixed_wav(streamed_wav(0) + pcm.tobytes())


def levels(wav: bytes) -> tuple[float, int]:
    pcm = array.array("h", wav[44:])
    if sys.byteorder == "big":
        pcm.byteswap()
    return 20 * math.log10(math.sqrt(sum(x * x for x in pcm) / len(pcm)) / 32767), max(map(abs, pcm))


def test_the_two_voices_are_levelled_to_the_same_loudness():
    for amplitude in (800, 20000):
        assert levels(caster.leveled(tone(amplitude)))[0] == pytest.approx(caster.LEVEL_DBFS, abs=0.1)
    spiky = caster.leveled(tone(500)[:-2] + (32000).to_bytes(2, "little", signed=True))
    assert levels(spiky)[1] <= 32767   # a peak caps the gain rather than clipping
    assert caster.leveled(streamed_wav(0.5)) == streamed_wav(0.5)   # silence stays silence
    assert caster.wav_seconds(caster.fixed_wav(streamed_wav(1.5))) == 1.5
    with pytest.raises(ValueError):
        caster.fixed_wav(b"ID3 not a wav")
