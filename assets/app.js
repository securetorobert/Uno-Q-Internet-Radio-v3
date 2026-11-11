// UNO Q Internet Radio v3 - web UI logic. SPDX-License-Identifier: MIT
// Everything from the radio (station names, song titles) is inserted with
// textContent, never innerHTML: stream metadata is untrusted text.
"use strict";

const $ = (id) => document.getElementById(id);
const state = { status: null, stations: [], currentId: null, history: [], clockSkew: 0, dragging: false,
                insightsDay: null, insights: null, aiCfg: null };

// ------------------------------------------------------------------ helpers
async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch (_) { /* empty body */ }
  if (!res.ok) throw new Error((data && data.detail) || `${res.status} ${res.statusText}`);
  return data;
}

let toastTimer;
function toast(msg, isErr = false) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.toggle("err", isErr);
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), 2600);
}

function el(tag, props = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) n.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children) if (c) n.append(c);
  return n;
}

const ICONS = {
  play: "M7 4v16l13-8z",
  edit: "M4 17.2V20h2.8l8.3-8.3-2.8-2.8zM19.7 7.3a1 1 0 0 0 0-1.4l-1.6-1.6a1 1 0 0 0-1.4 0l-1.3 1.3 2.8 2.8z",
  up: "M12 6l-6 6h4v6h4v-6h4z",
  down: "M12 18l6-6h-4V6h-4v6H6z",
  plus: "M11 5h2v6h6v2h-6v6h-2v-6H5v-2h6z",
};
function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
  p.setAttribute("d", ICONS[name]);
  svg.append(p);
  return svg;
}

function logoEl(url, cls = "logo") {
  const fb = el("div", { class: `${cls} logo-fallback`, "aria-hidden": "true", text: "\u266A" });
  if (!url) return fb;
  const img = el("img", { class: cls, alt: "", src: url, loading: "lazy", referrerpolicy: "no-referrer" });
  img.addEventListener("error", () => img.replaceWith(fb));
  return img;
}

function fmtDuration(s) {
  s = Math.max(0, Math.floor(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const mm = String(m).padStart(h ? 2 : 1, "0"), ss = String(sec).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}
function fmtTime(ts) {
  const d = new Date(ts * 1000), today = new Date();
  const t = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return d.toDateString() === today.toDateString() ? t : `${d.toLocaleDateString([], { day: "numeric", month: "short" })} ${t}`;
}
const radioNow = () => Date.now() / 1000 - state.clockSkew;

// ------------------------------------------------------------------ now playing
function renderStatus() {
  const s = state.status;
  if (!s) return;
  const st = s.station;
  $("np-chan").textContent = s.index > 0 ? String(s.index).padStart(2, "0") : "--";
  const stEl = $("np-state");
  const label = { idle: s.playing_wanted ? "stopped" : "paused", connecting: "tuning", playing: "on air", error: "no signal" }[s.state] || s.state;
  stEl.textContent = label;
  stEl.className = `state ${s.state}`;
  $("np-station").textContent = st ? st.name : "No station";
  const meta = [st && st.genre, st && st.country, s.count ? `${s.index} of ${s.count}` : ""].filter(Boolean);
  $("np-meta").textContent = meta.join(" · ");
  document.title = st ? `${s.title ? s.title + " — " : ""}${st.name} · UNO Q Radio` : "UNO Q Radio";

  const logo = $("np-logo"), fb = $("np-logo-fb");
  if (st && st.logo) {
    if (logo.getAttribute("src") !== st.logo) { logo.src = st.logo; }
    logo.onerror = () => { logo.hidden = true; fb.hidden = false; };
    logo.onload = () => { logo.hidden = false; fb.hidden = true; };
  } else { logo.hidden = true; fb.hidden = false; logo.removeAttribute("src"); }

  const title = $("np-title"), sub = $("np-title-sub"), artist = $("np-artist"), kind = $("np-kind");
  const song = s.song;   // v3: tidied {kind, artist, title, by}, or null while waiting
  artist.hidden = true; kind.hidden = true;
  if (s.title) {
    const shown = song && song.title ? song.title : s.title;
    title.textContent = shown;
    title.title = s.title;            // raw text on hover
    title.classList.remove("muted");
    if (song && song.artist) { artist.textContent = song.artist; artist.hidden = false; }
    if (song && (song.kind === "show" || song.kind === "ad")) { kind.textContent = song.kind; kind.hidden = false; }
    const by = song && song.by === "gemma" ? " · tidied by Gemma" : "";
    sub.textContent = (s.title_since ? `since ${fmtTime(s.title_since)}` : "") + by;
  } else {
    title.classList.add("muted");
    if (s.state === "playing") {
      title.textContent = s.stream.metadata ? "Waiting for song info…" : "Live — this stream doesn't send song titles";
    } else if (s.state === "connecting") title.textContent = "Tuning…";
    else if (s.state === "error") title.textContent = "Couldn't reach this station. Check its URL in Edit.";
    else title.textContent = "—";
    sub.textContent = "";
  }

  const tech = $("np-tech");
  tech.replaceChildren();
  const bits = [];
  if (s.state === "playing") {
    if (s.stream.codec) bits.push(s.stream.codec);
    if (s.stream.bitrate) bits.push(`${s.stream.bitrate} kbps`);
    if (s.stream.sample_rate) bits.push(`${(s.stream.sample_rate / 1000).toFixed(1)} kHz`);
    if (s.stream.icy_name && st && s.stream.icy_name !== st.name) bits.push(s.stream.icy_name);
  }
  if (s.audio_device) bits.push(`out: ${s.audio_device}`);
  for (const b of bits) tech.append(el("span", { text: b }));

  // SVG elements have no .hidden property, so toggle the attribute itself
  const showPause = s.playing_wanted && s.state !== "error";
  $("ico-play").toggleAttribute("hidden", showPause);
  $("ico-pause").toggleAttribute("hidden", !showPause);
  $("btn-toggle").setAttribute("aria-label", showPause ? "Pause" : "Play");
  const mute = $("btn-mute");
  mute.classList.toggle("on", s.muted);
  mute.setAttribute("aria-label", s.muted ? "Unmute" : "Mute");
  $("ico-vol").toggleAttribute("hidden", s.muted);
  $("ico-muted").toggleAttribute("hidden", !s.muted);
  if (!state.dragging) setSlider(s.volume);
  if (state.currentId !== (st && st.id)) { state.currentId = st && st.id; renderStations(); }
  tickElapsed();
}

function tickElapsed() {
  const s = state.status;
  $("np-elapsed").textContent = s && s.state === "playing" && s.started_at ? fmtDuration(radioNow() - s.started_at) : "";
}

function setSlider(v) {
  const r = $("vol");
  r.value = v;
  r.style.setProperty("--fill", `${v}%`);
  $("vol-out").textContent = v;
}

// ------------------------------------------------------------------ stations
function renderStations() {
  const list = $("station-list");
  list.replaceChildren();
  const enabled = state.stations.filter((s) => s.enabled);
  $("st-count").textContent = state.stations.length ? `(${state.stations.length})` : "";
  $("station-empty").hidden = state.stations.length > 0;
  state.stations.forEach((st, i) => {
    const num = st.enabled ? String(enabled.indexOf(st) + 1).padStart(2, "0") : "–";
    const meta = [st.genre, st.country, st.url ? "direct URL" : st.rb_search ? "directory lookup" : "",
                  st.play_count ? `played ${st.play_count}×` : ""].filter(Boolean).join(" · ");
    const li = el("li", { class: `st${st.id === state.currentId ? " current" : ""}${st.enabled ? "" : " disabled"}` },
      el("span", { class: "st-num", text: num }),
      logoEl(st.logo),
      el("div", { class: "st-main", title: "Play", onclick: () => playStation(st) },
        el("div", { class: "st-name", text: st.name }),
        el("div", { class: "st-meta", text: meta })),
      el("div", { class: "st-actions" },
        el("button", { class: "icon-btn", "aria-label": `Play ${st.name}`, onclick: () => playStation(st), disabled: !st.enabled }, icon("play")),
        el("button", { class: "icon-btn", "aria-label": "Move up", onclick: () => move(st, -1), disabled: i === 0 }, icon("up")),
        el("button", { class: "icon-btn", "aria-label": "Move down", onclick: () => move(st, 1), disabled: i === state.stations.length - 1 }, icon("down")),
        el("button", { class: "icon-btn", "aria-label": `Edit ${st.name}`, onclick: () => openEdit(st) }, icon("edit"))));
    list.append(li);
  });
}

async function playStation(st) {
  if (!st.enabled) return toast("Enable this station first (Edit)", true);
  try { await api("POST", "/api/play", { station_id: st.id }); } catch (e) { toast(e.message, true); }
}
async function move(st, direction) {
  try { await api("POST", `/api/stations/${st.id}/move`, { direction }); } catch (e) { toast(e.message, true); }
}

// ------------------------------------------------------------------ edit dialog
let editing = null;
function openEdit(st) {
  editing = st;
  const f = $("edit-form");
  for (const k of ["name", "url", "rb_search", "rb_countrycode", "genre", "country", "logo", "homepage"]) f.elements[k].value = st[k] || "";
  f.elements.enabled.checked = !!st.enabled;
  $("edit-resolved").textContent = st.resolved_url && st.resolved_url !== st.url ? `Last working stream: ${st.resolved_url}` : "";
  $("edit-dialog").showModal();
}
$("edit-form").addEventListener("submit", async (ev) => {
  if (ev.submitter && ev.submitter.value !== "save") return;
  ev.preventDefault();
  const f = ev.target, body = {};
  for (const k of ["name", "url", "rb_search", "rb_countrycode", "genre", "country", "logo", "homepage"]) body[k] = f.elements[k].value;
  body.enabled = f.elements.enabled.checked;
  if (!body.url && !body.rb_search) return toast("Give a stream URL or a directory search name", true);
  try { await api("PUT", `/api/stations/${editing.id}`, body); $("edit-dialog").close(); toast("Saved"); }
  catch (e) { toast(e.message, true); }
});
$("edit-delete").addEventListener("click", async () => {
  if (!editing || !confirm(`Delete “${editing.name}”?`)) return;
  try { await api("DELETE", `/api/stations/${editing.id}`); $("edit-dialog").close(); toast("Deleted"); }
  catch (e) { toast(e.message, true); }
});

// ------------------------------------------------------------------ add / search
$("search-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const q = $("search-q").value.trim(), cc = $("search-cc").value.trim();
  const out = $("search-results");
  out.replaceChildren(el("li", { class: "r-meta", text: "Searching…" }));
  try {
    const hits = await api("GET", `/api/search?q=${encodeURIComponent(q)}&country=${encodeURIComponent(cc)}`);
    out.replaceChildren();
    if (!hits.length) out.append(el("li", { class: "r-meta", text: "No playable stations found." }));
    for (const h of hits) {
      const meta = [h.codec, h.bitrate ? `${h.bitrate} kbps` : "", h.country, h.genre].filter(Boolean).join(" · ");
      out.append(el("li", {},
        logoEl(h.logo),
        el("div", {}, el("div", { class: "r-name", text: h.name }), el("div", { class: "r-meta", text: meta })),
        el("button", { class: "btn btn-sm", onclick: (e) => addFromSearch(h, e.currentTarget) }, icon("plus"), " Add")));
    }
  } catch (e) { out.replaceChildren(el("li", { class: "r-meta", text: e.message })); }
});

async function addFromSearch(h, btn) {
  btn.disabled = true;
  try {
    await api("POST", "/api/stations", {
      name: h.name, url: h.url, genre: h.genre, country: h.country, logo: h.logo, homepage: h.homepage,
      rb_search: h.name, rb_countrycode: h.country,
    });
    btn.textContent = "Added";
    toast(`Added ${h.name}`);
  } catch (e) { btn.disabled = false; toast(e.message, true); }
}

$("add-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target, body = Object.fromEntries(new FormData(f).entries());
  if (!body.url && !body.rb_search) return toast("Give a stream URL or a directory search name", true);
  try { await api("POST", "/api/stations", body); f.reset(); toast(`Added ${body.name}`); }
  catch (e) { toast(e.message, true); }
});

// ------------------------------------------------------------------ history
function renderHistory() {
  const list = $("history-list");
  list.replaceChildren();
  $("history-empty").hidden = state.history.length > 0;
  for (const h of state.history) {
    const titleEl = el("div", { class: "h-title" });
    if (h.song) {
      if (h.artist) titleEl.append(el("span", { class: "h-artist", text: `${h.artist} – ` }));
      titleEl.append(document.createTextNode(h.song));
      if (h.kind === "show" || h.kind === "ad") titleEl.append(el("span", { class: "h-kind", text: h.kind }));
    } else titleEl.textContent = h.title;
    const changed = h.song && h.song !== h.title && `${h.artist} - ${h.song}` !== h.title;
    list.append(el("li", {},
      titleEl,
      el("div", { class: "h-time", text: fmtTime(h.played_at) }),
      el("div", { class: "h-station", text: h.station_name + (h.cleaned_by === "gemma" ? " · tidied by Gemma" : "") }),
      changed ? el("div", { class: "h-raw", text: `raw: ${h.title}` }) : null));
  }
}
$("btn-clear-history").addEventListener("click", async () => {
  if (!confirm("Clear the song history?")) return;
  try { await api("DELETE", "/api/history"); } catch (e) { toast(e.message, true); }
});

// ------------------------------------------------------------------ controls
$("btn-toggle").addEventListener("click", () => api("POST", "/api/control", { action: "toggle" }).catch((e) => toast(e.message, true)));
$("btn-next").addEventListener("click", () => api("POST", "/api/control", { action: "next" }).catch((e) => toast(e.message, true)));
$("btn-prev").addEventListener("click", () => api("POST", "/api/control", { action: "prev" }).catch((e) => toast(e.message, true)));
$("btn-mute").addEventListener("click", () => api("POST", "/api/control", { action: "mute" }).catch((e) => toast(e.message, true)));

let volTimer = null;
const vol = $("vol");
vol.addEventListener("pointerdown", () => (state.dragging = true));
vol.addEventListener("input", () => {
  setSlider(vol.value);
  clearTimeout(volTimer);   // throttle: at most one update every 120 ms while dragging
  volTimer = setTimeout(() => sendVolume(vol.value), 120);
});
vol.addEventListener("change", () => { state.dragging = false; clearTimeout(volTimer); sendVolume(vol.value); });
function sendVolume(v) {
  if (socket && socket.connected) socket.emit("volume", { volume: Number(v) });
  else api("POST", "/api/volume", { volume: Number(v) }).catch((e) => toast(e.message, true));
}

// keyboard: space = play/pause, arrows = station / volume
document.addEventListener("keydown", (e) => {
  if (e.target.closest("input, textarea, dialog")) return;
  if (e.key === " ") { e.preventDefault(); $("btn-toggle").click(); }
  else if (e.key === "ArrowRight") $("btn-next").click();
  else if (e.key === "ArrowLeft") $("btn-prev").click();
  else if (e.key === "ArrowUp" || e.key === "ArrowDown") {
    e.preventDefault();
    const v = Math.max(0, Math.min(100, Number(vol.value) + (e.key === "ArrowUp" ? 5 : -5)));
    setSlider(v); sendVolume(v);
  }
});

// ------------------------------------------------------------------ tabs
document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach((x) => x.setAttribute("aria-selected", String(x === t)));
  document.querySelectorAll(".panel").forEach((p) => (p.hidden = p.id !== `tab-${t.dataset.tab}`));
  if (t.dataset.tab === "insights") loadInsights(state.insightsDay);
  try { localStorage.setItem("radio-tab", t.dataset.tab); } catch (_) {}
}));
try {
  const saved = localStorage.getItem("radio-tab");
  if (saved) document.querySelector(`.tab[data-tab="${saved}"]`)?.click();
} catch (_) {}

// ------------------------------------------------------------------ v3: ask the radio
async function ask(text) {
  text = text.trim();
  if (!text) return;
  const form = $("ask-form"), reply = $("ask-reply"), rt = $("ask-reply-text"), src = $("ask-src");
  form.classList.add("thinking"); $("ask-btn").disabled = true;
  reply.hidden = false; reply.classList.remove("err");
  rt.replaceChildren(el("span", { class: "spinner", "aria-hidden": "true" }), "Thinking…");
  src.textContent = "";
  const slow = setTimeout(() => { rt.lastChild.textContent = "Asking Gemma… (a few seconds on the UNO Q)"; }, 700);
  try {
    const r = await api("POST", "/api/ask", { text });
    rt.textContent = r.reply;
    reply.classList.toggle("err", !r.ok);
    src.textContent = r.source === "gemma" ? `Gemma · ${(r.ms / 1000).toFixed(1)} s` : "instant";
    if (r.ok) $("ask-input").value = "";
  } catch (e) {
    rt.textContent = e.message; reply.classList.add("err");
  } finally {
    clearTimeout(slow);
    form.classList.remove("thinking"); $("ask-btn").disabled = false;
  }
}
$("ask-form").addEventListener("submit", (e) => { e.preventDefault(); ask($("ask-input").value); });
document.querySelectorAll("#ask-chips .chip").forEach((c) => c.addEventListener("click", () => ask(c.textContent)));

// ------------------------------------------------------------------ v3: Gemma status chip
function renderAi(ai) {
  if (!ai) return;
  const chip = $("ai-chip");
  const label = { ready: "Gemma ready", busy: "Gemma thinking", starting: "Gemma loading",
                  unavailable: "Gemma unavailable", off: "Gemma off" }[ai.state] || "Gemma";
  chip.className = `ai-chip ${ai.state}`;
  $("ai-text").textContent = label;
  chip.title = ai.detail ? `${label}: ${ai.detail}` : label;
  const st = $("ai-status");
  if (st) {
    const last = ai.last_ms ? ` Last answer took ${(ai.last_ms / 1000).toFixed(1)} s.` : "";
    st.textContent = `${ai.model}: ${label.replace("Gemma ", "")}.${last}` + (ai.queue ? ` ${ai.queue} waiting.` : "") +
      (ai.detail && ai.state !== "ready" ? ` ${ai.detail}` : "");
  }
}
$("ai-chip").addEventListener("click", () => {
  document.querySelector('.tab[data-tab="insights"]').click();
  setTimeout(() => $("ai-settings").scrollIntoView({ behavior: "smooth" }), 50);
});

// ------------------------------------------------------------------ v3: insights
const PART_LABELS = { morning: "Morning 5–12", afternoon: "Afternoon 12–17", evening: "Evening 17–22", night: "Night 22–5" };
function fmtMin(m) {
  m = Math.round(m);
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60), r = m % 60;
  return r ? `${h} h ${r} min` : `${h} h`;
}
function dayLabel(day, i) {
  if (i === 0) return "Today";
  if (i === 1) return "Yesterday";
  return new Date(`${day}T12:00:00`).toLocaleDateString([], { weekday: "short", day: "numeric" });
}

async function loadInsights(day) {
  try {
    const q = day ? `?day=${encodeURIComponent(day)}` : "";
    const [data, cfg] = await Promise.all([api("GET", `/api/insights${q}`), api("GET", "/api/ai")]);
    state.days = data.days;
    renderInsights(data);
    renderAiForm(cfg);
  } catch (e) { toast(e.message, true); }
}

function bars(listEl, rows) {
  listEl.replaceChildren();
  const max = Math.max(1, ...rows.map((r) => r.value));
  for (const r of rows) {
    const track = el("div", { class: "bar-track" });
    if (r.value > 0) track.append(el("div", { class: "bar-fill", style: `width:${(100 * r.value / max).toFixed(1)}%` }));
    listEl.append(el("li", { class: "bar", title: `${r.name}: ${fmtMin(r.value)}` },
      el("span", { class: "bar-name", text: r.name }), track, el("span", { class: "bar-value", text: fmtMin(r.value) })));
  }
}

function renderInsights(data) {
  state.insights = data;
  state.insightsDay = data.day;
  const days = $("days");
  days.replaceChildren();
  (state.days || []).forEach((d, i) => {
    days.append(el("button", { class: "day", type: "button", "aria-pressed": String(d.day === data.day),
                               onclick: () => loadInsights(d.day) },
      el("div", { class: "day-name", text: dayLabel(d.day, i) }),
      el("div", { class: "day-min", text: d.minutes ? fmtMin(d.minutes) : "–" })));
  });
  const f = data.facts;
  $("t-total").textContent = f.total_minutes ? fmtMin(f.total_minutes) : "0 min";
  $("t-stations").textContent = f.stations.length;
  $("t-songs").textContent = f.songs;
  $("t-busiest").textContent = f.busiest_part || "–";
  bars($("bars-stations"), f.stations.map((s) => ({ name: s.name, value: s.minutes })));
  bars($("bars-parts"), Object.entries(f.parts).map(([k, v]) => ({ name: PART_LABELS[k] || k, value: v })));
  const arts = $("artists");
  arts.replaceChildren();
  for (const a of f.top_artists) arts.append(el("li", {}, a.name, el("span", { text: `${a.plays}×` })));
  $("artists-empty").hidden = f.top_artists.length > 0;

  const txt = $("summary-text"), meta = $("summary-meta"), btn = $("btn-summary");
  if (data.pending) {
    txt.replaceChildren(el("span", { class: "spinner", "aria-hidden": "true" }), "Gemma is writing the summary…");
    txt.classList.add("muted"); meta.textContent = ""; btn.disabled = true;
  } else if (data.summary) {
    txt.textContent = data.summary.text; txt.classList.remove("muted");
    const who = data.summary.written_by === "gemma"
      ? `Written by Gemma 3 1B in ${(data.summary.ms / 1000).toFixed(1)} s from the numbers on this page`
      : "Plain summary (Gemma was off or unavailable)";
    meta.textContent = `${who} · ${fmtTime(data.summary.created_at)}`;
    btn.textContent = "Rewrite"; btn.disabled = false;
  } else {
    txt.textContent = f.total_minutes ? "No summary yet." : "Nothing played on this day yet.";
    txt.classList.add("muted"); meta.textContent = "";
    btn.textContent = "Write summary"; btn.disabled = !f.total_minutes;
  }
}
$("btn-summary").addEventListener("click", async () => {
  try {
    await api("POST", "/api/insights/summary", { day: state.insightsDay });
    loadInsights(state.insightsDay);
  } catch (e) { toast(e.message, true); }
});

function renderAiForm(cfg) {
  state.aiCfg = cfg;
  const f = $("ai-form");
  for (const k of ["ai_enabled", "ai_commands", "ai_titles", "ai_summary_auto"]) f.elements[k].checked = !!cfg[k];
  f.elements.ai_summary_time.value = cfg.ai_summary_time || "21:00";
  for (const k of ["ai_commands", "ai_titles", "ai_summary_auto"]) f.elements[k].disabled = !cfg.ai_enabled;
  renderAi(cfg.status);
}
$("ai-form").addEventListener("change", async (e) => {
  const f = $("ai-form"), body = {};
  for (const k of ["ai_enabled", "ai_commands", "ai_titles", "ai_summary_auto"]) body[k] = f.elements[k].checked;
  if (f.elements.ai_summary_time.value) body.ai_summary_time = f.elements.ai_summary_time.value;
  try { renderAiForm(await api("POST", "/api/ai", body)); toast("Saved"); } catch (err) { toast(err.message, true); }
});

// ------------------------------------------------------------------ live updates
function onStatus(s) {
  state.clockSkew = Date.now() / 1000 - s.server_time;
  state.status = s;
  renderStatus();
  renderAi(s.ai);
}
function onStations(p) { state.stations = p.stations; state.currentId = p.current_id; renderStations(); }
function onHistory(h) { state.history = h; renderHistory(); }

function setLink(cls, text) { $("link").className = `link ${cls}`; $("link-text").textContent = text; }

let socket = null;
let pollTimer = null;
async function poll() {
  try {
    const [s, st, h] = await Promise.all([api("GET", "/api/status"), api("GET", "/api/stations"), api("GET", "/api/history")]);
    onStatus(s); onStations(st); onHistory(h);
    if (!socket || !socket.connected) setLink("on", "polling");
  } catch (_) { setLink("off", "offline"); }
}

if (typeof io === "function") {
  socket = io({ transports: ["websocket", "polling"] });
  socket.on("connect", () => { setLink("on", "live"); clearInterval(pollTimer); pollTimer = null; });
  socket.on("disconnect", () => { setLink("off", "reconnecting"); if (!pollTimer) pollTimer = setInterval(poll, 3000); });
  socket.on("status", onStatus);
  socket.on("stations", onStations);
  socket.on("history", onHistory);
  socket.on("insights", (d) => { if (!$("tab-insights").hidden && d.day === state.insightsDay) loadInsights(d.day); });
} else {
  pollTimer = setInterval(poll, 3000);
}
poll();
setInterval(tickElapsed, 1000);
