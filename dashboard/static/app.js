"use strict";

const $ = (id) => document.getElementById(id);
const S = {
  jobs: [],
  jobsKey: "",
  selected: null,
  job: null,
  analysis: null,
  analysisFor: null,
  activeScene: -1,
  activeSeg: -1,
};
const ACTIVE = new Set(["queued", "running"]);
const STATE_LABEL = { queued: "Queued", running: "Running", done: "Done", failed: "Failed", cancelled: "Cancelled", interrupted: "Interrupted" };
const STAGE_TEXT = { pending: "Waiting", running: "Working…", done: "Done", skipped: "Skipped", failed: "Failed", cancelled: "Cancelled" };
const STAGE_ICON = { done: "✓", skipped: "–", failed: "✕", cancelled: "■" };

// ---------- helpers ----------

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function clock(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}
const compact = (n) => (n == null ? "–" : Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 }).format(n));
function bytes(n) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i >= 3 ? 1 : 0)} ${units[i]}`;
}
function elapsed(t0, t1) {
  if (!t0) return "";
  const s = Math.max(0, Math.round((t1 || Date.now() / 1000) - t0));
  return s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s` : `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}
const fileUrl = (id, rel) => `/files/${id}/${rel.split("/").map(encodeURIComponent).join("/")}`;

async function api(path, opts = {}) {
  const res = await fetch(path, { cache: "no-store", ...opts });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.error || `${res.status} ${res.statusText}`), { status: res.status });
  return data;
}
const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });

function flash(btn, text) {
  const old = btn.textContent;
  btn.textContent = text;
  setTimeout(() => { btn.textContent = old; }, 1600);
}

// ---------- system meters ----------

function setMeter(id, pct, text) {
  const el = $(id), bar = el.querySelector("i");
  bar.style.width = `${Math.min(100, pct)}%`;
  bar.className = pct > 85 ? "hot" : "";
  el.querySelector("b").textContent = text;
}

async function tickSystem() {
  try {
    const s = await api("/api/system");
    setMeter("m-cpu", s.cpu, `${Math.round(s.cpu)}%`);
    setMeter("m-ram", s.ram_percent, `${bytes(s.ram_used)} / ${bytes(s.ram_total)}`);
    $("m-disk").querySelector("b").textContent = bytes(s.disk_free);
    $("tools").innerHTML = Object.entries(s.tools)
      .map(([name, ok]) => `<span class="tool ${ok ? "" : "bad"}" title="${esc(name)} ${ok ? "installed" : "missing"}">${esc(name)}</span>`)
      .join("");
    $("live").classList.remove("off");
  } catch {
    $("live").classList.add("off");
  }
  setTimeout(tickSystem, 2000);
}

// ---------- job list ----------

function renderJobs() {
  const key = JSON.stringify([S.jobs, S.selected]);
  if (key === S.jobsKey) return;
  S.jobsKey = key;
  $("job-count").textContent = S.jobs.length ? `${S.jobs.length} video${S.jobs.length > 1 ? "s" : ""}` : "";
  if (!S.jobs.length) {
    $("jobs").innerHTML = `<li class="none">No videos yet. Paste a link above.</li>`;
    return;
  }
  $("jobs").innerHTML = S.jobs.map((j) => `
    <li><button class="job ${j.id === S.selected ? "sel" : ""}" data-id="${j.id}">
      ${j.thumbnail ? `<img src="${fileUrl(j.id, j.thumbnail)}" alt="">` : `<span class="ph"></span>`}
      <span class="jt">
        <b>${esc(j.title || j.url)}</b>
        <small><span class="badge ${j.state}">${STATE_LABEL[j.state] || j.state}</span>${esc(j.state === "running" ? j.stage_label || "" : j.channel || "")}</small>
        ${ACTIVE.has(j.state) ? `<span class="mini"><i style="width:${j.progress}%"></i></span>` : ""}
      </span>
    </button></li>`).join("");
}

function select(id) {
  if (S.selected === id) return;
  S.selected = id;
  S.job = null;
  history.replaceState(null, "", `#${id}`);
  resetResults();
  renderJobs();
  refreshDetail();
}

// ---------- job detail ----------

async function refreshDetail() {
  const id = S.selected;
  if (!id) return;
  try {
    const job = await api(`/api/jobs/${id}`);
    if (S.selected !== id) return;
    S.job = job;
    renderDetail(job);
    if (job.state === "done" && S.analysisFor !== id) await loadAnalysis(id);
    if (job.state !== "done" && S.analysisFor === id) resetResults();
  } catch (err) {
    if (err.status === 404) {
      S.selected = null;
      $("detail").hidden = true;
      $("empty").hidden = false;
    }
  }
}

function renderDetail(job) {
  $("empty").hidden = true;
  $("detail").hidden = false;
  const m = job.meta || {};
  const v = m.video || {};
  $("d-title").textContent = m.title || "Fetching video details…";
  const thumb = $("d-thumb");
  if (m.thumbnail) {
    const src = fileUrl(job.id, m.thumbnail);
    if (thumb.getAttribute("src") !== src) thumb.src = src;
    thumb.hidden = false;
  } else {
    thumb.hidden = true;
  }
  $("d-sub").textContent = [
    m.channel,
    m.view_count != null && `${compact(m.view_count)} views`,
    m.upload_date && `uploaded ${m.upload_date}`,
    (v.duration || m.duration) && clock(v.duration || m.duration),
    `model ${job.options.model} · ${job.options.language === "auto" ? "auto language" : job.options.language}`,
  ].filter(Boolean).join(" · ");
  const link = $("d-url");
  link.href = m.webpage_url || job.url;
  link.textContent = m.webpage_url || job.url;

  const badge = $("d-state");
  badge.className = `badge ${job.state}`;
  badge.textContent = STATE_LABEL[job.state] || job.state;
  $("b-cancel").hidden = !ACTIVE.has(job.state);
  $("b-retry").hidden = ACTIVE.has(job.state);
  $("b-prompt").hidden = !job.claude_prompt || job.state !== "done";

  $("d-overall").style.width = `${job.state === "done" ? 100 : job.progress}%`;
  $("d-elapsed").textContent = job.started
    ? `${job.state === "running" ? "Running for" : "Took"} ${elapsed(job.started, job.state === "running" ? null : job.finished)}`
    : job.state === "queued" ? "Waiting for the current job to finish" : "";

  $("d-stages").innerHTML = job.stage_order.map((key, i) => {
    const st = job.stages[key];
    return `<li class="st ${st.status}">
      <div class="top"><span class="dot">${st.status === "running" ? "" : STAGE_ICON[st.status] || i + 1}</span><b>${esc(st.label)}</b></div>
      <small>${esc(st.detail || STAGE_TEXT[st.status])}</small>
      ${st.status === "running" ? `<span class="mini"><i style="width:${st.progress}%"></i></span>` : ""}
      <em>${st.started ? elapsed(st.started, st.ended) : "&nbsp;"}</em>
    </li>`;
  }).join("");

  const err = $("d-error");
  err.hidden = !job.error;
  err.textContent = job.error || "";
  if (job.state === "failed") $("d-logbox").open = true;

  const pre = $("d-log");
  const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
  const text = (job.log || []).join("\n") || "No output yet.";
  if (pre.textContent !== text) {
    pre.textContent = text;
    if (atBottom) pre.scrollTop = pre.scrollHeight;
  }
}

// ---------- results ----------

function resetResults() {
  S.analysis = null;
  S.analysisFor = null;
  S.activeScene = -1;
  S.activeSeg = -1;
  $("results").hidden = true;
  const player = $("player");
  player.pause();
  player.removeAttribute("src");
  player.load();
}

async function loadAnalysis(id) {
  const a = await api(fileUrl(id, "analysis.json"));
  if (S.selected !== id) return;
  S.analysis = a;
  S.analysisFor = id;
  renderResults(id, a);
}

function niceStep(duration, ticks = 6) {
  const steps = [5, 10, 15, 30, 60, 120, 300, 600, 900, 1800];
  return steps.find((s) => duration / s <= ticks) || 3600;
}

function bars(el, values, labels, unit) {
  const max = Math.max(1, ...values);
  el.classList.toggle("dense", values.length > 24);
  el.innerHTML = values.map((v, i) => `
    <div class="b" title="${esc(labels[i])}: ${v} ${unit}">
      <em>${v}</em><i style="height:${(v / max) * 100}%"></i><span class="${i % 5 === 0 ? "keep" : ""}">${esc(labels[i])}</span>
    </div>`).join("");
}

function renderResults(id, a) {
  $("results").hidden = false;
  const k = a.metrics, h = a.hook, v = a.video, D = v.duration || 1;

  const tiles = [
    ["Duration", clock(v.duration), `${v.width}×${v.height} · ${Math.round(v.fps)} fps`],
    ["Scenes", k.scene_count, `${k.cuts} cuts`],
    ["Cuts / min", k.cuts_per_min, `median shot ${k.median_shot}s`],
    ["Avg shot", `${k.avg_shot}s`, `${k.min_shot}s shortest · ${k.max_shot}s longest`],
    ["Words / min", k.wpm ?? "–", k.words ? `${compact(k.words)} words in total` : "no speech found"],
    ["Speech", k.speech_ratio != null ? `${Math.round(k.speech_ratio * 100)}%` : "–", k.longest_silence != null ? `longest silence ${k.longest_silence}s` : "of runtime"],
    [`Hook cuts · ${h.seconds}s`, h.cuts, `avg shot ${h.avg_shot}s`],
    ["First words", h.first_speech != null ? `${h.first_speech}s` : "none", `${h.words} words in the hook`],
  ];
  $("kpis").innerHTML = tiles.map(([label, value, sub]) => `<div class="kpi"><span>${esc(label)}</span><b>${esc(value)}</b><small>${esc(sub)}</small></div>`).join("");

  const player = $("player");
  player.src = fileUrl(id, v.file);
  const poster = a.frames.find((f) => f.t >= 1) || a.frames[0];
  if (poster) player.poster = fileUrl(id, poster.file);

  $("t-lang").textContent = a.language ? `language: ${a.language} · click a line to jump` : "";
  $("transcript").innerHTML = a.segments.length
    ? a.segments.map((s, i) => `<li data-t="${s.start}" data-i="${i}"><time>${clock(s.start)}</time><span>${esc(s.text)}</span></li>`).join("")
    : `<li class="none">No speech detected.</li>`;

  $("timeline").innerHTML = a.scenes.map((s) => {
    const pace = s.length < 2 ? "fast" : s.length < 5 ? "mid" : "slow";
    return `<button class="seg ${pace}" style="width:${(s.length / D) * 100}%" data-t="${s.start}" title="Scene ${s.index} · ${clock(s.start)}–${clock(s.end)} · ${s.length}s"></button>`;
  }).join("") + `<div class="playhead" id="playhead"></div>`;
  const step = niceStep(D);
  const ticks = [];
  for (let t = 0; t < D - step * 0.4; t += step) ticks.push(`<span style="left:${(t / D) * 100}%">${clock(t)}</span>`);
  $("axis").innerHTML = ticks.join("");

  const minutes = a.per_minute.map((p) => clock(p.minute * 60));
  bars($("chart-cuts"), a.per_minute.map((p) => p.cuts_rate), minutes, "cuts/min");
  bars($("chart-words"), a.per_minute.map((p) => p.wpm), minutes, "words/min");

  $("hook-title").textContent = `Hook · first ${h.seconds} seconds`;
  $("hook-sum").textContent = `${h.cuts} cuts · first words at ${h.first_speech ?? "–"}s · ${h.words} words`;
  $("hook-text").textContent = h.text ? `“${h.text}”` : "No narration in the hook.";
  $("hook-strip").innerHTML = a.frames.filter((f) => f.kind === "hook")
    .map((f) => `<button class="fr" data-t="${f.t}"><img loading="lazy" src="${fileUrl(id, f.file)}" alt="Frame at ${f.t}s"><span>${f.t.toFixed(1)}s</span></button>`).join("");

  $("scene-count").textContent = `${a.scenes.length} scenes · click to play from there`;
  $("scenes").innerHTML = a.scenes.map((s, i) => `
    <button class="scene" data-t="${s.start}" data-i="${i}">
      ${s.frame ? `<img loading="lazy" src="${fileUrl(id, s.frame)}" alt="Scene ${s.index}">` : `<span class="noimg">not sampled</span>`}
      <span class="meta"><b>#${s.index} · ${clock(s.start)}</b><em>${s.length}s</em></span>
      <p>${esc(s.text || "No narration")}</p>
    </button>`).join("");
  sync();
}

function indexAt(list, t) {
  let lo = 0, hi = list.length - 1, found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (list[mid].start <= t) { found = mid; lo = mid + 1; } else hi = mid - 1;
  }
  return found;
}

function sync() {
  const a = S.analysis;
  if (!a) return;
  const t = $("player").currentTime || 0;
  const ph = $("playhead");
  if (ph) ph.style.left = `${Math.min(100, (t / (a.video.duration || 1)) * 100)}%`;

  const si = indexAt(a.scenes, t);
  if (si !== S.activeScene) {
    document.querySelectorAll(".scene.on, .seg.on").forEach((el) => el.classList.remove("on"));
    if (si >= 0) {
      document.querySelector(`.scene[data-i="${si}"]`)?.classList.add("on");
      document.querySelectorAll(".seg")[si]?.classList.add("on");
    }
    S.activeScene = si;
  }

  let gi = indexAt(a.segments, t);
  if (gi >= 0 && t > a.segments[gi].end + 0.3) gi = -1;
  if (gi !== S.activeSeg) {
    $("transcript").querySelector("li.on")?.classList.remove("on");
    if (gi >= 0) {
      const li = $("transcript").querySelector(`li[data-i="${gi}"]`);
      if (li) {
        li.classList.add("on");
        const box = $("transcript");
        box.scrollTo({ top: li.offsetTop - box.clientHeight / 3, behavior: "smooth" });
      }
    }
    S.activeSeg = gi;
  }

  const scene = a.scenes[si];
  const line = gi >= 0 ? a.segments[gi].text : "";
  $("now").innerHTML = scene
    ? `<b>Scene ${scene.index}</b> · ${clock(t)} · shot ${scene.length}s${line ? `<br>${esc(line)}` : ""}`
    : "";
}

// ---------- polling ----------

async function tick() {
  let active = false;
  try {
    const { jobs } = await api("/api/jobs");
    S.jobs = jobs;
    active = jobs.some((j) => ACTIVE.has(j.state));
    if (!S.selected && jobs.length) select(jobs[0].id);
    renderJobs();
    if (S.selected) await refreshDetail();
    $("live").classList.remove("off");
  } catch {
    $("live").classList.add("off");
  }
  setTimeout(tick, active ? 1000 : 3000);
}

// ---------- events ----------

$("new-job").addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = $("form-msg"), btn = $("submit");
  btn.disabled = true;
  msg.className = "form-msg";
  msg.textContent = "Adding to the queue…";
  try {
    const { id } = await post("/api/jobs", { url: $("url").value, model: $("model").value, language: $("lang").value });
    $("url").value = "";
    msg.textContent = "Queued. Progress shows on the right.";
    S.selected = null;
    select(id);
    tick();
  } catch (err) {
    msg.className = "form-msg err";
    msg.textContent = err.message;
  } finally {
    btn.disabled = false;
  }
});

$("jobs").addEventListener("click", (e) => {
  const btn = e.target.closest(".job");
  if (btn) select(btn.dataset.id);
});

$("b-cancel").addEventListener("click", async () => {
  await post(`/api/jobs/${S.selected}/cancel`).catch((err) => alert(err.message));
  refreshDetail();
});
$("b-retry").addEventListener("click", async () => {
  await post(`/api/jobs/${S.selected}/retry`).catch((err) => alert(err.message));
  resetResults();
  refreshDetail();
});
$("b-open").addEventListener("click", () => post(`/api/jobs/${S.selected}/open`).catch((err) => alert(err.message)));
$("b-prompt").addEventListener("click", async (e) => {
  const text = S.job?.claude_prompt;
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    flash(e.currentTarget, "Copied ✓");
  } catch {
    window.prompt("Copy this prompt into Claude:", text);
  }
});

$("results").addEventListener("click", (e) => {
  const target = e.target.closest("[data-t]");
  if (!target) return;
  const player = $("player");
  player.currentTime = parseFloat(target.dataset.t) + 0.01;
  player.play().catch(() => {});
});
$("player").addEventListener("timeupdate", sync);
$("player").addEventListener("seeked", sync);

window.addEventListener("hashchange", () => {
  const id = location.hash.slice(1);
  if (id) select(id);
});
const fromHash = location.hash.slice(1);
if (fromHash) select(fromHash);
tick();
tickSystem();
