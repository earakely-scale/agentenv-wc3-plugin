"use strict";
// The RTS spectator: the map and every unit in view, step by step, live from data.json or from the data a
// recording embeds (window.RTS_DATA). Game-agnostic: it draws timeline.py's static map and frames. When the env
// captures the game's own picture (live.client), that is the main view and the map becomes the minimap beside it.
// Beside them: what the players say they are planning (notes), the feed, each player's card and the momentum graph.
// ?view is the game alone, full-frame, as a broadcast frames it (the env's spectator view); ?panel is the sidebar
// alone, on a transparent page, for a broadcast's page widget.
const $ = id => document.getElementById(id);
const QUERY = new URLSearchParams(location.search);
const VIEW = QUERY.has("view"), PANEL = QUERY.has("panel");
const TERRAIN = ["#2f4a2a", "#46452f", "#1f4466", "#121418"];
const NEUTRAL = "#b8b8b8";
const S = {static: null, frames: [], idx: -1, follow: true, playing: false, terrain: null, scale: 1, client: false,
  video: null, waiting: []};
const BASE = location.pathname.replace(/\/$/, "");

for (const [on, mode] of [[VIEW, "view"], [PANEL, "panel"]])
  if (on) { document.body.classList.add(mode); document.documentElement.classList.add(mode); }

function clock(t) {
  if (t == null) return "0:00";
  const s = Math.max(0, Math.floor(t)), h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60, sec = s % 60;
  return (h ? `${h}:${String(m).padStart(2, "0")}` : `${m}`) + ":" + String(sec).padStart(2, "0");
}
const esc = s => String(s).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const players = () => (S.static && S.static.players) || [];
const colorOf = owner => (players().find(p => p.slot === owner) || {}).color || NEUTRAL;
const labelOf = slot => (players().find(p => p.slot === slot) || {}).label || `player ${slot}`;
const textOf = e => typeof e === "string" ? e : e.text;
const majorOf = e => typeof e === "object" && e.major;
const statsOf = (frame, slot) => ((frame && frame.players) || {})[slot] || ((frame && frame.players) || {})[String(slot)] || {};
const num = n => n == null ? "–" : n >= 10000 ? `${(n / 1000).toFixed(1)}k` : String(n);

function terrainCanvas(t) {
  if (!t || !t.codes) return null;
  const bytes = Uint8Array.from(atob(t.codes), c => c.charCodeAt(0));
  const c = document.createElement("canvas");
  c.width = t.width; c.height = t.height;
  const ctx = c.getContext("2d"), img = ctx.createImageData(t.width, t.height);
  const rgb = TERRAIN.map(h => [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16)));
  for (let j = 0; j < t.height; j++) for (let i = 0; i < t.width; i++) {
    const code = rgb[Math.min(3, bytes[j * t.width + i] || 0)], p = ((t.height - 1 - j) * t.width + i) * 4;
    img.data[p] = code[0]; img.data[p + 1] = code[1]; img.data[p + 2] = code[2]; img.data[p + 3] = 255;
  }
  ctx.putImageData(img, 0, 0);
  return c;
}

function setStatic(st) {
  S.static = st;
  S.terrain = terrainCanvas(st.terrain);
  const b = st.bounds, w = b.max_x - b.min_x, h = b.max_y - b.min_y, side = S.client ? (VIEW ? 300 : 320)
    : VIEW ? 1600 : 900;
  S.scale = side / Math.max(w, h);
  const canvas = $("map");
  canvas.width = Math.round(w * S.scale); canvas.height = Math.round(h * S.scale);
  const title = QUERY.get("title") || st.title || "Spectator";
  $("title").textContent = title;
  document.title = title;
  $("sub").textContent = [st.map, players().map(p => `${p.label} (${p.race})`).join(" vs ")].filter(Boolean).join(" · ");
  $("limit").textContent = st.time_limit ? ` / ${clock(st.time_limit)}` : "";
}

function toPx(x, y) {
  const b = S.static.bounds;
  return [(x - b.min_x) * S.scale, (b.max_y - y) * S.scale];
}

function draw() {
  const st = S.static, frame = S.frames[S.idx];
  if (!st) return;
  const canvas = $("map"), ctx = canvas.getContext("2d");
  ctx.fillStyle = "#0b0d10"; ctx.fillRect(0, 0, canvas.width, canvas.height);
  if (S.terrain) {
    const t = st.terrain, [x0, y1] = toPx(t.origin[0], t.origin[1] + t.height * t.cell);
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(S.terrain, x0, y1, t.width * t.cell * S.scale, t.height * t.cell * S.scale);
  }
  ctx.fillStyle = "rgba(10, 40, 18, .8)";
  for (const [x, y] of st.trees || []) { const [px, py] = toPx(x, y); ctx.fillRect(px - 1, py - 1, 2, 2); }
  for (const p of st.points || []) {
    const [px, py] = toPx(p.x, p.y);
    if (p.kind === "gold") { ctx.fillStyle = "#f2c84b"; ctx.beginPath(); ctx.moveTo(px, py - 6); ctx.lineTo(px + 6, py);
      ctx.lineTo(px, py + 6); ctx.lineTo(px - 6, py); ctx.fill(); }
    else if (p.kind === "start") { ctx.strokeStyle = "rgba(255,255,255,.55)"; ctx.lineWidth = 1.5; ctx.beginPath();
      ctx.arc(px, py, 14, 0, 2 * Math.PI); ctx.stroke(); }
    else if (p.kind === "camp") { ctx.fillStyle = "rgba(230,90,70,.7)"; ctx.fillRect(px - 2.5, py - 2.5, 5, 5); }
    else if (p.kind === "shop") { ctx.fillStyle = "#b07ce8"; ctx.fillRect(px - 4, py - 4, 8, 8); }
  }
  const small = S.client ? 0.7 : 1;
  for (const [, owner, , x, y, hp, kind] of (frame && frame.units) || []) {
    const [px, py] = toPx(x, y), color = colorOf(owner);
    ctx.globalAlpha = 0.45 + 0.55 * Math.max(0, Math.min(100, hp)) / 100;
    ctx.fillStyle = color; ctx.strokeStyle = "rgba(0,0,0,.7)"; ctx.lineWidth = 1;
    if (kind === 1) { const r = 6 * small; ctx.fillRect(px - r, py - r, 2 * r, 2 * r); ctx.strokeRect(px - r, py - r, 2 * r, 2 * r); }
    else { const r = (kind === 2 ? 6.5 : kind === 3 ? 3 : 4.5) * small; ctx.beginPath(); ctx.arc(px, py, r, 0, 2 * Math.PI); ctx.fill();
      if (kind === 2) { ctx.strokeStyle = "#fff"; ctx.lineWidth = 2; } ctx.stroke(); }
  }
  ctx.globalAlpha = 1;
  sidebar(frame);
  momentum();
  syncVideo(frame);
}

// What each player last said it was planning, as of the frame shown.
function plans() {
  const latest = new Map();
  for (let i = S.idx; i >= 0 && latest.size < players().length; i--)
    for (const n of (S.frames[i].notes || []).slice().reverse())
      if (n.kind === "plan" && !latest.has(n.slot)) latest.set(n.slot, [S.frames[i].t, n.text]);
  return [...latest.entries()].sort((a, b) => a[0] - b[0]);
}

function sidebar(frame) {
  $("clock").textContent = clock(frame && frame.t);
  const result = frame && frame.result, live = !S.embedded && !result, waiting = live && S.waiting.length;
  $("badge").className = "badge " + (result ? "over" : waiting ? "waiting" : live ? "live" : "");
  $("badge").textContent = result ? result.replace(/_/g, " ").toUpperCase() : waiting ? "WAITING" : live ? "● LIVE"
    : "REPLAY";
  const said = plans();
  $("plans").innerHTML = said.map(([slot, [t, text]]) => `<div class="plan" style="--c:${colorOf(slot)}">
    <div class="who">${esc(labelOf(slot))}<time>${clock(t)}</time></div><p>${esc(text)}</p></div>`).join("");
  $("planhead").hidden = !said.length;
  $("players").innerHTML = players().map(p => {
    const s = statsOf(frame, p.slot), food = s.food ? `${s.food[0]}/${s.food[1]}` : "–", a = s.agent;
    const minutes = Math.max(1, (frame && frame.t) || 0) / 60;
    const agent = a ? `<div class="agent">${a.cost_usd != null ? `$${a.cost_usd.toFixed(2)} so far` : ""}${
      a.decisions != null ? ` · ${Math.round(a.decisions / minutes)} decisions/min` : ""}</div>` : "";
    return `<div class="player" style="--c:${p.color}"><div class="name">${esc(p.label)}</div>
      <div class="meta">${esc(p.race || "")} · ${esc(p.controller || "")}</div>
      <div class="stats"><div><span>gold</span>${num(s.gold)}</div><div><span>lumber</span>${num(s.lumber)}</div>
      <div><span>food</span>${food}</div><div><span>army</span>${num(s.army)}</div>
      <div><span>buildings</span>${s.structures ?? "–"}</div><div><span>score</span>${num(s.score)}</div></div>${agent}</div>`;
  }).join("");
  const lines = [];
  for (let i = S.idx; i >= 0 && lines.length < (PANEL ? 10 : 40); i--)
    for (const e of (S.frames[i].events || []).slice().reverse())
      if (lines.length < (PANEL ? 10 : 40) && (!PANEL || majorOf(e) || typeof e === "string")) lines.push([S.frames[i].t, e]);
  $("events").innerHTML = lines.map(([t, e]) => {
    const side = typeof e === "object" && e.side != null ? colorOf(e.side) : "transparent";
    return `<li class="${majorOf(e) ? "major" : ""}" style="--c:${side}"><time>${clock(t)}</time>${esc(textOf(e))}</li>`;
  }).join("") || '<li class="dim">Nothing yet.</li>';
  $("scrub").max = Math.max(0, S.frames.length - 1); $("scrub").value = Math.max(0, S.idx);
  $("pos").textContent = `${S.idx + 1} / ${S.frames.length} steps`;
}

// Each player's score over the game, its army's value dashed, and who is ahead now.
function momentum() {
  const canvas = $("momentum"), ctx = canvas.getContext("2d"), ps = players(), frames = S.frames.slice(0, S.idx + 1);
  const w = canvas.width = canvas.clientWidth * devicePixelRatio, h = canvas.height = canvas.clientHeight * devicePixelRatio;
  ctx.clearRect(0, 0, w, h);
  if (frames.length < 2 || !ps.length || !w) { $("ahead").innerHTML = ""; $("aheadtext").textContent = ""; return; }
  const t0 = frames[0].t, t1 = Math.max(frames.at(-1).t, t0 + 1), step = Math.max(1, Math.floor(frames.length / w));
  for (const [key, dash] of [["army", [4, 4]], ["score", []]]) {
    const top = Math.max(1, ...frames.flatMap(f => ps.map(p => statsOf(f, p.slot)[key] || 0)));
    for (const p of ps) {
      ctx.strokeStyle = p.color; ctx.lineWidth = 2 * devicePixelRatio;
      ctx.setLineDash(dash.map(d => d * devicePixelRatio)); ctx.globalAlpha = dash.length ? 0.55 : 1;
      ctx.beginPath();
      for (let i = 0; i < frames.length; i += step) {
        const f = frames[i], x = (f.t - t0) / (t1 - t0) * w, y = h - 4 - (statsOf(f, p.slot)[key] || 0) / top * (h - 8);
        i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
      }
      ctx.stroke();
    }
  }
  ctx.setLineDash([]); ctx.globalAlpha = 1;
  const now = ps.map(p => [p, statsOf(frames.at(-1), p.slot).score || 0]), total = now.reduce((a, [, s]) => a + s, 0) || 1;
  $("ahead").innerHTML = now.map(([p, s]) => `<span style="flex:${Math.max(s, 1)};background:${p.color}" title="${esc(p.label)}"></span>`).join("");
  const lead = now.slice().sort((a, b) => b[1] - a[1]);
  $("aheadtext").textContent = lead.length > 1 && lead[0][1] > lead[1][1]
    ? `${lead[0][0].label} ahead by ${Math.round((lead[0][1] - lead[1][1]) / total * 100)}% of the score` : "Level on score";
}

// A recording with the game's video beside it keeps the two in step by each frame's "w": while it plays, the video
// is the clock and the frames follow it (tick), so a game with few frames still plays smoothly; a frame picked by
// hand moves the video to it.
function syncVideo(frame) {
  const v = S.video;
  if (!v || !frame || frame.w == null || !isFinite(v.duration)) return;
  const next = S.frames[S.idx + 1];
  if (v.currentTime < frame.w - 0.25 || (next && next.w != null && v.currentTime >= next.w + 0.25))
    v.currentTime = Math.min(frame.w, v.duration - 0.05);
  if (S.playing && v.paused) v.play().catch(() => {});
  if (!S.playing && !v.paused) v.pause();
}

// The game's picture becomes the main view, and the start notice moves from the map onto it: the map is only the
// minimap now.
function onClient() {
  S.client = true;
  document.body.classList.add("has-client");
  $("client").parentElement.append($("waiting"));
}

// Before the game begins: who it waits for (the game starts once every player has made its first move).
function waitingFor() {
  const names = S.waiting.map(labelOf), el = $("waiting");
  el.hidden = !names.length;
  if (names.length) {
    const who = names.length > 1 ? `${names.slice(0, -1).join(", ")} and ${names.at(-1)}` : names[0];
    el.innerHTML = `<b>Waiting for ${esc(who)}</b><span>The game starts once every player has made its first move</span>`;
  }
}

function showClient() {
  onClient();
  $("client").src = `${BASE}/client`;
  if (S.static) setStatic(S.static);
}

function show(idx) { S.idx = Math.max(0, Math.min(S.frames.length - 1, idx)); draw(); }

async function poll() {
  try {
    const since = S.frames.length ? S.frames[S.frames.length - 1].t : -1;
    const r = await fetch(`${BASE}/data.json?since=${since}`, {cache: "no-store"});
    const doc = await r.json();
    if (doc.live && doc.live.client && !S.client) showClient();
    S.waiting = (doc.live && doc.live.waiting) || [];
    if (!S.static || (doc.static && doc.static.game !== S.static.game)) { S.frames = []; setStatic(doc.static); }
    else if (doc.static) S.static.players = doc.static.players;   // names a player gave itself
    S.frames.push(...doc.frames);
    if (S.follow || VIEW || PANEL) show(S.frames.length - 1);
    waitingFor();
    if (S.idx >= 0) sidebar(S.frames[S.idx]);
  } catch (e) { /* the env is restarting or gone: try again */ }
  setTimeout(poll, 1000);
}

// Replays step a frame per 100 ms; beside the game's video, to the frame the video has reached.
function tick() {
  const v = S.video;
  if (S.playing && v && isFinite(v.duration)) {
    let i = S.idx;
    while (i < S.frames.length - 1 && S.frames[i + 1].w != null && S.frames[i + 1].w <= v.currentTime) i++;
    if (i !== S.idx) show(i);
    if (v.ended) S.playing = false;
  } else if (S.playing && S.idx < S.frames.length - 1) show(S.idx + 1); else S.playing = false;
  $("play").textContent = S.playing ? "❚❚" : "▶";
  setTimeout(tick, 100);
}

$("scrub").addEventListener("input", e => { S.follow = false; $("follow").checked = false; show(+e.target.value); });
$("follow").addEventListener("change", e => { S.follow = e.target.checked; if (S.follow) show(S.frames.length - 1); });
$("play").addEventListener("click", () => { S.playing = !S.playing; if (S.playing && S.idx >= S.frames.length - 1) show(0);
  S.follow = false; $("follow").checked = false; syncVideo(S.frames[S.idx]); });
document.addEventListener("keydown", e => { if (e.key === " ") { e.preventDefault(); $("play").click(); }
  if (e.key === "ArrowRight") show(S.idx + 1); if (e.key === "ArrowLeft") show(S.idx - 1); });

if (window.RTS_DATA) {
  S.embedded = true; S.follow = false; $("follow").parentElement.style.display = "none";
  if (window.RTS_DATA.video) {   // the game's own video, a file beside this page
    const v = document.createElement("video");
    v.id = "client"; v.src = window.RTS_DATA.video; v.muted = true; v.playsInline = true; v.preload = "auto";
    v.onloadedmetadata = () => { S.video = v; onClient(); setStatic(S.static); show(S.idx); };
    $("client").replaceWith(v);
  }
  setStatic(window.RTS_DATA.static); S.frames = window.RTS_DATA.frames || []; show(0); S.playing = true; tick();
} else { poll(); tick(); }
