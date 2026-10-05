"""Two AI casters for an RTS match's live stream, ported from agentenv-openciv-plugin's streamer/caster.py for any RTS
env that serves casting.json (agentenv_rts.live.casting), read every 2 s. A play-by-play caster and an analyst trade
lines, each an agent that looks the match up with tools first, while the analyst researches in the background; a
text-to-speech model voices them, all through an OpenAI-compatible endpoint (CAST_BASE_URL, CAST_API_KEY), and the
viewer's ?stream&cast=<this> plays them with captions:

    GET /cast.json?since=<id>  {"lines": [{"id", "speaker", "name", "text", "audio", "seconds", "t", "focus",
                                "kind"}], "speaking_until": <unix seconds>}
    GET /audio/<id>.wav, GET /health

A beat's next line is written to land when about LEAD_SECONDS of speech are left, and looks things up only while the
audio queued outlasts the lookup. Stdlib only, as it runs in the streamer image's system Python; the key never appears
in what it prints."""

from __future__ import annotations

import argparse
import array
import codecs
import http.client
import json
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PBP, COLOR = "pbp", "color"
SEATS = {PBP: "play_by_play", COLOR: "analyst"}   # the speakers as a task's broadcast names them
MODEL, TTS_MODEL = "anthropic/claude-haiku-4-5", "openai/gpt-4o-mini-tts"
CASTERS = {  # speaker: name, text-to-speech voice, how that voice sounds; the defaults a task's broadcast may change
    PBP: ("Max", "ash", "An esports play-by-play caster calling a live strategy match. High energy, quick and punchy, "
                        "smiling; build excitement on the big moments (fights, fallen heroes, lead changes) and land "
                        "the names and numbers crisply. Never shouting."),
    COLOR: ("Ada", "sage", "A calm, sharp esports analyst at the casters' desk. Measured and confident, with a dry, "
                           "amused wit; lean on the key number or word; steady and unhurried, warm but precise."),
}
POLL_SECONDS = 2
LEAD_SECONDS = 4        # speech left when the next line lands: writing and voicing it starts that much earlier
LEVEL_DBFS = -22        # every line's loudness (RMS): the voices come back from text-to-speech ~12 dB apart
GAP_SECONDS = 0.5       # between two lines
RETRY_SECONDS = 5       # after a failed LLM call
FRESH_SECONDS = 75      # a moment or note not called by then is old news
NEWS_SECONDS = 60       # nor is one from more game seconds ago, also when joining a game under way
LIKE_SECONDS = 10       # moments of one kind this close in game time are called together
MOMENTUM_SECONDS = 120  # the standings' change in score and army: over this many game seconds
RECENT_SECONDS = 120    # the DATA's recent moments
STORY_SECONDS = 300     # a game this long has a story to tell
MEMORY = 16             # lines the casters see in each call, so they don't repeat themselves (`said` has the rest)
AUDIO_KEPT = 200
LINES_KEPT = 2000       # the broadcast's lines kept for `said` and the page
MAX_WORDS = 30
MAX_TOKENS = 1500       # a call's reply: a line or a lookup, after whatever thinking the model does first
WORDS_PER_SECOND = 2.5  # a line's length when its audio failed
CHAT_SECONDS = 45       # a model call's timeout
LOOKUP_ROUNDS = 3       # a live line's rounds of lookups before it must speak
CALL_SECONDS = 2.0      # a model call's time and a line's voicing, until measured (a running average after that)
VOICE_SECONDS = 1.5
LANDING_SECONDS = 1.5   # a line lands this long before the audio runs out: the gap between lines and the page's poll
LOOKUPS_AT_ONCE = 4     # tool calls answered in one round
MAX_LINES = 4           # a beat's lines, counting the answers to questions it asks
LINE_FAILS = 3          # failed lines in a row that end a beat
OUTRO_FAILS = 6         # the outro's: the stream lingers a minute after the game ends for it
REFUSED = re.compile(r"(?=.*\b(?:tools?|tool_choice|function(?:s| call(?:ing)?)?)\b)(?=.*(?:not supported|unsupported|"
                     r"does ?n[o']t support|not (?:be )?enabled|not available|not allowed|isn't supported))",
                     re.IGNORECASE | re.DOTALL)   # a 400 that turns tools (or a forced call) down, not a bad generation
OUT_OF_TIME = "(You are live and out of time: call say with your line now.)"
RESEARCH_SECONDS = 45   # the analyst's research: at most this often, and only once the game has moved on
RESEARCH_ROUNDS = 6
NOTEBOOK = 8            # talking points kept
POINT_BEATS = 2         # a point is in this many beats' calls, then dropped
POINT_SECONDS = 180     # or once it is this many game seconds old
POINT_CHARS = 300
TOOL_CHARS = 1500       # a tool's answer, at most
KINDS = ("death", "tier", "expand", "fight", "hero", "fight_end", "level")   # major moments, the biggest first
BREAKING = KINDS[:4]    # news that cuts into a beat under way: a hero or building lost, a new tier or base, a fight
HERO_KINDS = ("hero", "level", "learn")
TREND_STATS = ("score", "army")
# Slurs and strong profanity, whole words: agentenv-openciv-plugin's moderation.BLOCKLIST, in ROT13 so the repository
# holds no plaintext slurs. A line that says one gets "bleep" instead.
BLOCKLIST = codecs.decode(
    "shpx shpxre shpxref shpxvat shpxrq shpxva zbgureshpxre zbgureshpxref zbgureshpxvat fuvg fuvgf fuvggl "
    "fuvgurnq ohyyfuvg phag phagf gjng gjngf ovgpu ovgpurf juber juberf fyhg fyhgf jnaxre jnaxref nffubyr "
    "nffubyrf pbpxfhpxre avttre avttref avttn avttnf snttbg snttbgf snt sntf genaal genaavrf "
    "ergneq ergneqf ergneqrq xvxr xvxrf fcvp fcvpf puvax puvaxf tbbx tbbxf jrgonpx jrgonpxf ornare "
    "ornaref pbba pbbaf cnxv cnxvf enturnq enturnqf gbjryurnq gbjryurnqf fnaqavttre", "rot13").split()
BLOCKED = re.compile(r"\b(?:" + "|".join(sorted(BLOCKLIST, key=len, reverse=True)) + r")\b", re.IGNORECASE)
MASKED = re.compile(r"(?<![*\w])[A-Za-z]+(?:\*{2,}[A-Za-z]*|\*[A-Za-z]+)")   # "s***", as a game masks it, or "f**k"
TOPICS = {  # what the analysis is about when nothing new happened, each in turn
    "lead": "the lead: who is ahead on score, by how much and since when, and who has the momentum",
    "economy": "the economy: gold and lumber banked, food against the cap. Who is floating resources, and why?",
    "armies": "the armies: army value and unit counts side by side, and who would want a fight right now",
    "heroes": "the heroes: who has which, their levels and what they have learned, and any hero that has fallen",
    "clock": "the clock: how far into the time limit the game is, and who wins on score if time runs out now",
    "plans": "the players' own plans: does what they said match what they are doing on the board?",
    "agents": "the AI players themselves: decisions, tokens and cost so far, against how they are doing",
    "story": "the story of the game so far: the swings, the turning points, and how the score got to where it is now",
}

DESK = """\
the live broadcast of a real-time strategy match between AIs: a player the DATA lists as "agent" is an AI model \
playing through the env, going by its label exactly as the DATA writes it; any other, such as "the game's AI", is run \
by the game itself. It is AI against AI, or an AI model against the game's own AI, and the desk is two AI casters, \
who talk to each other, not at the camera:
- Max, play-by-play: the energy. Calls what just happened, fast and vivid, and sells the big moments. Short lines, 6 \
to 16 words.
- Ada, colour analyst: calm, sharp, dry wit. Says why it matters with one concrete number or comparison, reads the \
players' plans against the board, and asks the question the viewers are thinking. Up to 22 words.
They have chemistry: they hand off to each other, react to what the other just said, and now and then disagree. A \
line may address the other caster by name, never its own speaker: Max says "Ada", Ada says "Max". They know the \
match only from the DATA and their tools."""

STYLE = """\
The style, with placeholders in angle brackets (never facts):
Max: "There it is! <label>'s <hero> goes down, and that base is wide open!"
Ada: "Nine hundred in army value to four hundred, Max. If I'm <other label>, I'm rebuilding right now."

Rules:
- Facts come only from the DATA and what your tools return. Never invent moments, fights, units, heroes, rules, \
numbers, causes or motives, nor a figure of speech that implies one (no ambush or comeback the match doesn't show). \
Plans are what a player says, not what happened: a plan to expand is not an expansion until the moments show it. \
Opinions, questions and predictions are welcome when they are clearly opinions.
- One or two concrete numbers a line: scores, army value, gold, lumber, food, units, the clock.
- Name players by label as the DATA writes it, with the faction now and then; after that the label alone is fine.
- Written to be spoken: one or two short sentences a line. No emoji, markdown, stage directions, hashtags or lists. \
Say "six minutes in", never "6:00".
- Never repeat a fact, joke or opening from RECENT LINES or what the desk has said before: find a fresh angle or \
another player. No stock phrases: not "that's not X, that's Y", not "Ada here", and don't start lines with "And", \
"Well" or "Wow".
- Don't explain the rules of the game, or how scores and wins are decided: the viewers know strategy games.
- This is a public stream: no profanity or slurs, and a word the DATA masks (like "s***") stays unsaid. A rude \
note is described in your own words, never quoted."""

LIVE = """\
You write only your own line, then the other caster answers it: a conversation a line at a time. React to what was \
actually just said: build on it, answer the question, or push back, and hand the moment on.

Before you speak you may look the match up with the tools: the standings, a player in depth, score or army over the \
game, the feed's moments, the players' plans, and what the desk has already said. You are live, so look up only what \
makes this line sharper (the number that matters, a comparison, a callback), one or two lookups at most, then speak \
by calling say. Never read a tool's answer out: pick the one fact that matters. The NOTEBOOK has what Ada found \
researching the game in the background: use a point when it fits the moment."""

SAY_JSON = """\
Reply with JSON only: {"text": "<your line>", "focus": "<player label or null>"}. "focus" is the player the line is \
mostly about, else null. Inside the text, quote with single quotes, never double quotes."""

RESEARCH = """\
The broadcast is running, and you are preparing for what comes next: dig through the match with the tools and jot \
down talking points the desk can use in the next few minutes. A good point is a fact with its numbers that the \
standings alone don't show: a swing, a comparison over time, a plan against what the player actually did, a fight's \
toll, an earlier moment, the player nobody has talked about. Each point is one or two sentences of facts, not a line \
to read out. Don't jot what the NOTEBOOK or the RECENT LINES already have. Look up what you need, jot one to three \
points with jot, then stop: reply with no tool call.

Rules: facts come only from the DATA and what your tools return; never invent moments, numbers, causes or motives. \
Plans are what a player says, not what happened."""


def tool(name: str, description: str, properties: dict, required: tuple = ()) -> dict:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties, "required": list(required)}}}


PLAYER = {"type": "string", "description": "a player's label, or its slot number"}
SINCE = {"type": "number", "description": "game seconds; leave out for the whole game"}
LOOKUPS = [
    tool("standings", "The table now, best score first: score and army value with their change over two minutes, "
                      "resources, food, units, structures, each AI's decisions, tokens and cost, and plans.", {}),
    tool("player", "One player in depth: rank and stats now, score and army 1, 3 and 5 minutes ago, its moments by "
                   "kind and the latest, its plans, and its AI's costs.", {"player": PLAYER}, ("player",)),
    tool("trend", "Score or army value over the game (a point every 30 s), at most 12 points a player.",
         {"stat": {"type": "string", "enum": list(TREND_STATS)}, "since_t": SINCE}, ("stat",)),
    tool("moments", "The feed's moments, newest last, by kind (such as fight, fight_end, death, hero, level, tier, "
                    "expand, build, train), player and time.", {"kind": {"type": "string"}, "side": PLAYER,
                                                                "since_t": SINCE}),
    tool("notes", "What the players told the spectators, their plans, newest last.", {"player": PLAYER}),
    tool("said", "What the desk has already said in this broadcast that has these words, newest last: for callbacks, "
                 "and to keep from repeating.", {"query": {"type": "string"}}),
]
LOOKUP_ARGS = {t["function"]["name"]: set(t["function"]["parameters"]["properties"]) for t in LOOKUPS}
REQUIRED = {t["function"]["name"]: t["function"]["parameters"]["required"] for t in LOOKUPS}
SAY = tool("say", "Speak your line, live: one or two short sentences, written to be spoken. This ends your turn.",
           {"text": {"type": "string"}, "focus": {"type": "string", "description": "the player the line is mostly "
                                                                                  "about, by label; empty for none"}},
           ("text",))
JOT = tool("jot", "Jot a talking point in the desk's notebook: one or two sentences of facts with their numbers.",
           {"point": {"type": "string"}, "player": PLAYER}, ("point",))


class Unknown(Exception):
    """A lookup of something the match doesn't have: the model reads why."""


@dataclass
class Moment:
    """Something the casters may call once: a moment of the feed, or a player's note."""
    key: tuple
    kind: str             # the feed's kind, or "note"
    t: float              # game seconds
    side: int | None      # whose it is
    text: str             # as the DATA puts it
    seen: float           # when the caster first saw it
    major: bool = False


@dataclass
class Beat:
    """A subject the desk talks about, a line at a time: the speakers alternate, `first` first."""
    kind: str             # intro, news, plans, color or outro
    task: str             # what the casters do now
    first: str            # who speaks first
    count: int            # how many lines (a question to the other caster earns an answer, up to MAX_LINES)
    moments: list[Moment] = field(default_factory=list)
    topic: str | None = None
    lines: list[dict] = field(default_factory=list)   # written so far
    cut_in: bool = False  # it breaks off a beat under way
    begun: bool = False
    fails: int = 0
    points: list[dict] = field(default_factory=list)   # the notebook as it was when the beat began

    @property
    def speaker(self) -> str:
        """Who speaks the next line."""
        return self.first if len(self.lines) % 2 == 0 else (PBP if self.first == COLOR else COLOR)

    @property
    def done(self) -> bool:
        return len(self.lines) >= self.count


class Match:
    """The match as casting.json has it: the players and the clock now, each player's score and army over the game,
    and every moment and note so far. The result is the env's, from the first player's side as RTS envs report it
    (victory, defeat, draw), or time_limit when time ran out undecided."""

    def __init__(self, game: str | None = None):
        self.game = game
        self.title = self.t = self.limit = None
        self.desk = self.result = ""
        self.over = False
        self.players: list[dict] = []
        self.history: list[dict] = []
        self.moments: list[dict] = []
        self.notes: list[dict] = []

    def add(self, doc: dict) -> None:
        """Folds in a casting.json reply, whose moments and notes are those after the last reply's clock."""
        self.t, self.limit = doc["clock"]["t"], doc["clock"]["limit"]
        self.title, self.desk = doc.get("title"), doc.get("desk") or ""
        self.over, self.result = bool(doc.get("over")), doc.get("result") or ""
        self.players, self.history = doc.get("players") or [], doc.get("history") or []
        self.moments += doc.get("moments") or []
        self.notes += doc.get("notes") or []

    @property
    def started(self) -> bool:
        return bool(self.players) and self.t is not None

    def player(self, slot) -> dict | None:
        return next((p for p in self.players if str(p["slot"]) == str(slot)), None)

    def who(self, slot) -> str:
        p = self.player(slot)
        if p is None:
            return "?"
        return f"{p['label']} ({p['faction']})" if p.get("faction") else p["label"]

    def named(self, name) -> dict | None:
        """A player by label, slot or faction (a faction only one player has); None for anything else ("none" too)."""
        name = str(name).strip().lower() if isinstance(name, str | int) else ""
        if name in ("", "none", "null"):
            return None
        for key in ("label", "slot", "faction"):
            found = [p for p in self.players if str(p.get(key)).lower() == name]
            if len(found) == 1:
                return found[0]
        return None

    def slot_named(self, name) -> int | None:
        p = self.named(name)
        return p["slot"] if p else None

    def ranked(self) -> list[dict]:
        return sorted(self.players, key=lambda p: -(p["stats"].get("score") or 0))

    def history_at(self, t: float) -> dict:
        """The newest history entry at or before `t`; {} before the first."""
        return next((h for h in reversed(self.history) if h["t"] <= t), {})

    @staticmethod
    def scores(entry: dict) -> dict[str, float]:
        return {s: v["score"] for s, v in entry["players"].items() if v.get("score") is not None}

    def leader(self, entry: dict) -> str | None:
        """The slot with the top score in a history entry; None while it is tied."""
        top = max((scores := self.scores(entry)).values(), default=None)
        leaders = [s for s, v in scores.items() if v == top]
        return leaders[0] if len(leaders) == 1 else None

    def leader_since(self) -> tuple[str, float] | None:
        if not self.history or (leader := self.leader(self.history[-1])) is None:
            return None
        since = self.history[-1]["t"]
        for h in reversed(self.history):
            if self.leader(h) != leader:
                break
            since = h["t"]
        return leader, since

    def involved(self, moment: dict, p: dict) -> bool:
        """Whether a moment is the player's: its side, or its text names it (a fight names both sides)."""
        return moment.get("side") == p["slot"] or p["label"] in str(moment.get("text") or "")


class Caster:
    """Writes and voices the casters' lines a line at a time, and keeps them for the stream page."""

    def __init__(self, data_url: str, base_url: str, api_key: str, *, tts_model: str = TTS_MODEL,
                 casters: dict[str, tuple[str, str, str]] = CASTERS, models: dict[str, str] | None = None,
                 title: str | None = None, clock: Callable[[], float] = time.time):
        self.data_url = data_url.split("?")[0].rstrip("/")
        self.base_url = base_url.rstrip("/").removesuffix("/v1")
        self.api_key = api_key
        self.tts_model, self.title = tts_model, title
        self.models = {s: (models or {}).get(s) or MODEL for s in CASTERS}
        self.casters = casters
        self.clock = clock
        self.lock = threading.Lock()     # the lines, the audio and the notebook: the page and the research read them
        self.data = threading.RLock()    # the match: the poll writes it, the lookups read it
        self.lines: list[dict] = []
        self.line_count = 0
        self.audio: dict[int, bytes] = {}
        self.speaking_until = 0.0
        self.polled_at = float("-inf")
        self.retry_at = 0.0
        self.writing_seconds: dict[str, float] = {}   # how long each caster's last line took to write and voice
        self.data_down = False
        self.no_tools: set[str] = set()   # models the endpoint turned tools down for: they write from the DATA
        self.no_force: set[str] = set()   # models that take tools but not a forced say
        self.call_seconds: dict[str, float] = {}   # each model's live calls, a running average
        self.voice_seconds = VOICE_SECONDS
        self.researched_at = float("-inf")
        self.researched_t: float | None = None
        self.start(None)

    def start(self, game: str | None) -> None:
        """A new game: the casters start over, with an intro."""
        self.match = Match(game)
        self.known: set[tuple] = set()
        self.pending: list[Moment] = []
        self.introduced = self.finished = False
        self.topics_used: dict[str, int] = {}
        self.beats = 0
        self.last_kind: str | None = None
        self.beat: Beat | None = None
        with self.lock:
            self.notebook: list[dict] = []
            self.game_line = self.line_count   # RECENT LINES and `said` start after it

    @property
    def names(self) -> tuple[str, str]:
        """The play-by-play caster's name and the analyst's."""
        return self.casters[PBP][0], self.casters[COLOR][0]

    def log(self, message: str) -> None:
        if self.api_key:
            for form in (self.api_key, repr(self.api_key)[1:-1]):
                message = message.replace(form, "<cast key>")
        print(message, flush=True)

    # ---- the loop ----

    def run(self) -> None:
        threading.Thread(target=self.research_loop, daemon=True).start()
        while True:
            try:
                self.tick()
            except Exception as e:  # the show goes on: no glitch in the data or in a reply ends the casting
                self.log(f"caster: {e!r}")
                self.retry_at = self.clock() + RETRY_SECONDS
            time.sleep(0.25)

    def tick(self) -> None:
        now = self.clock()
        if now - self.polled_at >= POLL_SECONDS:
            self.polled_at = now
            self.poll()
        upcoming = self.beat.speaker if self.beat is not None and not self.beat.done else None   # else not chosen yet
        writing = self.writing_seconds.get(upcoming, max(self.writing_seconds.values(), default=0.0))
        due = self.speaking_until - now <= LEAD_SECONDS + writing
        if self.finished or self.data_down or now < self.retry_at or not (due or self.match.over):
            return   # the outro waits for no one; with the match data gone, there is nothing to talk about
        beat = self.beat
        if beat is None or beat.done or self.breaking(beat) or (self.match.over and beat.kind != "outro"):
            under_way = beat is not None and not beat.done
            beat = self.beat = self.next_beat()
            if beat is None:
                return
            beat.cut_in = under_way and beat.kind == "news"
        self.speak(beat)

    def lines_since(self, since: int) -> dict:
        with self.lock:
            return {"lines": [line for line in self.lines if line["id"] > since],
                    "speaking_until": round(self.speaking_until, 2)}

    # ---- reading the match ----

    def poll(self) -> None:
        since = self.match.t if self.match.t is not None else -1
        try:
            with urllib.request.urlopen(f"{self.data_url}/casting.json?since={since}", timeout=10) as r:
                doc = json.load(r)
        except (OSError, ValueError, http.client.HTTPException) as e:
            if not self.data_down:
                self.log(f"caster: no match data from {self.data_url} ({e}); retrying")
            self.data_down = True
            return
        self.data_down = False
        with self.data:
            if doc.get("game") != self.match.game:
                cut = self.match.t is not None   # that reply has only what came after the old game's clock
                self.start(doc.get("game"))
                if cut:
                    return self.poll()
            joining = self.match.t is None
            self.match.add(doc)
            m, now = self.match, self.clock()
            for x in doc.get("moments") or ():
                if not joining or x["t"] >= m.t - NEWS_SECONDS:
                    self.remember(Moment(("moment", x["id"]), x["kind"] or "", x["t"], x["side"],
                                         f"{game_time(x['t'])}: {x['text']}", now, bool(x["major"])))
            for n in doc.get("notes") or ():
                if not joining or n["t"] >= m.t - NEWS_SECONDS:
                    self.remember(Moment(("note", n["t"], n["slot"], n["text"]), "note", n["t"], n["slot"],
                                         f'{m.who(n["slot"])}, at {game_time(n["t"])}: "{n["text"]}"', now))

    def remember(self, moment: Moment) -> None:
        if moment.key not in self.known:
            self.known.add(moment.key)
            self.pending.append(moment)

    # ---- choosing the beat ----

    def news(self) -> list[Moment]:
        """The fresh major moments, the biggest first."""
        m, now = self.match, self.clock()
        self.pending = [x for x in self.pending if now - x.seen <= FRESH_SECONDS and x.t >= m.t - NEWS_SECONDS]
        return sorted((x for x in self.pending if x.major), key=lambda x: (rank(x.kind), -x.t))

    def breaking(self, beat: Beat) -> bool:
        """Whether news has come in that is bigger than the beat under way, which then gives way to it."""
        if not beat.lines or beat.done or beat.kind in ("intro", "outro"):
            return False
        top = min((rank(x.kind) for x in beat.moments if x.major), default=len(KINDS))
        return any(x.kind in BREAKING and rank(x.kind) < top for x in self.news())

    def next_beat(self) -> Beat | None:
        m = self.match
        if not m.started or self.finished:
            return None
        if m.over:
            return self.outro()
        if not self.introduced:
            return self.intro()
        news = self.news()
        moments = news[:3] + [x for x in news[3:] if any(x.kind == e.kind and abs(x.t - e.t) <= LIKE_SECONDS
                                                         for e in news[:3])][:3]
        pbp, color = self.names
        if moments:
            return Beat("news", "Call these moments of the game, the biggest first:\n"
                        + "\n".join(f"- {x.text}" for x in moments)
                        + f"\n{pbp} calls it with energy; {color} answers with what it means, one number from the "
                          "match. Between your lines, call every moment above.",
                        PBP, 2 if len(moments) == 1 else 3, moments)
        notes = self.notes_to_read()
        if notes and self.last_kind != "plans":
            return Beat("plans", "The players' latest plans, as they told the spectators:\n"
                        + "\n".join(f"- {x.text}" for x in notes)
                        + f"\n{pbp} relays one, paraphrased; {color} checks it against the board: do the numbers "
                          "back it up?", PBP, 2, notes)
        return self.color()

    def notes_to_read(self) -> list[Moment]:
        """The two newest notes, from different players."""
        out: list[Moment] = []
        for x in reversed(self.pending):
            if x.kind == "note" and x.side not in {o.side for o in out}:
                out.append(x)
        return out[:2]

    def intro(self) -> Beat:
        m = self.match
        title = f' to "{self.title}"' if self.title else ""
        roster = "; ".join(f"{m.who(p['slot'])}, {p.get('controller') or 'a player'}" for p in m.players)
        limit = f", with a {m.limit / 60:g}-minute time limit" if m.limit else ""
        joined = f" We join {game_time(m.t)} in, with the game under way." if m.t >= NEWS_SECONDS else ""
        pbp, color = self.names
        return Beat("intro", f"Open the broadcast. {pbp} welcomes everyone{title}; between them, {pbp} and {color} "
                             f"name every player and who plays it: {roster}. {color} sets the stakes from the game's "
                             f"brief{limit}. The last line throws to the action.{joined} Make it big: this is the "
                             "opening.", PBP, 3)

    def outro(self) -> Beat:
        pbp, color = self.names
        return Beat("outro", f"The game is over: {self.result()}. {pbp} calls the result with the final score; {color} "
                             f"says why, with one number, and names one standout moment of the game; {pbp} thanks the "
                             "viewers and signs off for both of you. These are the last lines of the broadcast.",
                    PBP, 3)

    def result(self) -> str:
        """The end as the env reports it, from the first player's side, with the final scores."""
        m = self.match
        first, rest = m.who(m.players[0]["slot"]), [m.who(p["slot"]) for p in m.players[1:]]
        end = {"victory": f"{first} wins", "draw": "a draw", "time_limit": "time ran out at the limit, undecided",
               "defeat": f"{first} is defeated" + (f": {rest[0]} wins" if len(rest) == 1 else "")}.get(
            m.result, m.result.replace("_", " "))
        scores = ", ".join(f"{m.who(p['slot'])} {number(p['stats'].get('score'))}" for p in m.ranked())
        return f"{end}, at {game_time(m.t)}; the final scores: {scores}"

    def color(self) -> Beat:
        m = self.match
        fits = {"lead": len(m.players) >= 2, "heroes": any(x.get("kind") in HERO_KINDS for x in m.moments),
                "clock": bool(m.limit), "plans": bool(m.notes), "agents": any(p.get("agent") for p in m.players),
                "story": m.t >= STORY_SECONDS}
        topic = min((t for t in TOPICS if fits.get(t, True)), key=lambda t: self.topics_used.get(t, -1))
        pbp, color = self.names
        return Beat("color", f"Nothing new to call this moment, so the desk fills with analysis. The angle: "
                             f"{TOPICS[topic]}; or a storyline from the NOTEBOOK, if one is fresher. {color} opens "
                             f"with a sharp observation and a number; {pbp} reacts.",
                    COLOR, 2, topic=topic)

    # ---- writing and voicing a line ----

    def begin(self, beat: Beat) -> None:
        """A beat's first line is being written: it reads the notebook as it is now."""
        beat.begun = True
        with self.lock:
            t = self.match.t
            self.notebook = [p for p in self.notebook if p["beats"] < POINT_BEATS
                             and t - POINT_SECONDS <= p["t"] <= t]
            beat.points = list(self.notebook)

    def called(self, beat: Beat) -> None:
        """A beat's first line is out: what it calls is called, whether or not it is cut short. A beat that fails
        before it says anything calls nothing, so its news, its topic and the intro are there for the next one."""
        self.beats += 1
        self.last_kind = beat.kind
        self.introduced = True
        called = {x.key for x in beat.moments}
        self.pending = [x for x in self.pending if x.key not in called]
        if beat.topic:
            self.topics_used[beat.topic] = self.beats
        with self.lock:
            for p in beat.points:
                p["beats"] += 1

    def speak(self, beat: Beat) -> None:
        """Writes the beat's next line, voices it and publishes it."""
        began, speaker = self.clock(), beat.speaker
        if not beat.begun:
            self.begin(beat)
        line = self.write(beat)
        if line is None:
            self.retry_at = self.clock() + RETRY_SECONDS
            beat.fails += 1
            if beat.fails >= (OUTRO_FAILS if beat.kind == "outro" else LINE_FAILS):
                beat.count = len(beat.lines)   # it ends where it got to, and the desk moves on
                if not beat.lines and beat.topic:   # analysis that keeps failing gives way to another; news waits
                    self.topics_used[beat.topic] = self.beats
                self.finished = beat.kind == "outro"   # the broadcast's last lines are never started again
            return
        beat.fails = 0
        if not beat.lines:
            self.called(beat)
        wav = self.voice(line)
        beat.lines.append(line)
        if (len(beat.lines) == beat.count < MAX_LINES and beat.kind not in ("intro", "outro")
                and line["text"].rstrip().endswith("?")):
            beat.count += 1   # a question to the other caster: it answers
        self.publish(beat, line, wav)
        self.finished = beat.kind == "outro" and beat.done
        self.writing_seconds[speaker] = self.clock() - began

    def prompt(self, beat: Beat) -> str:
        """What the speaker of the beat's next line reads: the DATA, the notebook, the show so far and its task."""
        speaker = beat.speaker
        me = self.casters[speaker][0]
        other = self.casters[COLOR if speaker == PBP else PBP][0]
        recent = self.recent()
        with self.data:
            data = self.summary()
            notebook = "\n".join(f"- {game_time(p['t'])}"
                                 f"{', ' + self.match.who(p['slot']) if p['slot'] is not None else ''}: {p['point']}"
                                 for p in beat.points)
        n, i = beat.count, len(beat.lines)
        if i == 0:
            where = ("You cut in: the desk was on something else, and this news can't wait." if beat.cut_in
                     else "You open this exchange.")
        else:
            where = f'{beat.lines[-1]["name"]} just said: "{beat.lines[-1]["text"]}"\n' + (
                f"Yours is the last line of this exchange: answer {other}, and land it." if i == n - 1 else
                f"Answer {other}, and keep it moving.")
        return (f"DATA\n{data}\n\n"
                + (f"NOTEBOOK (what {self.casters[COLOR][0]}'s research found; use a point when it fits)\n{notebook}"
                   "\n\n" if notebook else "")
                + f"RECENT LINES (oldest first)\n{recent or '(none: this is the opening)'}"
                f"\n\nNOW\n{beat.task}\n\nYOUR LINE: you are {me}, line {i + 1} of {n}, "
                f"{'6 to 16' if speaker == PBP else 'at most 22'} words. {where}")

    def game_lines(self) -> list[dict]:
        """This game's lines so far."""
        with self.lock:
            return [line for line in self.lines if line["id"] > self.game_line]

    def recent(self) -> str:
        return "\n".join(f"{line['name']}: {line['text']}" for line in self.game_lines()[-MEMORY:])

    def system(self, speaker: str, then: str) -> str:
        """Who the speaker is, the desk, the game as the env describes it, and then `then`."""
        role = "play-by-play" if speaker == PBP else "colour analyst"
        game = f"\n\nTHE GAME, as the env describes it: {self.match.desk}" if self.match.desk else ""
        return (renamed(f"You are {CASTERS[speaker][0]}, the {role} caster on {DESK}", self.casters) + game
                + renamed(f"\n\n{then}", self.casters))

    def time_to_look(self, model: str) -> bool:
        """Whether the audio queued outlasts one more of the model's lookups, the line after it, its voicing and its
        landing before the audio runs out."""
        call = self.call_seconds.get(model, CALL_SECONDS)
        return self.clock() + 2 * call + self.voice_seconds + LANDING_SECONDS <= self.speaking_until

    def write(self, beat: Beat) -> dict | None:
        """The beat's next line, from its speaker's agent: a round of lookups at a time while the audio queued
        outlasts it (at most LOOKUP_ROUNDS), then its line. None if the calls failed."""
        speaker = beat.speaker
        model = self.models[speaker]
        tools = model not in self.no_tools
        messages = [{"role": "system", "content": self.system(speaker, f"{STYLE}\n\n{LIVE if tools else SAY_JSON}")},
                    {"role": "user", "content": self.prompt(beat)}]
        if tools and not self.time_to_look(model):
            messages[1]["content"] += f"\n{OUT_OF_TIME}"   # forced or not, it knows
        round_ = 0
        try:
            while round_ <= LOOKUP_ROUNDS:
                body = {"model": model, "max_tokens": MAX_TOKENS, "messages": messages}
                last = round_ == LOOKUP_ROUNDS or not self.time_to_look(model)
                if tools:   # the last round has only say, for a model that can't be forced to it
                    body["tools"] = [SAY] if last else [*LOOKUPS, SAY]
                    if last and model not in self.no_force:
                        body["tool_choice"] = {"type": "function", "function": {"name": "say"}}
                try:
                    message = self.chat(body)
                except urllib.error.HTTPError as e:
                    why = failure(e)
                    if not (tools and e.code == 400 and REFUSED.search(why)):
                        raise ValueError(why) from None
                    if "tool_choice" in body:   # tools, but not a forced say: it is asked to speak instead
                        self.no_force.add(model)
                        self.log(f"caster: {model} turned a forced say down ({why}); its casters are asked instead")
                        continue
                    if round_:
                        raise ValueError(why) from None
                    self.no_tools.add(model)
                    self.log(f"caster: the endpoint turned tools down for {model} ({why}); its caster writes from "
                             "the DATA alone")
                    return self.write(beat)
                calls = message.get("tool_calls") or []
                said = next((c for c in calls if (c.get("function") or {}).get("name") == "say"), None)
                if said is not None:
                    args = arguments(said)
                    try:
                        return self.line(speaker, args.get("text"), args.get("focus"))
                    except ValueError as e:   # nothing to say: it says why, and the caster tries again
                        if round_ == LOOKUP_ROUNDS:
                            raise
                        calls, answers = [said], [{"role": "tool", "tool_call_id": said.get("id") or "call_say",
                                                   "content": f"error: {e}; call say with your line as its text"}]
                else:
                    if not calls:
                        return self.line_of(speaker, message.get("content"), message)
                    answers = self.answers(calls)
                round_ += 1
                if tools and (round_ == LOOKUP_ROUNDS or not self.time_to_look(model)):
                    answers[-1]["content"] += f"\n{OUT_OF_TIME}"
                messages += [assistant(message, calls), *answers]
            raise ValueError(f"no line after {LOOKUP_ROUNDS + 1} calls")
        except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError, http.client.HTTPException) as e:
            self.log(f"caster: the {beat.kind} beat's line failed, retrying in {RETRY_SECONDS} s: {failure(e)}")
        return None

    def chat(self, body: dict) -> dict:
        began = self.clock()
        reply = json.loads(self.post("/v1/chat/completions", body, CHAT_SECONDS))
        if "jot" not in {t["function"]["name"] for t in body.get("tools") or ()}:   # a live call
            seconds = self.call_seconds.get(body["model"], CALL_SECONDS)
            self.call_seconds[body["model"]] = seconds + 0.3 * (self.clock() - began - seconds)
        message = reply["choices"][0]["message"]
        if not isinstance(message, dict):
            raise ValueError("the reply has no message")
        message["finish_reason"] = reply["choices"][0].get("finish_reason")
        return message

    def answers(self, calls: list, jot: Callable[[dict], str] | None = None) -> list[dict]:
        """The tool messages answering a round of calls, one for each, in order: lookups (at most LOOKUPS_AT_ONCE),
        and with `jot`, the research's jots."""
        out, looked = [], 0
        for n, call in enumerate(calls):
            fn = call.get("function") or {}
            if jot is not None and fn.get("name") == "jot":
                answer = jot(arguments(call))
            else:
                looked += 1
                answer = (self.lookup(fn.get("name"), fn.get("arguments")) if looked <= LOOKUPS_AT_ONCE
                          else f"error: at most {LOOKUPS_AT_ONCE} lookups at a time")
            out.append({"role": "tool", "tool_call_id": call.get("id") or f"call_{n}", "content": answer})
        return out

    def line_of(self, speaker: str, content, message: dict) -> dict:
        """A line from a reply without a say call: its JSON ({"text", "focus"}), or its text."""
        content = str(content or "").strip()
        if not content:
            raise ValueError(f"the reply has no text (finish_reason {message.get('finish_reason')})")
        start = content.find("{")
        if start >= 0:
            try:
                reply, _ = json.JSONDecoder().raw_decode(content, start)
            except ValueError:
                reply = None
            if isinstance(reply, dict) and "text" in reply:
                return self.line(speaker, reply.get("text"), reply.get("focus"))
            if reply is not None:
                raise ValueError(f"no line in the reply: {content[:120]!r}")
        other = re.escape(self.casters[COLOR if speaker == PBP else PBP][0])
        said = [x for x in content.splitlines() if x.strip() and not re.match(rf"^\W*{other}\W*:", x)]
        if not said:
            raise ValueError(f"no line in the reply: {content[:120]!r}")
        return self.line(speaker, said[0].strip().strip('"'), None)

    def line(self, speaker: str, text, focus) -> dict:
        """A line as it is said, cleaned up for speech; ValueError when nothing is left. An address to its own speaker
        was meant for the other ("A fine move, Ada" from Ada is to Max); "I'm Ada" is not addressed to anyone."""
        text = spoken(text, tuple(name for name, _, _ in self.casters.values()))
        if not text:
            raise ValueError("an empty line")
        own, other = (self.casters[s][0] for s in (speaker, PBP if speaker == COLOR else COLOR))
        text = re.sub(rf"(?:(?<=, )|^){re.escape(own)}(?=\s*(?:[,.!?]|$))", other, text)
        with self.data:
            return {"speaker": speaker, "text": text, "focus": self.match.slot_named(focus)}

    def voice(self, line: dict) -> bytes | None:
        """The line spoken, as a WAV file; None if the speech call failed."""
        _, voice, style = self.casters[line["speaker"]]
        began = self.clock()
        try:
            wav = leveled(fixed_wav(self.post("/v1/audio/speech", {
                "model": self.tts_model, "voice": voice, "input": line["text"], "instructions": style,
                "response_format": "wav"}, 45)))
        except (OSError, ValueError, http.client.HTTPException) as e:
            self.log(f"caster: no voice for a line, captions only: {failure(e)}")
            return None
        self.voice_seconds += 0.3 * (self.clock() - began - self.voice_seconds)
        return wav

    def post(self, path: str, body: dict, timeout: float) -> bytes:
        request = urllib.request.Request(f"{self.base_url}{path}", data=json.dumps(body).encode(), headers={
            "Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as r:
            return r.read()

    def publish(self, beat: Beat, line: dict, wav: bytes | None) -> None:
        with self.lock:
            seconds = wav_seconds(wav) if wav else len(line["text"].split()) / WORDS_PER_SECOND
            self.speaking_until = max(self.clock(), self.speaking_until) + seconds + GAP_SECONDS
            self.line_count += 1
            ident = self.line_count
            if wav:
                self.audio[ident] = wav
            for old in [k for k in self.audio if k <= ident - AUDIO_KEPT]:
                del self.audio[old]
            line.update(id=ident, name=self.casters[line["speaker"]][0], audio=f"audio/{ident}.wav" if wav else None,
                        seconds=round(seconds, 2), t=self.match.t, kind=beat.kind)
            self.lines.append({k: line[k] for k in ("id", "speaker", "name", "text", "audio", "seconds", "t",
                                                    "focus", "kind")})
            del self.lines[:-LINES_KEPT]
        self.log(f"[{beat.kind} {game_time(self.match.t)}] {line['name']}: {line['text']}")

    # ---- the analyst's research, in the background ----

    def research_loop(self) -> None:
        while True:
            time.sleep(1)
            try:
                if self.research_due():
                    self.research_once()
            except Exception as e:  # research is a bonus: the show never waits on it
                self.log(f"caster: research: {e!r}")
                self.researched_at = self.clock()

    def research_due(self) -> bool:
        m = self.match
        return (self.models[COLOR] not in self.no_tools and m.started and not m.over and self.introduced
                and not self.data_down and self.clock() - self.researched_at >= RESEARCH_SECONDS
                and m.t != self.researched_t)

    def research_once(self) -> int:
        """One session of the analyst digging through the match; returns the points it jotted."""
        self.researched_at = self.clock()
        with self.data:
            game, t, data = self.match.game, self.match.t, self.summary()
        self.researched_t = t
        recent = self.recent()
        with self.lock:
            notebook = "\n".join(f"- {game_time(p['t'])}: {p['point']}" for p in self.notebook)
        messages = [{"role": "system", "content": self.system(COLOR, RESEARCH)},
                    {"role": "user", "content": f"DATA\n{data}\n\nNOTEBOOK\n{notebook or '(empty)'}\n\n"
                                                f"RECENT LINES (oldest first)\n{recent or '(none yet)'}"}]
        jotted = 0
        for _ in range(RESEARCH_ROUNDS):
            try:
                message = self.chat({"model": self.models[COLOR], "max_tokens": MAX_TOKENS, "messages": messages,
                                     "tools": [*LOOKUPS, JOT]})
            except (OSError, ValueError, KeyError, IndexError, TypeError, http.client.HTTPException) as e:
                why = failure(e)
                if isinstance(e, urllib.error.HTTPError) and e.code == 400 and REFUSED.search(why) and not jotted \
                        and len(messages) == 2:
                    self.no_tools.add(self.models[COLOR])   # no research without tools
                self.log(f"caster: research call failed: {why}")
                break
            if not (calls := message.get("tool_calls") or []):
                break
            answers = self.answers(calls, lambda args: self.jot(game, t, args))
            jotted += sum(a["content"].startswith("jotted (") for a in answers)
            messages += [assistant(message, calls), *answers]
        return jotted

    def jot(self, game: str | None, t: float, args: dict) -> str:
        point = " ".join(str(args.get("point") or "").split())[:POINT_CHARS]
        if not point:
            return "error: jot needs a point"
        with self.data, self.lock:   # a new game can't begin between the check and the jot
            if self.match.game != game:
                return "error: that game is over; a new one has begun"
            self.notebook.append({"t": t, "slot": self.match.slot_named(args.get("player")), "point": point,
                                  "beats": 0})
            del self.notebook[:-NOTEBOOK]
            return f"jotted ({len(self.notebook)} in the notebook)"

    # ---- the lookups: the match as short text ----

    def lookup(self, name, args) -> str:
        """A lookup's answer, as the model reads it: an error is text too, never an exception."""
        if name not in LOOKUP_ARGS:
            return f"error: there is no tool {name!r}; the tools: {', '.join(LOOKUP_ARGS)}, say"
        try:
            args = json.loads(args) if isinstance(args, str) and args.strip() else args or {}
        except ValueError:
            return f"error: the arguments are not JSON: {str(args)[:80]!r}"
        if not isinstance(args, dict):
            return "error: the arguments must be an object"
        takes = LOOKUP_ARGS[name]
        needs = [k for k in REQUIRED[name] if args.get(k) in (None, "")]
        if needs or (takes and set(args) - takes and not set(args) & takes):
            return (f"error: {name} needs {', '.join(needs) or 'other arguments'}; it takes "
                    f"{', '.join(sorted(takes)) or 'none'}, got {', '.join(sorted(args)) or 'none'}")
        ignored = sorted(set(args) - takes)
        try:
            with self.data:
                if not self.match.started:
                    return "error: the game hasn't started"
                out = getattr(self, f"look_{name}")(**{k: v for k, v in args.items()
                                                       if k in takes and v not in (None, "")})
            if ignored:
                out = f"(ignored {', '.join(ignored)}: {name} takes {', '.join(sorted(takes)) or 'none'})\n{out}"
        except Unknown as e:
            return f"error: {e}"
        except Exception as e:  # a model's odd arguments (or a bug) answer with an error, never stop the line
            self.log(f"caster: the {name} lookup failed on {json.dumps(args)[:200]}: {e!r}")
            return f"error: {name} failed on those arguments ({type(e).__name__})"
        return out if len(out) <= TOOL_CHARS else out[:TOOL_CHARS - 1].rsplit("\n", 1)[0] + "\n…"

    def player_of(self, name) -> dict:
        found = self.match.named(name)
        if found is None:
            players = ", ".join(f"{self.match.who(p['slot'])} (slot {p['slot']})" for p in self.match.players)
            raise Unknown(f"no player {name!r}; the players: {players}")
        return found

    def look_standings(self) -> str:
        return "\n".join([f"At {game_time(self.match.t)}:", *self.standings()])

    def look_player(self, player) -> str:
        m = self.match
        p = self.player_of(player)
        slot = p["slot"]
        out = [f"{m.who(slot)}, {p.get('controller') or 'a player'}: {m.ranked().index(p) + 1} of {len(m.players)} "
               f"on score at {game_time(m.t)}" + ("; GAME OVER" if m.over else ""),
               "; ".join(stat_parts(p["stats"], {})) or "no stats yet"]
        back = [f"{game_time(h['t'])}: score {number(was.get('score'))}, army {number(was.get('army'))}"
                for h in (m.history_at(m.t - 60 * minutes) for minutes in (1, 3, 5))
                if (was := (h.get("players") or {}).get(str(slot)))]
        if back:
            out.append("Before: " + "; ".join(dict.fromkeys(back)))
        mine = [x for x in m.moments if m.involved(x, p)]
        if mine:
            out.append("Feed: " + ", ".join(f"{n} {k}" for k, n in Counter(x["kind"] for x in mine).most_common()))
            out += [f"- {game_time(x['t'])}: {x['text']}" for x in mine[-6:]]
        out += [f'Plan, {game_time(n["t"])}: "{n["text"]}"' for n in m.notes if n["slot"] == slot][-3:]
        if agent := agent_part(p.get("agent")):
            out.append(f"Its AI: {agent}")
        return "\n".join(out)

    def look_trend(self, stat, since_t=None) -> str:
        m = self.match
        if stat not in TREND_STATS:
            raise Unknown(f"no stat {stat!r}; the stats: {', '.join(TREND_STATS)}")
        span = [h for h in m.history if since_t is None or h["t"] >= float(since_t)]
        if not span:
            raise Unknown(f"no history since {game_time(float(since_t))}: the game is at {game_time(m.t)}")
        picks = sorted({round(k * (len(span) - 1) / 11) for k in range(12)}) if len(span) > 12 else range(len(span))
        sample = [span[k] for k in picks]
        out = [f"{stat}, {game_time(span[0]['t'])} to {game_time(span[-1]['t'])}:"]
        for p in m.players[:8]:
            known = [(h["t"], v) for h in sample
                     if (v := (h["players"].get(str(p["slot"])) or {}).get(stat)) is not None]
            change = f" ({known[-1][1] - known[0][1]:+,} over these)" if len(known) > 1 else ""
            out.append(f"{m.who(p['slot'])}: " + (", ".join(f"{game_time(t)}: {v:,}" for t, v in known) or "no data")
                       + change)
        return "\n".join(out)

    def look_moments(self, kind=None, side=None, since_t=None) -> str:
        m = self.match
        p = self.player_of(side) if side is not None else None
        since = float(since_t) if since_t is not None else None
        found = [x for x in m.moments if (kind is None or x["kind"] == kind) and (since is None or x["t"] >= since)
                 and (p is None or m.involved(x, p))]
        if not found:
            kinds = sorted({str(x["kind"]) for x in m.moments})
            return "No such moments" + (f"; the kinds in this game: {', '.join(kinds)}" if kind else "")
        head = f"{len(found)} moments" + (", the latest 15:" if len(found) > 15 else ":")
        return "\n".join([head, *(f"- {game_time(x['t'])}: {x['text']}" + (" (major)" if x.get("major") else "")
                                  for x in found[-15:])])

    def look_notes(self, player=None) -> str:
        m = self.match
        slot = self.player_of(player)["slot"] if player is not None else None
        found = [n for n in m.notes if slot is None or n["slot"] == slot]
        if not found:
            return "No notes" + (f" from {m.who(slot)}" if slot is not None else "") + " yet"
        head = f"{len(found)} notes" + (", the latest 10:" if len(found) > 10 else ":")
        return "\n".join([head, *(f'- {game_time(n["t"])}, {m.who(n["slot"])}: "{n["text"]}"' for n in found[-10:])])

    def look_said(self, query="") -> str:
        words = str(query).lower().split()
        lines = [x for x in self.game_lines() if all(w in f"{x['name']} {x['text']}".lower() for w in words)]
        if not lines:
            return "The desk hasn't said that yet" if words else "The desk hasn't said anything yet"
        head = f"{len(lines)} lines" + (", the latest 8:" if len(lines) > 8 else ":")
        return "\n".join([head, *(f"- {game_time(x['t'])}, {x['name']}: {x['text']}" for x in lines[-8:])])

    # ---- the DATA the casters read ----

    def summary(self) -> str:
        m = self.match
        out = [f'Broadcast: "{self.title}"'] if self.title else []
        out += [f"Game: {m.title}"] if m.title else []
        out.append(f"Clock: {game_time(m.t)}"
                   + (f" of {game_time(m.limit)}, {game_time(max(0, m.limit - m.t))} left" if m.limit else "")
                   + (" (GAME OVER)" if m.over else ""))
        if m.over:
            out.append(f"Result: {self.result()}")
        out.append(f"Standings (score and army value in resources, each with its change over the last "
                   f"{MOMENTUM_SECONDS // 60} minutes; gold and lumber; food used of the cap; units and structures; "
                   "an AI's decisions, tokens and cost so far):")
        out += self.standings()
        scores = m.scores(m.history[-1]) if m.history else {}
        if leader := m.leader_since():
            gap = sorted(scores.values(), reverse=True)
            out.append(f"Leader: {m.who(leader[0])}, ahead on score since {game_time(leader[1])}"
                       + (f", by {gap[0] - gap[1]:,}" if len(gap) > 1 else ""))
        elif scores:
            top = max(scores.values())
            out.append(f"No leader: {' and '.join(m.who(s) for s, v in scores.items() if v == top)} are level on "
                       f"{top:,}")
        if recent := [x for x in m.moments if x["t"] >= m.t - RECENT_SECONDS][-12:]:
            out.append("Recent moments (the feed, newest last):\n"
                       + "\n".join(f"- {game_time(x['t'])}: {x['text']}" for x in recent))
        return "\n".join(out)

    def standings(self) -> list[str]:
        """A line per player, best score first, then its latest plan."""
        m = self.match
        before = (m.history_at(m.t - MOMENTUM_SECONDS).get("players") or {})
        plans = {n["slot"]: n for n in m.notes}
        out = []
        for n, p in enumerate(m.ranked(), 1):
            parts = stat_parts(p["stats"], before.get(str(p["slot"])) or {})
            parts += [agent] if (agent := agent_part(p.get("agent"))) else []
            out.append(f"{n}. {m.who(p['slot'])}, {p.get('controller') or 'a player'}: " + "; ".join(parts))
            if plan := plans.get(p["slot"]):
                out.append(f'   plan ({game_time(plan["t"])}): "{plan["text"]}"')
        return out


def rank(kind: str) -> int:
    return KINDS.index(kind) if kind in KINDS else len(KINDS)


def game_time(t) -> str:
    """Game seconds as a clock shows them: 6:05."""
    t = int(t or 0)
    return f"{t // 60}:{t % 60:02d}"


def number(value) -> str:
    return "?" if value is None else f"{value:,}"


def stat_parts(st: dict, was: dict) -> list[str]:
    """A player's stats as the DATA puts them, score and army with their change since `was`."""
    out = []
    for key, name in (("score", "score"), ("army", "army")):
        if (now := st.get(key)) is not None:
            out.append(f"{name} {now:,}" + (f" ({now - was[key]:+,})" if was.get(key) is not None else ""))
    if st.get("gold") is not None:
        out.append(f"{st['gold']:,} gold, {number(st.get('lumber'))} lumber")
    if len(food := st.get("food") or ()) == 2 and None not in food:
        out.append(f"food {food[0]} of {food[1]}")
    if st.get("units") is not None:
        out.append(f"{st['units']} units, {number(st.get('structures'))} structures")
    return out


def agent_part(agent: dict | None) -> str:
    """An AI player's running costs, from its stats note."""
    agent = agent or {}
    parts = [f"{agent['decisions']:,} decisions"] if agent.get("decisions") is not None else []
    parts += [f"{agent['tokens']:,} tokens"] if agent.get("tokens") is not None else []
    parts += [f"${agent['cost_usd']:.2f} so far"] if agent.get("cost_usd") is not None else []
    return ", ".join(parts)


def assistant(message: dict, calls: list) -> dict:
    """A reply's turn as it goes back to the model with the answers to its calls: with its thinking, which a model that
    thinks (Anthropic's, through LiteLLM) wants back before its tool calls."""
    out = {"role": "assistant", "content": message.get("content"), "tool_calls": calls}
    out |= {k: message[k] for k in ("thinking_blocks", "reasoning_content") if message.get(k)}
    return out


def arguments(call: dict) -> dict:
    """A tool call's arguments as an object: {} when they aren't one."""
    args = (call.get("function") or {}).get("arguments")
    try:
        args = json.loads(args) if isinstance(args, str) else args
    except ValueError:
        return {}
    return args if isinstance(args, dict) else {}


def casters_of(config: dict) -> dict[str, tuple[str, str, str]]:
    """The two casters as a task's broadcast sets them up (`play_by_play` and `analyst`, each {name, voice, style}),
    with the default for whatever it leaves out."""
    return {speaker: tuple((config.get(seat) or {}).get(key, default)
                           for key, default in zip(("name", "voice", "style"), CASTERS[speaker], strict=True))
            for speaker, seat in SEATS.items()}


def models_of(config: dict) -> dict[str, str]:
    """Each caster's model as a task's broadcast sets it: its own `model`, else the casters' `model`, else MODEL."""
    return {speaker: (config.get(seat) or {}).get("model") or config.get("model") or MODEL
            for speaker, seat in SEATS.items()}


def renamed(text: str, casters: dict[str, tuple[str, str, str]]) -> str:
    """`text`, written for Max and Ada, with the casters' own names."""
    names = {CASTERS[s][0]: casters[s][0] for s in CASTERS}
    return re.sub(r"\b(Max|Ada)\b", lambda m: names[m[1]], text)


def spoken(text, names: tuple[str, ...] = ("Max", "Ada")) -> str:
    """A line as it is said: no stage directions or speaker prefix, "bleep" for a blocked or masked word (a voice
    could read "s***" as the word), at most MAX_WORDS words (whole sentences)."""
    text = MASKED.sub("bleep", str(text or ""))   # before its stars read as a stage direction
    text = BLOCKED.sub("bleep", re.sub(r"\*[^*]*\*|\[[^\]]*\]", "", text))
    prefix = "|".join(re.escape(name) for name in names)
    text = " ".join(re.sub(rf"^(?:\s*(?:{prefix})\s*:)+\s*", "", text).split())   # "Max: Ada: …" too
    if len(text.split()) <= MAX_WORDS:
        return text
    kept: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if len(" ".join([*kept, sentence]).split()) > MAX_WORDS:
            break
        kept.append(sentence)
    return " ".join(kept) or " ".join(text.split()[:MAX_WORDS]) + "…"


def wav_chunks(wav: bytes):
    """(id, offset of its data, declared size) of each chunk of a WAV file."""
    if wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
        raise ValueError("not a WAV file")
    pos = 12
    while pos + 8 <= len(wav):
        cid, size = wav[pos:pos + 4], int.from_bytes(wav[pos + 4:pos + 8], "little")
        yield cid, pos + 8, size
        if cid == b"data":
            return
        pos += 8 + size + (size & 1)


def fixed_wav(wav: bytes) -> bytes:
    """A streamed WAV (sizes left at 0xFFFFFFFF, as text-to-speech APIs send it) with its real sizes, so a browser
    knows its length."""
    data = next((at for cid, at, _ in wav_chunks(wav) if cid == b"data"), None)
    if data is None:
        raise ValueError("a WAV file without audio")
    out = bytearray(wav)
    out[4:8] = (len(wav) - 8).to_bytes(4, "little")
    out[data - 4:data] = (len(wav) - data).to_bytes(4, "little")
    return bytes(out)


def leveled(wav: bytes) -> bytes:
    """A 16-bit PCM WAV scaled to LEVEL_DBFS, or as close as it gets without clipping; other WAVs as they are."""
    chunks = {cid: at for cid, at, _ in wav_chunks(wav)}
    fmt, data = chunks.get(b"fmt "), chunks.get(b"data")
    if fmt is None or data is None or wav[fmt:fmt + 2] != b"\x01\x00" or wav[fmt + 14:fmt + 16] != b"\x10\x00":
        return wav
    end = data + (len(wav) - data) // 2 * 2
    samples = array.array("h", wav[data:end])
    if sys.byteorder == "big":
        samples.byteswap()
    if not samples or not (peak := max(max(samples), -min(samples))):
        return wav
    rms = math.sqrt(math.fsum(x * x for x in samples) / len(samples))
    gain = min(32767 * 10 ** (LEVEL_DBFS / 20) / rms, 32767 / peak)
    out = array.array("h", (max(-32768, min(32767, round(x * gain))) for x in samples))
    if sys.byteorder == "big":
        out.byteswap()
    return wav[:data] + out.tobytes() + wav[end:]


def wav_seconds(wav: bytes) -> float:
    byte_rate = None
    for cid, at, size in wav_chunks(wav):
        if cid == b"fmt ":
            byte_rate = int.from_bytes(wav[at + 8:at + 12], "little")
        elif cid == b"data" and byte_rate:
            return min(size, len(wav) - at) / byte_rate
    raise ValueError("a WAV file without a format or audio")


def failure(e: Exception) -> str:
    """What went wrong, with an HTTP error's reply, which says why."""
    if isinstance(e, urllib.error.HTTPError):
        try:
            body = e.read(300).decode(errors="replace")
        except OSError:
            body = ""
        return f"HTTP {e.code} {' '.join(body.split())}"
    return str(e) or repr(e)


def handler(caster: Caster) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            url = urllib.parse.urlsplit(self.path)
            if url.path == "/health":
                return self.reply(200, b"ok", "text/plain")
            if url.path == "/cast.json":
                since = urllib.parse.parse_qs(url.query).get("since", ["0"])[0]
                if not re.fullmatch(r"-?\d+", since):
                    return self.reply(400, b"since is a line id: the last one you have, or 0", "text/plain")
                return self.reply(200, json.dumps(caster.lines_since(int(since))).encode(), "application/json")
            found = re.fullmatch(r"/audio/(\d+)\.wav", url.path)
            if found and (wav := caster.audio.get(int(found[1]))) is not None:
                return self.reply(200, wav, "audio/wav")
            self.reply(404, b"not found", "text/plain")

        def reply(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args) -> None:
            pass

    return Handler


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--data", required=True, help="the env's live view, e.g. http://127.0.0.1:41589/live")
    p.add_argument("--port", type=int, default=8790)
    p.add_argument("--config", type=json.loads, default={},
                   help='the casters as a task\'s broadcast sets them up, JSON: {"model": the model that writes the '
                        'lines, "tts_model": the one that speaks them, "play_by_play" and "analyst": {"name", "voice", '
                        '"style", "model": that caster\'s own}}; what it leaves out keeps its default')
    p.add_argument("--title", help="the broadcast's title, for the intro")
    args = p.parse_args()
    base_url, api_key = os.environ.get("CAST_BASE_URL", "").strip(), os.environ.get("CAST_API_KEY", "").strip()
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in api_key):
        print("caster: CAST_API_KEY has a control character in it", file=sys.stderr)
        return 2
    if not base_url or not api_key:
        print("caster: set CAST_BASE_URL (the endpoint, e.g. https://your-litellm-proxy) and CAST_API_KEY",
              file=sys.stderr)
        return 2
    config = args.config
    caster = Caster(args.data, base_url, api_key, models=models_of(config),
                    tts_model=config.get("tts_model", TTS_MODEL), casters=casters_of(config), title=args.title)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler(caster))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    caster.log(f"Casting {caster.data_url} on http://127.0.0.1:{args.port}/cast.json: "
               f"{' and '.join(name for name, _, _ in caster.casters.values())}, written by "
               f"{' and '.join(dict.fromkeys(caster.models.values()))}, voiced by {caster.tts_model}")
    caster.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
