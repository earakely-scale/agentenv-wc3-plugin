// The wc3env-AgentEnv replay browser: lists runs.json's runs and plays a run's HTML replay, fetched from the dataset
// at the revision runs.json pins (or from ?data=<base URL>, to try a dataset folder before it is pushed).
"use strict";

const SETS = [["all", "All"], ["ladder", "Ladder"], ["drills", "Drills"], ["duels", "Duels"],
              ["references", "Warcraft vs Warcraft"]];
const RACES = {human: "Human", orc: "Orc", undead: "Undead", night_elf: "Night Elf", nightelf: "Night Elf"};
const state = {set: "all", model: "", outcome: "", search: "", sort: "order", desc: false};
let DATA, RUNS, BASE;

const $ = (id) => document.getElementById(id);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null) continue;
    if (k === "class") node.className = v; else if (k === "text") node.textContent = v; else node.setAttribute(k, v);
  }
  for (const child of children.flat()) if (child != null) node.append(child);
  return node;
}

const race = (r) => RACES[r] || r;
const minutes = (s) => s == null ? "–" : `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`;
const money = (c) => c == null ? "–" : c === 0 ? "$0" : c < 0.01 ? "<$0.01" : `$${c.toFixed(2)}`;
const pct = (x) => x == null ? "–" : `${Math.round(x * 100)}%`;
const fileUrl = (path) => BASE + path;
const blobUrl = (path) => `https://huggingface.co/datasets/${DATA.dataset}/blob/${DATA.revision}/${path}`;

function title(run) {
  if (run.set === "ladder") return `vs ${run.opponent} · ${run.map} · seed ${run.seed}`;
  if (run.set === "drills") return `${run.task.replace(/^drill-/, "").replaceAll("-", " ")} drill`;
  return `${race(run.race)} mirror duel · seed ${run.seed}`;
}

function result(run) {
  if (run.set === "drills") return `${run.checks_met} of ${run.num_checks} checks${run.passed ? " · passed" : ""}`;
  return run.outcome;
}

function resultClass(run) {
  if (run.set === "drills") return run.passed ? "win" : run.checks_met / run.num_checks >= 0.75 ? "draw" : "loss";
  return run.outcome;
}

// Home

function featured() {
  const box = $("featured");
  for (const f of DATA.featured) {
    const run = RUNS.find((r) => r.id === f.id);
    box.append(el("a", {class: "card", href: `#run=${encodeURIComponent(run.id)}`},
      f.still ? el("img", {class: "still", src: f.still, alt: `${run.model}: ${title(run)}`, loading: "lazy"}) : null,
      el("span", {class: "set", text: SETS.find(([k]) => k === run.set)[1]}),
      el("b", {text: run.model}),
      el("span", {class: "sub", text: title(run)}),
      el("p", {text: f.why}),
      el("span", {class: "chips"}, el("span", {class: `chip ${resultClass(run)}`, text: result(run)}),
        el("span", {class: "chip", text: minutes(run.game_seconds)}), el("span", {class: "chip", text: money(run.cost_usd)}))));
  }
}

function group(runs, by) {
  const out = new Map();
  for (const r of runs) { if (!out.has(r[by])) out.set(r[by], []); out.get(r[by]).push(r); }
  return out;
}

const mean = (xs) => xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null;
const wdl = (rs) => ["win", "draw", "loss"].map((o) => rs.filter((r) => r.outcome === o).length).join(" / ");

function table(caption, head, rows) {
  return el("div", {class: "summary"}, el("p", {class: "caption", text: caption}), el("div", {class: "scroll"},
    el("table", {},
      el("thead", {}, el("tr", {}, head.map((h, i) => el("th", {class: i ? "num" : null, text: h})))),
      el("tbody", {}, rows.map((row) => el("tr", {}, row.map((c, i) => el("td", {class: i ? "num" : "name", text: c}))))))));
}

function results() {
  const box = $("results");
  const ladder = [...group(RUNS.filter((r) => r.set === "ladder"), "model")]
    .sort(([, a], [, b]) => b.length - a.length || mean(b.map((r) => r.reward)) - mean(a.map((r) => r.reward))
      || mean(b.map((r) => r.score_share)) - mean(a.map((r) => r.score_share)))
    .map(([m, rs]) => [m, rs.length, wdl(rs), pct(mean(rs.map((r) => r.score_share))), money(mean(rs.map((r) => r.cost_usd)))]);
  box.append(table("Ladder: Human against the Orc AI, 30-minute games", ["Model", "Games", "Won / drawn / lost", "Score share", "Cost a game"], ladder));
  const drills = [...group(RUNS.filter((r) => r.set === "drills"), "model")]
    .map(([m, rs]) => [m, `${rs.filter((r) => r.passed).length} of ${rs.length}`, pct(mean(rs.map((r) => r.reward))), money(mean(rs.map((r) => r.cost_usd)))])
    .sort((a, b) => parseInt(b[1]) - parseInt(a[1]));
  box.append(table("Drills: one skill each, passed when every check is met", ["Model", "Passed", "Checks met", "Cost a drill"], drills));
  const duels = [...group(RUNS.filter((r) => r.set === "duels" || r.set === "references"), "model")]
    .sort(([, a], [, b]) => mean(b.map((r) => r.reward)) - mean(a.map((r) => r.reward)))
    .map(([m, rs]) => [m, rs.length, mean(rs.map((r) => r.reward)).toFixed(2), wdl(rs), money(mean(rs.map((r) => r.cost_usd)))]);
  box.append(table("Mirror duels, from player 0's side", ["Player 0", "Duels", "Points", "Won / drawn / lost", "Cost a duel"], duels));
}

function controls() {
  for (const [key, label] of SETS) {
    const n = key === "all" ? RUNS.length : RUNS.filter((r) => r.set === key).length;
    const tab = el("button", {role: "tab", "data-set": key}, `${label} `, el("span", {class: "dim", text: n}));
    tab.onclick = () => { state.set = key; render(); };
    $("sets").append(tab);
  }
  const models = [...new Set(RUNS.map((r) => r.model))];
  $("model").append(el("option", {value: "", text: "Every model"}), ...models.map((m) => el("option", {value: m, text: m})));
  $("outcome").append(el("option", {value: "", text: "Every result"}),
    ...[["win", "Won or passed"], ["draw", "Drawn"], ["loss", "Lost"]].map(([v, t]) => el("option", {value: v, text: t})));
  $("model").onchange = (e) => { state.model = e.target.value; render(); };
  $("outcome").onchange = (e) => { state.outcome = e.target.value; render(); };
  $("search").oninput = (e) => { state.search = e.target.value.toLowerCase(); render(); };
  for (const th of document.querySelectorAll("#runs th")) {
    th.onclick = () => {
      state.desc = state.sort === th.dataset.key ? !state.desc : th.classList.contains("num");
      state.sort = th.dataset.key;
      render();
    };
  }
}

function sortValue(run, key) {
  if (key === "result") return run.set === "drills" ? run.checks_met / run.num_checks : {win: 2, draw: 1, loss: 0}[run.outcome];
  if (key === "task") return `${run.set} ${title(run)}`;
  return run[key];
}

function render() {
  for (const tab of $("sets").children) tab.setAttribute("aria-selected", tab.dataset.set === state.set);
  for (const th of document.querySelectorAll("#runs th")) {
    th.classList.toggle("sorted", th.dataset.key === state.sort);
    th.classList.toggle("desc", th.dataset.key === state.sort && state.desc);
  }
  const shown = RUNS.filter((r) => (state.set === "all" || r.set === state.set) && (!state.model || r.model === state.model)
      && (!state.outcome || resultClass(r) === state.outcome)
      && (!state.search || `${r.task} ${title(r)}`.toLowerCase().includes(state.search)))
    .sort((a, b) => {
      const x = sortValue(a, state.sort), y = sortValue(b, state.sort);
      const order = x == null ? 1 : y == null ? -1 : x < y ? -1 : x > y ? 1 : 0;
      return state.desc ? -order : order;
    });
  $("count").textContent = `${shown.length} of ${RUNS.length} runs`;
  $("runs").tBodies[0].replaceChildren(...shown.map((r) => {
    const row = el("tr", {tabindex: "0"},
      el("td", {}, el("span", {class: `set ${r.set}`, text: SETS.find(([k]) => k === r.set)[1]}), " ", title(r)),
      el("td", {text: r.model}),
      el("td", {}, el("span", {class: `chip ${resultClass(r)}`, text: result(r)})),
      el("td", {class: "num", text: r.reward == null ? "–" : r.reward.toFixed(2)}),
      el("td", {class: "num", text: pct(r.score_share)}),
      el("td", {class: "num", text: minutes(r.game_seconds)}),
      el("td", {class: "num", text: r.turns ?? "–"}),
      el("td", {class: "num", text: money(r.cost_usd)}));
    const open = () => { location.hash = `run=${encodeURIComponent(r.id)}`; };
    row.onclick = open;
    row.onkeydown = (e) => { if (e.key === "Enter") open(); };
    return row;
  }));
}

// Replay

let loading = 0;

// A fresh frame per replay: changing an existing frame's srcdoc adds to the session history, so Back would step
// through replays instead of returning to the list.
function frame(html) {
  const next = el("iframe", {id: "r-frame", title: "Replay", sandbox: "allow-scripts"});
  if (html != null) next.srcdoc = html;
  $("r-frame").replaceWith(next);
}

async function play(run) {
  const mine = ++loading;
  document.title = `${run.model}: ${title(run)} · Warcraft III on AgentEnv`;
  $("r-title").textContent = `${run.model} · ${title(run)}`;
  $("r-chips").replaceChildren(...[
    el("span", {class: `chip ${resultClass(run)}`, text: result(run)}),
    el("span", {class: "chip", text: `reward ${run.reward == null ? "–" : run.reward.toFixed(2)}`}),
    el("span", {class: "chip", text: `${minutes(run.game_seconds)} game time`}),
    run.turns != null ? el("span", {class: "chip", text: `${run.turns} turns`}) : null,
    el("span", {class: "chip", text: money(run.cost_usd)})].filter(Boolean));
  const links = [["Video", run.video && fileUrl(run.video)], ["Transcript", run.transcript && blobUrl(run.transcript)],
                 ["Timeline JSON", run.timeline && blobUrl(run.timeline)], [".w3g replay", run.w3g && fileUrl(run.w3g)],
                 ["Replay file", run.replay && fileUrl(run.replay)]];
  $("r-links").replaceChildren(...links.filter(([, u]) => u).map(([t, u]) => el("a", {href: u, target: "_blank", rel: "noopener", text: t})));
  const facts = [`${race(run.race)} on ${run.map}`, `against ${run.opponent}`, `seed ${run.seed}`];
  if (run.orders != null) facts.push(`${run.orders} orders${run.orders_refused ? `, ${run.orders_refused} refused by the game` : ""}`);
  if (run.units_killed != null) facts.push(`${run.units_killed} enemy units killed`);
  const details = [el("span", {text: facts.join(" · ")})];
  if (run.checks) {
    details.push(el("ul", {class: "checks"}, run.checks.map((c) =>
      el("li", {class: c.met ? "met" : "missed"}, `${c.met ? "✓" : "✗"} ${c.check}`,
        c.measured != null ? el("span", {class: "dim", text: ` (measured ${c.measured})`}) : null))));
  }
  $("r-details").replaceChildren(...details);
  frame(null);
  $("r-status").textContent = "Loading the replay…";
  $("r-status").hidden = false;
  try {
    const response = await fetch(fileUrl(run.replay));
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    // The page names its video relative to itself, so it finds it in the dataset beside the page's own place.
    const base = `<base href="${fileUrl(run.replay.replace(/[^/]*$/, ""))}">`;
    const html = (await response.text()).replace("<head>", `<head>${base}`);
    if (mine !== loading) return;
    frame(html);
    $("r-status").hidden = true;
  } catch (error) {
    if (mine !== loading) return;
    $("r-status").replaceChildren(`The replay didn't load (${error.message}). `,
      el("a", {href: fileUrl(run.replay), target: "_blank", rel: "noopener", text: "Download it"}), " and open it in a browser.");
  }
}

function route() {
  const id = decodeURIComponent((location.hash.match(/^#run=(.+)$/) || [])[1] || "");
  const run = id && RUNS.find((r) => r.id === id);
  $("home").hidden = !!run;
  $("replay").hidden = !run;
  if (run) { window.scrollTo(0, 0); play(run); }
  else { loading++; frame(null); document.title = "Warcraft III on AgentEnv: every run, replayed"; }
}

async function main() {
  DATA = await (await fetch("runs.json")).json();
  RUNS = DATA.runs;
  RUNS.forEach((r, i) => { r.order = i; });   // the build's order: set, then task, then model
  BASE = new URLSearchParams(location.search).get("data")
    || `https://huggingface.co/datasets/${DATA.dataset}/resolve/${DATA.revision}/`;
  if (!BASE.endsWith("/")) BASE += "/";
  const datasetUrl = `https://huggingface.co/datasets/${DATA.dataset}`;
  for (const id of ["dataset-link", "nav-dataset"]) $(id).href = datasetUrl;
  for (const id of ["nav-plugin", "footer-plugin"]) $(id).href = `https://github.com/earakely-scale/agentenv-wc3-plugin/tree/${DATA.plugin}`;
  featured();
  results();
  controls();
  render();
  window.addEventListener("hashchange", route);
  route();
}

main();
