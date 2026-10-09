"""Build the Hugging Face dataset earakely-scale/wc3env-AgentEnv from the recorded sweeps, and push it. Two kinds of
tables share the repo:

- agentenv-hf's, one pair per v1 bundle, as `agent-env hf publish` writes them: the bundle's tasks and runs, each run's
  record and chat transcript, and the bundle itself, which `agent-env hf run` plays. The bundles are the sweeps' tasks
  without the model: a v1 task plays wc3-llm's default model, and `--model` picks another. A bundle's runs come from
  sweeps that played each task once per model, so they are built sweep by sweep and joined to their v1 task by its
  axes (template, map, opponent, seed).
- WC3's own, one family per bundle: what each task stages and checks, and each run's outcome, score, checks and cost,
  joined to agentenv-hf's episodes by `episode_id`; and the Warcraft-against-Warcraft duels as the duels' references.

`build` runs where the sweeps ran (their folders and the agent-env store they wrote to) and writes a folder; every
file goes through agentenv-hf's check for keys and token shapes, and a check for this machine's paths and hosts, first.
`push` sends the folder from a machine with a Hub login as one commit on top of the commit it read, removing what the
build no longer writes, and tags it.

    python scripts/hub_dataset.py build --runs ~/runs --out build/hub/dataset
    python scripts/hub_dataset.py push build/hub/dataset --tag v0.2.0 --message "v0.2.0: ..."
"""

import argparse
import dataclasses
import io
import json
import re
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from agent_env.artifact import FileArtifact
from agent_env.store.routing import namespace_routing
from agent_env.task.store import find_task_instance
from agentenv_hf import dataset, records, runs
from agentenv_hf.card import card
from agentenv_hf.scan import known_values, scan
from huggingface_hub import CommitOperationAdd, CommitOperationDelete, HfApi
from PIL import Image, ImageChops

from agentenv_rts.recording import BAR
from agentenv_wc3 import drills
from agentenv_wc3.prompts import RACES
from agentenv_wc3.sweep import DIFFICULTIES, Spec, map_name, results, task_name, task_of

ROOT = Path(__file__).resolve().parents[1]
SWEEPS = ROOT / "sweeps"
CARD = ROOT / "hub" / "dataset" / "README.md"
SPACE = ROOT / "hub" / "space"
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
REPO = "earakely-scale/wc3env-AgentEnv"
PLUGIN = "agentenv-wc3 @ git+https://github.com/earakely-scale/agentenv-wc3-plugin@{ref}"
SETUP = ('agent-env wc3 build-worker "<game folder>" && agent-env wc3 license import "<game folder>" && '
         "agent-env wc3 setup --agent")
SPLIT = "eval"
PLACEHOLDER = "anthropic/claude-haiku-4-5"   # task_of sets a model; the bundle's tasks leave it to the agent
REFERENCES = "duel-baselines-final"
HERO = ("duels-final", "fireworks_ai/deepseek-v4p1-flash")   # the card's clip: this model's most one-sided duel win
MODELS = {
    "fireworks_ai/deepseek-v4p1-flash": "DeepSeek V4.1 Flash",
    "anthropic/claude-haiku-4-5": "Claude Haiku 4.5",
    "openai/gpt-5.4-mini": "GPT-5.4 mini",
    "anthropic/claude-sonnet-5-5": "Claude Sonnet 5.5",
    "anthropic/claude-opus-5-5": "Claude Opus 5.5",
}
SCRIPTED = "Warcraft's attack-move"   # both player slots of a reference duel
SCRIPTS = {"attack": "a scripted attacker", "raid": "scripted raiders"}
MIRROR = "a mirror army on Warcraft's attack-move"
SETS = ("ladder", "drills", "duels", "references")
FEATURED = (   # the Space's "Start here", by episode id, each with what to watch for
    ("wc3-v1-ladder/ladder-frontier-claude-sonnet-5-5-8jb4mv4h",
     "Lasts the full 30 minutes against the normal AI for a draw, though its army trails from minute 12."),
    ("wc3-v1-duels/duels-undead-deepseek-v4p1-flash-s1-msjysvkm",
     "Beats Warcraft's own attack-move with the same Undead army in 54 seconds, keeping 79% of its army."),
    ("wc3-v1-drills/drills-hero-in-danger-deepseek-v4p1-flash-cqelp2cl",
     "Keeps a badly hurt hero alive with enemies on top of it: uses one of its items, spends its skill points and "
     "ends at 79% health."),
    ("wc3-v1-ladder/ladder-frontier-claude-opus-5-5-1vd9ncwb",
     "Matches the normal AI's army in value until minute 18; then the AI pulls away and wipes it out at 27.5."),
)
SAVED = {   # a run's match files that the dataset keeps, by kind and name ending, and where
    ("html_replay", ".html"): "replays/{id}.html",
    ("timeline", "-timeline.json"): "timelines/{id}.json",
    ("replay", ".w3g"): "w3g/{id}.w3g",
    ("replay", ".w3g.json"): "w3g/{id}.w3g.json",
    ("agent_files", "-wc3-llm-transcript.json"): "transcripts/{id}.json",
}
COLUMNS = {"replays": "replay", "timelines": "timeline", "w3g": "w3g", "transcripts": "transcript"}
FOLDERS = ("tasks", "episodes", "references", "raw", "runs", "bundles", "assets", *COLUMNS)
LEAKS = re.compile(rb"/home/|/Users/|scale\.com|amazonaws|\bi-0[0-9a-f]{8,}\b|arakelyan", re.IGNORECASE)


@dataclasses.dataclass(frozen=True)
class V1:
    family: str           # WC3's tables: tasks/<family>.parquet, episodes/<family>.parquet
    bundle: str           # agentenv-hf's tables and the bundle `agent-env hf run` plays
    spec: str             # the sweep spec its tasks come from
    prefix: str           # its task names: <prefix>-<the axes it varies>
    sweeps: tuple[str, ...]
    description: str


BUNDLES = (
    V1("drills", "wc3-v1-drills", "drills", "drill", ("drills-final",),
       "Warcraft III drills: 25 staged scenes of 1.5 to 10 game minutes, one skill each, graded on the share of their "
       "checks met."),
    V1("ladder", "wc3-v1-ladder", "ladder", "ladder", ("ladder-final", "frontier-final"),
       "Warcraft III against the game's own AI: Human against the easy, normal and insane Orc AI on two maps and two "
       "seeds, 30-minute games graded on the outcome."),
    V1("duels", "wc3-v1-duels", "duels", "mirror", ("duels-final",),
       "Warcraft III mirror duels: two identical late-game armies of one race, the model against Warcraft's own "
       "attack-move, graded on the outcome."),
)
DEFAULT = "wc3-v1-drills"


def spec(v1: V1) -> Spec:
    return dataclasses.replace(Spec.load(SWEEPS / f"{v1.spec}.toml"), name=v1.prefix, models=[PLACEHOLDER])


def v1_task(s: Spec, axes: dict) -> str:
    """The v1 task a sweep's run played: the one with its template, map, race, opponent and seed."""
    return task_name(s, {**axes, "template": axes["template"].removesuffix("-baseline"), "model": PLACEHOLDER})


def write_bundle(v1: V1, root: Path) -> dict[str, dict]:
    """Writes the bundle: its tasks, its evals and a README. Returns each task's axes by name."""
    s = spec(v1)
    templates = s.templates()
    shutil.rmtree(root, ignore_errors=True)
    (root / "tasks").mkdir(parents=True)
    (root / "evals").mkdir()
    combos = {}
    for c in s.combinations():
        name = task_name(s, c)
        steps = task_of(s, templates[c["template"]], c, name)
        next(step for step in steps if step["id"] == s.player_step).pop("model")
        (root / "tasks" / f"{name}.json").write_text(json.dumps(steps, indent=2) + "\n")
        combos[name] = c
    for file, text in evals(v1, combos).items():
        (root / "evals" / file).write_text(text)
    (root / "README.md").write_text(
        f"{v1.description}\n\nPart of the dataset {REPO}, played by the agentenv-wc3 plugin "
        "(https://github.com/earakely-scale/agentenv-wc3-plugin) on your own copy of Warcraft III. Apache-2.0.\n")
    return combos


def evals(v1: V1, combos: dict[str, dict]) -> dict[str, str]:
    """Drills by skill (the plugin's own), the ladder by AI level, the duels as one."""
    def toml(names):
        return "tasks = [\n" + "".join(f'  "{n}",\n' for n in names) + "]\n"
    if v1.family == "drills":
        return drills.evals()
    if v1.family == "ladder":
        return {f"ladder-{level}.toml": toml(n for n, c in combos.items() if c["opponent"]["computer"] == level)
                for level in ("easy", "normal", "insane")}
    return {"duels.toml": toml(combos)}


def step(steps: list[dict], kind: str, **match) -> dict:
    return next(s for s in steps if s["type"] == kind and all(s.get(k) == v for k, v in match.items()))


def task_row(v1: V1, name: str, c: dict, steps: list[dict]) -> dict:
    settings = step(steps, "open_lobby")["game_settings"]
    agent = step(steps, "deploy_agent", agent_name="wc3")
    pretty, _ = map_name(c["map"])
    row = {"task": name, "map": pretty, "map_file": c["map"], "race": c["race"], "seed": c["seed"],
           "time_limit_seconds": settings.get("time_limit_seconds"),
           "max_cost_usd": float(agent["env_vars"]["WC3_MAX_COST_USD"])}
    opponent = c["opponent"] or {}
    if v1.family == "drills":
        checks = step(steps, "rts_grade")["checks"]
        scripted = next((s.get("env_vars", {}).get("SCRIPT") for s in steps
                         if s["type"] == "deploy_agent" and s.get("a2a_agent_id") == "wc3-scripted"), None)
        return row | {"skill": drills.skill(name), "opponent_ai": opponent.get("computer"),
                      "opponent_race": opponent.get("race"), "opponent_script": scripted,
                      "num_checks": len(checks), "checks": json.dumps(checks)}
    if v1.family == "ladder":
        return row | {"opponent_ai": opponent["computer"], "opponent_race": opponent["race"]}
    return row | {"decide_ratio": settings.get("decide_ratio")}


def measured(result: dict) -> float | None:
    found = re.search(r"measured (-?[\d.]+)", result.get("evidence") or "")
    return float(found[1]) if found else None


def episode_row(v1: V1, episode_id: str, task: str, sweep: str, r: dict, verifications: dict) -> dict:
    """WC3's row for one run, from its sweep's results row and its verifier's results."""
    share = (r["score"] / (r["score"] + r["opponent_score"])
             if r.get("score") is not None and r.get("opponent_score") else None)
    row = {"episode_id": episode_id, "task": task, "model": r["model"], "sweep": sweep, "outcome": r["outcome"],
           "result": r["result"], "reward": r["grade"], "score": r["score"], "opponent_score": r["opponent_score"],
           "score_share": share, "units_killed": r["units_killed"], "orders": r["orders"],
           "orders_refused": r.get("refused"), "turns": r["decisions"], "game_seconds": r["game_seconds"],
           "cost_usd": r["cost_usd"]}
    results = next(iter(verifications.values()))["results"]
    if v1.family == "drills":
        checks = [{"check": x["criterion"], "met": x["result"], "measured": measured(x)}
                  for x in results if x.get("weight") == 1]
        return row | {"skill": drills.skill(task), "checks_met": sum(x["met"] for x in checks),
                      "num_checks": len(checks), "passed": all(x["met"] for x in checks),
                      "checks": json.dumps(checks)}
    if v1.family == "duels":
        by_name = {x["name"]: x for x in results}
        return row | {"race": r["race"], "seed": r["seed"],
                      "army_kept_percent": measured(by_name.get("army_kept_percent", {})),
                      "enemy_army_destroyed_percent": measured(by_name.get("enemy_army_destroyed_percent", {}))}
    return row | {"map_file": r["map"], "opponent_ai": r["opponent"]["computer"], "seed": r["seed"]}


def reference_row(s: Spec, tasks: set[str], r: dict) -> dict:
    """A Warcraft-against-Warcraft duel, from player 0's side: the floor a model's duel from that slot reads against."""
    task = v1_task(s, r)
    return {"task": task if task in tasks else None, "race": r["race"], "seed": r["seed"], "outcome": r["outcome"],
            "result": r["result"], "reward": r["grade"], "score": r["score"], "opponent_score": r["opponent_score"],
            "score_share": r["score"] / (r["score"] + r["opponent_score"]), "units_killed": r["units_killed"],
            "orders": r["orders"], "game_seconds": r["game_seconds"]}


def run_files(instance: str, episode_id: str) -> tuple[dict[str, bytes], dict[str, str | None]]:
    """A run's files the dataset keeps (its HTML replay, timeline, .w3g with its startup options, and wc3-llm's
    untrimmed transcript) under its episode id, and the columns that point to them. The map's video is left out:
    the HTML replay plays the same pictures."""
    record = find_task_instance(instance)
    metadata = (record.context or {}).get("metadata") or {}
    saved = [f for fs in (metadata.get("match_files") or {}).values() for f in fs]
    files = {}
    for f in saved:
        where = next((path for (kind, end), path in SAVED.items() if f["kind"] == kind and f["name"].endswith(end)
                      and not (end == ".w3g" and f["name"].endswith(".w3g.json"))), None)
        if where:
            if (path := where.format(id=episode_id)) in files:
                raise ValueError(f"{instance}: more than one {f['kind']} {f['name']}")
            files[path] = FileArtifact.get(f["artifact_id"], f["version"]).load()
    columns = {column: next((p for p in files if p.startswith(f"{folder}/") and not p.endswith(".w3g.json")), None)
               for folder, column in COLUMNS.items()}
    return files, columns


def parquet(rows: list[dict]) -> bytes:
    keys = dict.fromkeys(k for row in rows for k in row)
    sink = io.BytesIO()
    pq.write_table(pa.Table.from_pylist([{k: row.get(k) for k in keys} for row in rows]), sink)
    return sink.getvalue()


def family_files(v1: V1, root: Path, runs_dir: Path) -> dict[str, bytes]:
    """The bundle's files and both kinds of its tables."""
    combos = write_bundle(v1, root)
    s = spec(v1)
    files = dataset.build(runs.locate(str(root)), name=v1.bundle, split=SPLIT, which="latest").files
    parts, raw, rows = [], {}, []
    for sweep in v1.sweeps:
        played = runs.locate(str(runs_dir / sweep))
        given = {r["instance"]: r for r in results(runs_dir / sweep) if not r.get("void") and r.get("instance")}
        built = dataset.build(played, name=v1.bundle, split=SPLIT, which="latest", instance_ids=tuple(given)).files
        rewrite = records.Rewrite(played.id_root, v1.bundle)
        by_episode = {rewrite.text(i): r for i, r in given.items()}
        episodes = pq.read_table(io.BytesIO(built[f"episodes/{v1.bundle}.parquet"]))
        names = [v1_task(s, by_episode[e]) for e in episodes.column("episode_id").to_pylist()]
        if unknown := set(names) - set(combos):
            raise ValueError(f"{sweep}: runs of tasks {sorted(unknown)} that {v1.bundle} doesn't have")
        parts.append(episodes.set_column(episodes.schema.get_field_index("task"), "task", pa.array(names)))
        for line in built[f"raw/{v1.bundle}.jsonl"].decode().splitlines():
            run = json.loads(line)
            raw[run["episode_id"]] = line
            r = by_episode[run["episode_id"]]
            kept, columns = run_files(r["instance"], run["episode_id"])
            files |= kept
            rows.append(episode_row(v1, run["episode_id"], v1_task(s, r), sweep, r,
                                    run["record"]["metadata"]["verifications"]) | columns)
    episodes = pa.concat_tables(parts).sort_by([("model", "ascending"), ("task", "ascending")])
    sink = io.BytesIO()
    pq.write_table(episodes, sink)
    order = episodes.column("episode_id").to_pylist()
    files[f"episodes/{v1.bundle}.parquet"] = sink.getvalue()
    files[f"raw/{v1.bundle}.jsonl"] = "".join(raw[i] + "\n" for i in order).encode()
    tasks = [task_row(v1, n, c, json.loads((root / "tasks" / f"{n}.json").read_text())) for n, c in combos.items()]
    files[f"tasks/{v1.family}.parquet"] = parquet(tasks)
    files[f"episodes/{v1.family}.parquet"] = parquet(sorted(rows, key=lambda r: order.index(r["episode_id"])))
    if v1.family == "duels":
        references = []
        for r in results(runs_dir / REFERENCES):
            episode_id = f"duels-references/{r['instance'].rpartition('/')[2]}"
            kept, columns = run_files(r["instance"], episode_id)
            files |= kept
            references.append({"episode_id": episode_id, **reference_row(s, set(combos), r), **columns})
        files["references/duels.parquet"] = parquet(references)
    return files


def hero_video(runs_dir: Path) -> tuple[bytes, dict]:
    """The map's video of HERO's model's most one-sided duel win, and that run's results row."""
    sweep, model = HERO
    best = max((r for r in results(runs_dir / sweep) if r["model"] == model and r["outcome"] == "win"),
               key=lambda r: r["score"] / (r["score"] + r["opponent_score"]))
    metadata = (find_task_instance(best["instance"]).context or {}).get("metadata") or {}
    video = next(f for fs in (metadata.get("match_files") or {}).values() for f in fs if f["kind"] == "map_video")
    return FileArtifact.get(video["artifact_id"], video["version"]).load(), best


def card_text(plugin: str) -> str:
    """hub/dataset/README.md with each bundle's two configs and its needs, as agentenv-hf's publish writes them."""
    text = CARD.read_text()
    for v1 in BUNDLES:
        text = card(text, name=v1.bundle, split=SPLIT, repo=None, description=None, license=None,
                    needs={"plugins": [plugin], "setup": SETUP})
    return text


def check(files: dict[str, bytes]) -> None:
    """agentenv-hf's key check, then this machine's paths and hosts, which no record should carry out."""
    scan(files, known_values())
    for path, content in files.items():
        if path.endswith(".parquet"):
            content = json.dumps(pq.read_table(io.BytesIO(content)).to_pylist(), default=str).encode()
        if found := LEAKS.search(content):
            raise ValueError(f"{path} holds {found[0].decode(errors='replace')!r}")


def build(runs_dir: Path, out: Path, plugin: str) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    with namespace_routing():   # the sweeps' runs live in the @local namespace's own store
        for v1 in BUNDLES:
            files |= family_files(v1, out / "bundles" / v1.bundle, runs_dir)
        files["assets/hero.mp4"], hero = hero_video(runs_dir)
    print(f"the card's clip: {hero['task']} ({hero['model']}, {hero['outcome']})")
    for sweep in sorted({*(s for v1 in BUNDLES for s in v1.sweeps), REFERENCES}):
        for name in ("sweep.json", "results.jsonl", "report.md"):
            if (runs_dir / sweep / name).is_file():
                files[f"runs/{sweep}/{name}"] = (runs_dir / sweep / name).read_bytes()
    files["README.md"] = card_text(plugin).encode()
    return files


def write(out: Path, files: dict[str, bytes]) -> None:
    for folder in FOLDERS:
        shutil.rmtree(out / folder, ignore_errors=True)
    for path, content in files.items():
        (out / path).parent.mkdir(parents=True, exist_ok=True)
        (out / path).write_bytes(content)


def media(out: Path) -> None:
    """The card's clip and the Space's thumbnail from assets/hero.mp4: the duel cropped to where it moves, under the
    recording's title bar, as an animated WebP (ffmpeg cuts the frames, Pillow writes them) and its frame two thirds
    in, when the armies are locked together."""
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(out / "assets" / "hero.mp4"), "-vf", "fps=10",
                        f"{tmp}/%04d.png"], check=True)
        frames = [Image.open(f).convert("RGB") for f in sorted(Path(tmp).glob("*.png"))]
    box = action(frames)
    shots = [shot(f, box) for f in frames]
    shots[0].save(out / "assets" / "hero.webp", save_all=True, append_images=shots[1:], duration=100, loop=0,
                  quality=80)
    shots[len(shots) * 2 // 3].save(out / "assets" / "thumbnail.jpg", quality=90)


def action(frames: list[Image.Image], pad: int = 48) -> tuple[int, int, int, int]:
    """The map's box (below the title bar) where anything changes over the clip, padded and widened to 16:9."""
    w, h = frames[0].size
    first, moved = frames[0].crop((0, BAR, w, h)), None
    for frame in frames[1:]:
        changed = ImageChops.difference(first, frame.crop((0, BAR, w, h))).convert("L").point(lambda v: v > 40 and 255)
        moved = changed if moved is None else ImageChops.lighter(moved, changed)
    x0, y0, x1, y1 = moved.getbbox() or (0, 0, w, h - BAR)
    x0, y0, x1, y1 = x0 - pad, y0 - pad, x1 + pad, y1 + pad
    width = max(x1 - x0, (y1 - y0) * 16 // 9, w // 2)
    width = min(width, w, (h - BAR) * 16 // 9)
    height = width * 9 // 16
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    left = min(max(cx - width // 2, 0), w - width)
    top = min(max(cy - height // 2, 0), h - BAR - height)
    return left, top, left + width, top + height


def shot(frame: Image.Image, box: tuple[int, int, int, int], width: int = 800) -> Image.Image:
    w = frame.width
    left, top, right, bottom = box
    region = frame.crop((left, BAR + top, right, BAR + bottom)).resize((w, w * 9 // 16), Image.LANCZOS)
    canvas = Image.new("RGB", (w, BAR + region.height))
    canvas.paste(frame.crop((0, 0, w, BAR)), (0, 0))
    canvas.paste(region, (0, BAR))
    return canvas.resize((width, canvas.height * width // w), Image.LANCZOS)


def space_runs(dataset_dir: Path) -> list[dict]:
    """Every run the Space lists, from WC3's tables: what it played, how it ended and where its files are."""
    def table(path):
        return pq.read_table(dataset_dir / path).to_pylist()
    tasks = {r["task"]: r for f in ("ladder", "drills", "duels") for r in table(f"tasks/{f}.parquet")}
    out = []
    for family in ("ladder", "drills", "duels"):
        for r in table(f"episodes/{family}.parquet"):
            task = tasks[r["task"]]
            out.append({
                "id": r["episode_id"], "set": family, "task": r["task"], "model": MODELS.get(r["model"], r["model"]),
                "outcome": r["outcome"], "reward": r["reward"], "score_share": r["score_share"],
                "game_seconds": r["game_seconds"], "turns": r["turns"], "cost_usd": r["cost_usd"],
                "orders": r["orders"], "orders_refused": r["orders_refused"], "units_killed": r["units_killed"],
                "map": task["map"], "race": task["race"], "seed": task["seed"],
                "opponent": (f"the {task['opponent_ai']} {RACES[task['opponent_race']]} AI" if task.get("opponent_ai")
                             else SCRIPTS.get(task.get("opponent_script")) or MIRROR),
                "skill": r.get("skill"), "checks_met": r.get("checks_met"), "num_checks": r.get("num_checks"),
                "passed": r.get("passed"), "checks": json.loads(r["checks"]) if r.get("checks") else None,
                **{column: r.get(column) for column in COLUMNS.values()}})
    for r in table("references/duels.parquet"):
        out.append({"id": r["episode_id"], "set": "references", "task": r["task"] or f"mirror-{r['race']}-s{r['seed']}",
                    "model": SCRIPTED, "outcome": r["outcome"], "reward": r["reward"],
                    "score_share": r["score_share"], "game_seconds": r["game_seconds"], "turns": None,
                    "cost_usd": 0.0, "orders": r["orders"], "orders_refused": None,
                    "units_killed": r["units_killed"], "map": "Echo Isles", "race": r["race"], "seed": r["seed"],
                    "opponent": MIRROR,
                    **{column: r.get(column) for column in COLUMNS.values()}})
    return sorted(out, key=order)


def order(run: dict) -> tuple:
    """The Space's default order: by task set, then the ladder by map, AI level and seed, the drills by skill as the
    plugin lists them, the duels by race and seed; then by model."""
    skills = list(drills.SKILLS)
    if run["set"] == "ladder":
        level = next(i for i, d in enumerate(DIFFICULTIES) if f" {d} " in run["opponent"])
        task = (run["map"], level, run["seed"])
    elif run["set"] == "drills":
        task = (skills.index(run["skill"]), run["task"])
    else:
        task = (list(RACES).index(run["race"]), run["seed"])
    return SETS.index(run["set"]), task, run["model"]


def space(dataset_dir: Path, out: Path, revision: str) -> None:
    """The Space: hub/space's page, the runs it lists (runs.json) and its thumbnail. It loads each replay from the
    dataset at `revision`, so it holds no game data of its own."""
    shutil.rmtree(out, ignore_errors=True)
    shutil.copytree(SPACE, out)
    listed = space_runs(dataset_dir)
    known = {r["id"] for r in listed}
    if missing := [i for i, _ in FEATURED if i not in known]:
        raise ValueError(f"featured runs {missing} aren't in the dataset")
    (out / "runs.json").write_text(json.dumps({
        "dataset": REPO, "revision": revision, "plugin": f"v{VERSION}",
        "featured": [{"id": i, "why": why} for i, why in FEATURED], "runs": listed}, separators=(",", ":")))
    shutil.copy(dataset_dir / "assets" / "thumbnail.jpg", out / "thumbnail.jpg")


def push(out: Path, repo: str, tag: str | None, message: str, repo_type: str = "dataset") -> str:
    """One commit of the folder on top of the commit read, removing what the folder no longer has; then the tag."""
    api = HfApi()
    api.create_repo(repo, repo_type=repo_type, exist_ok=True, space_sdk="static" if repo_type == "space" else None)
    parent = api.repo_info(repo, repo_type=repo_type).sha
    local = {p.relative_to(out).as_posix(): p for p in sorted(out.rglob("*")) if p.is_file()}
    check({path: p.read_bytes() for path, p in local.items()})
    remote = set(api.list_repo_files(repo, repo_type=repo_type, revision=parent)) - {".gitattributes"}
    operations = [CommitOperationAdd(path_in_repo=path, path_or_fileobj=str(p)) for path, p in local.items()]
    operations += [CommitOperationDelete(path_in_repo=path) for path in sorted(remote - set(local))]
    commit = api.create_commit(repo, operations, commit_message=message, repo_type=repo_type, parent_commit=parent)
    if tag:
        api.create_tag(repo, tag=tag, repo_type=repo_type, revision=commit.oid)
    return commit.oid


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    b = commands.add_parser("build", help="write the dataset folder from the recorded sweeps")
    b.add_argument("--runs", type=Path, default=Path.home() / "runs", help="the folder of the sweeps' run folders")
    b.add_argument("--out", type=Path, required=True)
    b.add_argument("--plugin-ref", default=f"v{VERSION}", help="the plugin tag the card's needs pin")
    m = commands.add_parser("media", help="the card's clip and the Space's thumbnail, with ffmpeg")
    m.add_argument("out", type=Path)
    s = commands.add_parser("space", help="write the Space's folder from a built dataset folder")
    s.add_argument("dataset", type=Path)
    s.add_argument("--out", type=Path, required=True)
    s.add_argument("--revision", default=f"v{VERSION}", help="the dataset revision the Space loads replays from")
    p = commands.add_parser("push", help="push a built folder to the Hub as one commit")
    p.add_argument("out", type=Path)
    p.add_argument("--repo", default=REPO)
    p.add_argument("--type", dest="repo_type", choices=("dataset", "space"), default="dataset")
    p.add_argument("--tag")
    p.add_argument("--message", default="Publish wc3env on AgentEnv")
    args = parser.parse_args()
    if args.command == "build":
        files = build(args.runs.expanduser(), args.out, PLUGIN.format(ref=args.plugin_ref))
        check(files)
        write(args.out, files)
        for path in sorted(p for p in files if p.endswith(".parquet")):
            print(f"{path}: {pq.read_metadata(io.BytesIO(files[path])).num_rows} rows")
    elif args.command == "media":
        media(args.out)
    elif args.command == "space":
        space(args.dataset, args.out, args.revision)
    else:
        print(push(args.out, args.repo, args.tag, args.message, args.repo_type))


if __name__ == "__main__":
    main()
