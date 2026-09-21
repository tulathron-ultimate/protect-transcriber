/* Protect Transcriber — UI logic. No build step, no framework. */
"use strict";

// --------------------------------------------------------------------------- //
// tiny helpers
// --------------------------------------------------------------------------- //

const $ = (id) => document.getElementById(id);
const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

/** APP_TOKEN support: ?token=… is remembered so later fetches carry it. */
const token = (() => {
  const fromUrl = new URLSearchParams(location.search).get("token");
  if (fromUrl) {
    try { localStorage.setItem("pt_token", fromUrl); } catch { /* private mode */ }
    return fromUrl;
  }
  try { return localStorage.getItem("pt_token") || ""; } catch { return ""; }
})();

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (token) headers["X-App-Token"] = token;
  if (options.body && !(options.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const payload = await response.json();
      if (payload.detail) detail = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail);
    } catch { /* non-JSON error body */ }
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

function toast(message, kind = "") {
  const node = el("div", `toast ${kind}`, message);
  $("toasts").append(node);
  setTimeout(() => node.remove(), kind === "err" ? 9000 : 4500);
}

const pad = (n) => String(n).padStart(2, "0");

/** Date -> "YYYY-MM-DDTHH:MM:SS" in *local* time, which datetime-local wants. */
function toLocalInput(date) {
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  );
}
function fromLocalInput(value) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}
function toDayInput(date) {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function formatDuration(seconds) {
  seconds = Math.max(0, Math.round(seconds));
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h) return `${h}h ${pad(m)}m`;
  if (m) return `${m}m ${pad(s)}s`;
  return `${s}s`;
}
function formatClock(seconds) {
  seconds = Math.max(0, seconds);
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}
function formatBytes(bytes) {
  if (!bytes) return "—";
  const units = ["B", "KB", "MB", "GB"];
  let index = 0;
  while (bytes >= 1024 && index < units.length - 1) { bytes /= 1024; index += 1; }
  return `${bytes.toFixed(index ? 1 : 0)} ${units[index]}`;
}
function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (ch) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
}

// --------------------------------------------------------------------------- //
// state
// --------------------------------------------------------------------------- //

const state = {
  cameras: [],
  cameraFilter: "",
  selectedCamera: null,
  day: new Date(),
  events: [],
  range: { start: null, end: null },
  jobs: [],
  searching: false,
  config: { maxRangeSeconds: 14400 },
  viewerJob: null,
};

// --------------------------------------------------------------------------- //
// status pills
// --------------------------------------------------------------------------- //

function setPill(id, kind, label, title) {
  const pill = $(id);
  pill.className = `pill ${kind}`;
  pill.querySelector(".label").textContent = label;
  if (title) pill.title = title;
}

async function refreshProtectStatus() {
  try {
    const info = await api("/api/protect");
    if (info.connected) {
      const version = info.nvr?.version ? ` ${info.nvr.version}` : "";
      setPill("pill-protect", "ok", `Protect${version}`, info.nvr?.name || "Connected");
    } else {
      setPill("pill-protect", "err", "Protect", info.error || "Not connected");
    }
  } catch (error) {
    setPill("pill-protect", "err", "Protect", error.message);
  }
}

async function refreshWhisperStatus(refresh = false) {
  const list = $("instance-list");
  try {
    const data = await api(`/api/whisper${refresh ? "?refresh=true" : ""}`);
    const total = data.instances.length;
    const kind = data.healthy === 0 ? "err" : data.healthy < total ? "warn" : "ok";
    setPill("pill-whisper", kind, `Whisper ${data.healthy}/${total}`,
      "Click to re-probe instances");
    list.replaceChildren();
    for (const instance of data.instances) {
      const row = el("div", `instance ${instance.healthy ? "" : "down"}`);
      row.append(el("span", "iname", instance.name));
      row.append(el("span", "ikind", instance.healthy ? instance.kind : "down"));
      const load = instance.healthy
        ? `${instance.inFlight}/${instance.concurrency}${instance.realtimeFactor ? ` · ${instance.realtimeFactor}×` : ""}`
        : "";
      row.append(el("span", "load", load));
      row.title = instance.healthy
        ? `${instance.url} · model ${instance.model} · ${instance.completed} done, ${instance.failed} failed`
        : `${instance.url} — ${instance.detail}`;
      list.append(row);
    }
    if (!total) {
      const empty = el("div", "instance", "No instances configured");
      empty.style.color = "var(--text-faint)";
      list.append(empty);
    }
    return data;
  } catch (error) {
    setPill("pill-whisper", "err", "Whisper", error.message);
    return null;
  }
}

// --------------------------------------------------------------------------- //
// cameras
// --------------------------------------------------------------------------- //

async function loadCameras() {
  const list = $("camera-list");
  list.replaceChildren(el("li", "empty", "Loading…"));
  try {
    const data = await api("/api/cameras");
    state.cameras = data.cameras;
    renderCameras();
    if (!state.selectedCamera && state.cameras.length) {
      // Default to the first camera that can actually produce audio.
      selectCamera(state.cameras.find((c) => c.hasAudio) || state.cameras[0]);
    }
  } catch (error) {
    list.replaceChildren(el("li", "empty", `Could not load cameras: ${error.message}`));
  }
}

function renderCameras() {
  const list = $("camera-list");
  const needle = state.cameraFilter.toLowerCase();
  const matches = state.cameras.filter((c) => c.name.toLowerCase().includes(needle));
  list.replaceChildren();
  if (!matches.length) {
    list.append(el("li", "empty", state.cameras.length ? "No camera matches." : "No cameras found."));
    return;
  }
  for (const camera of matches) {
    const item = el("li");
    const button = el("button", "camera");
    button.type = "button";
    button.setAttribute("aria-pressed", String(state.selectedCamera?.id === camera.id));
    button.append(el("span", "name", camera.name));
    const badge = camera.hasAudio
      ? el("span", "badge mic", "mic")
      : el("span", "badge nomic", "no mic");
    badge.title = camera.hasAudio
      ? "Camera has a mic — audio should be present"
      : "Protect reports no usable mic (missing or muted); a transcript is unlikely";
    button.append(badge);
    button.addEventListener("click", () => selectCamera(camera));
    item.append(button);
    list.append(item);
  }
}

function selectCamera(camera) {
  if (state.selectedCamera && state.selectedCamera.id !== camera.id) closePreview();
  state.selectedCamera = camera;
  $("selection-title").textContent = camera.name;
  renderCameras();
  loadEvents();
  updateSubmitState();
}

// --------------------------------------------------------------------------- //
// timeline
// --------------------------------------------------------------------------- //

const canvas = $("timeline");
const ctx = canvas.getContext("2d");
const EVENT_COLORS = {
  smartDetectZone: "#3d8bfd",
  smartAudioDetect: "#a78bfa",
  motion: "#37d399",
  ring: "#f0b429",
  sensorMotion: "#37d399",
};
let drag = null;

function dayBounds() {
  const start = new Date(state.day);
  start.setHours(0, 0, 0, 0);
  const end = new Date(start);
  end.setDate(end.getDate() + 1);
  return { start, end };
}

const timeToX = (date, width) => {
  const { start, end } = dayBounds();
  return ((date - start) / (end - start)) * width;
};
const xToTime = (x, width) => {
  const { start, end } = dayBounds();
  const ratio = Math.min(1, Math.max(0, x / width));
  return new Date(start.getTime() + ratio * (end - start));
};

function resizeCanvas() {
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || 600;
  const height = 96;
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  drawTimeline();
}

function drawTimeline() {
  const width = canvas.clientWidth || 600;
  const height = 96;
  const { start: dayStart, end: dayEnd } = dayBounds();
  ctx.clearRect(0, 0, width, height);

  // no-footage shading: before the oldest recording, and after "now"
  ctx.fillStyle = "rgba(255,255,255,0.035)";
  const recordingStart = state.selectedCamera?.recordingStart
    ? new Date(state.selectedCamera.recordingStart)
    : null;
  if (recordingStart && recordingStart > dayStart) {
    const x = Math.min(width, timeToX(recordingStart, width));
    if (x > 0) ctx.fillRect(0, 0, x, height);
  }
  const now = new Date();
  if (now < dayEnd && now > dayStart) {
    const x = timeToX(now, width);
    ctx.fillRect(x, 0, width - x, height);
  }

  // hour grid — label every hour when there is room, else every 3 hours
  const step = width > 720 ? 1 : width > 420 ? 3 : 6;
  ctx.font = "10px ui-monospace, monospace";
  ctx.textBaseline = "top";
  for (let hour = 0; hour <= 24; hour += 1) {
    const x = (hour / 24) * width;
    const major = hour % step === 0;
    ctx.strokeStyle = major ? "rgba(255,255,255,0.14)" : "rgba(255,255,255,0.05)";
    ctx.beginPath();
    ctx.moveTo(Math.round(x) + 0.5, major ? 14 : 22);
    ctx.lineTo(Math.round(x) + 0.5, height - 16);
    ctx.stroke();
    if (major && hour < 24) {
      ctx.fillStyle = "#6b7583";
      ctx.fillText(`${pad(hour)}:00`, Math.min(x + 3, width - 30), 2);
    }
  }

  // event markers
  for (const event of state.events) {
    const eventStart = new Date(event.start);
    const eventEnd = event.end ? new Date(event.end) : new Date(eventStart.getTime() + 4000);
    const x1 = timeToX(eventStart, width);
    const x2 = Math.max(x1 + 2, timeToX(eventEnd, width));
    ctx.fillStyle = EVENT_COLORS[event.type] || "#9aa4b2";
    ctx.globalAlpha = 0.85;
    ctx.fillRect(x1, height - 14, x2 - x1, 8);
    ctx.globalAlpha = 1;
  }

  // selection
  const { start, end } = state.range;
  if (start && end) {
    const x1 = timeToX(start, width);
    const x2 = timeToX(end, width);
    const left = Math.min(x1, x2);
    const right = Math.max(x1, x2);
    ctx.fillStyle = "rgba(61,139,253,0.22)";
    ctx.fillRect(left, 12, Math.max(2, right - left), height - 28);
    ctx.strokeStyle = "#3d8bfd";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(left + 0.5, 12);
    ctx.lineTo(left + 0.5, height - 16);
    ctx.moveTo(right - 0.5, 12);
    ctx.lineTo(right - 0.5, height - 16);
    ctx.stroke();
  }

  // "now" needle
  if (now < dayEnd && now > dayStart) {
    const x = timeToX(now, width);
    ctx.strokeStyle = "#f2685f";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x + 0.5, 8);
    ctx.lineTo(x + 0.5, height - 8);
    ctx.stroke();
  }
}

/** An event marker under the cursor, if any (the bottom 14px strip). */
function eventAt(x, y) {
  const width = canvas.clientWidth || 600;
  if (y < 96 - 16) return null;
  for (const event of state.events) {
    const start = new Date(event.start);
    const end = event.end ? new Date(event.end) : new Date(start.getTime() + 4000);
    const x1 = timeToX(start, width);
    const x2 = Math.max(x1 + 4, timeToX(end, width));
    if (x >= x1 - 2 && x <= x2 + 2) return event;
  }
  return null;
}

function canvasPoint(event) {
  const rect = canvas.getBoundingClientRect();
  return { x: event.clientX - rect.left, y: event.clientY - rect.top };
}

canvas.addEventListener("pointerdown", (pointerEvent) => {
  const { x, y } = canvasPoint(pointerEvent);
  const hit = eventAt(x, y);
  if (hit) {
    // Clicking a marker selects that event, padded a little on each side.
    const start = new Date(new Date(hit.start).getTime() - 3000);
    const end = new Date((hit.end ? new Date(hit.end).getTime() : new Date(hit.start).getTime() + 10000) + 3000);
    setRange(start, end);
    return;
  }
  canvas.setPointerCapture(pointerEvent.pointerId);
  drag = { origin: xToTime(x, canvas.clientWidth), moved: false };
});

canvas.addEventListener("pointermove", (pointerEvent) => {
  const { x, y } = canvasPoint(pointerEvent);
  if (!drag) {
    canvas.style.cursor = eventAt(x, y) ? "pointer" : "crosshair";
    return;
  }
  drag.moved = true;
  setRange(drag.origin, xToTime(x, canvas.clientWidth), { silent: true });
});

canvas.addEventListener("pointerup", (pointerEvent) => {
  if (!drag) return;
  const { x } = canvasPoint(pointerEvent);
  if (!drag.moved) {
    // A plain click gives a 60-second window starting where you clicked.
    const start = xToTime(x, canvas.clientWidth);
    setRange(start, new Date(start.getTime() + 60000));
  } else {
    setRange(drag.origin, xToTime(x, canvas.clientWidth));
  }
  drag = null;
});
canvas.addEventListener("pointercancel", () => { drag = null; });

// --------------------------------------------------------------------------- //
// range
// --------------------------------------------------------------------------- //

function setRange(a, b, { silent = false } = {}) {
  if (!a || !b) return;
  let start = a < b ? a : b;
  let end = a < b ? b : a;
  if (end - start < 1000) end = new Date(start.getTime() + 1000);
  state.range = { start, end };
  $("range-start").value = toLocalInput(start);
  $("range-end").value = toLocalInput(end);
  $("range-duration").textContent = formatDuration((end - start) / 1000);
  drawTimeline();
  if (!silent) updateSubmitState();
  else updateSubmitState();
}

function readRangeInputs() {
  const start = fromLocalInput($("range-start").value);
  const end = fromLocalInput($("range-end").value);
  if (start && end) setRange(start, end);
}

function updateSubmitState() {
  const { start, end } = state.range;
  const button = $("submit-job");
  const error = $("form-error");
  error.hidden = true;
  let reason = "";
  if (!state.selectedCamera) reason = "Pick a camera first.";
  else if (!start || !end) reason = "";
  else if (end <= start) reason = "End must be after start.";
  else if (start > new Date()) reason = "That range is in the future.";
  else {
    const seconds = (end - start) / 1000;
    if (seconds > state.config.maxRangeSeconds) {
      reason = `Range is ${formatDuration(seconds)}; the limit is ${formatDuration(state.config.maxRangeSeconds)}.`;
    }
  }
  const ready = Boolean(state.selectedCamera && start && end && !reason);
  button.disabled = !ready;
  // Preview has the same preconditions as transcribing, plus its own length cap
  // which the server enforces and reports.
  $("preview-job").disabled = !ready;
  if (reason && start && end) {
    error.textContent = reason;
    error.hidden = false;
  }
}

function setDay(date) {
  state.day = date;
  $("day-picker").value = toDayInput(date);
  loadEvents();
  drawTimeline();
}

async function loadEvents() {
  if (!state.selectedCamera) return;
  const { start, end } = dayBounds();
  try {
    const data = await api(
      `/api/cameras/${encodeURIComponent(state.selectedCamera.id)}/events` +
      `?start=${encodeURIComponent(start.toISOString())}&end=${encodeURIComponent(end.toISOString())}`
    );
    state.events = data.events || [];
  } catch {
    state.events = []; // markers are optional garnish
  }
  drawTimeline();
}

// --------------------------------------------------------------------------- //
// submitting
// --------------------------------------------------------------------------- //

async function submitJob() {
  const { start, end } = state.range;
  if (!state.selectedCamera || !start || !end) return;
  const button = $("submit-job");
  button.disabled = true;
  try {
    const job = await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify({
        cameraId: state.selectedCamera.id,
        start: start.toISOString(),
        end: end.toISOString(),
        title: $("opt-title").value.trim(),
        language: $("opt-language").value.trim() || null,
        task: $("opt-task").value,
        prompt: $("opt-prompt").value.trim() || null,
      }),
    });
    toast(`Queued “${job.title}” (${formatDuration((end - start) / 1000)})`, "ok");
    upsertJob(job);
  } catch (error) {
    toast(`Could not queue the job: ${error.message}`, "err");
  } finally {
    updateSubmitState();
  }
}

async function uploadClip(file) {
  const form = new FormData();
  form.append("file", file);
  form.append("title", file.name);
  form.append("language", $("opt-language").value.trim());
  form.append("task", $("opt-task").value);
  form.append("prompt", $("opt-prompt").value.trim());
  toast(`Uploading ${file.name} (${formatBytes(file.size)})…`);
  try {
    const job = await api("/api/jobs/upload", { method: "POST", body: form });
    toast(`Queued “${job.title}”`, "ok");
    upsertJob(job);
  } catch (error) {
    toast(`Upload failed: ${error.message}`, "err");
  }
}

// --------------------------------------------------------------------------- //
// jobs
// --------------------------------------------------------------------------- //

const ACTIVE_STATUSES = new Set(["queued", "exporting", "extracting", "transcribing"]);

async function loadJobs() {
  try {
    const data = await api("/api/jobs?limit=100");
    state.jobs = data.jobs;
    state.searching = false;
    renderJobs();
  } catch (error) {
    $("job-list").replaceChildren(el("li", "empty", `Could not load jobs: ${error.message}`));
  }
}

function upsertJob(job) {
  if (state.searching) return;
  const index = state.jobs.findIndex((j) => j.id === job.id);
  if (index === -1) state.jobs.unshift(job);
  else state.jobs[index] = { ...state.jobs[index], ...job };
  renderJobs();
  if (state.viewerJob === job.id && job.status === "completed") openViewer(job.id);
}

function stageLabel(job) {
  if (job.status === "transcribing" && job.chunkCount) {
    return `transcribing ${job.chunksDone}/${job.chunkCount}`;
  }
  return job.stage || job.status;
}

function renderJobs() {
  const list = $("job-list");
  list.replaceChildren();
  if (!state.jobs.length) {
    list.append(el("li", "empty", state.searching ? "No transcript matches." : "No transcripts yet."));
    return;
  }
  for (const job of state.jobs) {
    list.append(renderJob(job));
  }
}

function renderJob(job) {
  const item = el("li", `job ${ACTIVE_STATUSES.has(job.status) ? "" : "done"}`);

  const top = el("div", "job-top");
  top.append(el("span", "job-title", job.title || job.id));
  top.append(el("span", `job-status ${job.status}`, stageLabel(job)));
  item.append(top);

  const meta = el("div", "job-meta");
  const bits = [];
  if (job.cameraName) bits.push(job.cameraName);
  if (job.rangeStart) bits.push(new Date(job.rangeStart).toLocaleString());
  if (job.audioSeconds) bits.push(formatDuration(job.audioSeconds));
  if (job.clipBytes) bits.push(formatBytes(job.clipBytes));
  if (job.detectedLanguage) bits.push(job.detectedLanguage);
  if (job.instances?.length) bits.push(`via ${job.instances.join(", ")}`);
  for (const bit of bits) meta.append(el("span", null, bit));
  item.append(meta);

  if (ACTIVE_STATUSES.has(job.status)) {
    const bar = el("div", "bar");
    const fill = el("span");
    fill.style.width = `${Math.round((job.progress || 0) * 100)}%`;
    bar.append(fill);
    item.append(bar);
  }

  if (job.snippet) {
    const preview = el("p", "job-preview");
    preview.innerHTML = job.snippet; // server-generated <mark> around the hit
    item.append(preview);
  } else if (job.preview) {
    item.append(el("p", "job-preview", job.preview));
  }
  if (job.error) item.append(el("p", "job-error", job.error));

  const actions = el("div", "job-actions");
  if (job.status === "completed") {
    const open = el("button", "ghost", "Open");
    open.type = "button";
    open.addEventListener("click", () => openViewer(job.id));
    actions.append(open);
  }
  if (ACTIVE_STATUSES.has(job.status)) {
    const cancel = el("button", "ghost", "Cancel");
    cancel.type = "button";
    cancel.addEventListener("click", async () => {
      try { await api(`/api/jobs/${job.id}/cancel`, { method: "POST" }); }
      catch (error) { toast(error.message, "err"); }
    });
    actions.append(cancel);
  }
  if (job.status === "failed" || job.status === "canceled") {
    const retry = el("button", "ghost", "Retry");
    retry.type = "button";
    retry.addEventListener("click", async () => {
      try {
        upsertJob(await api(`/api/jobs/${job.id}/retry`, { method: "POST" }));
        toast("Re-queued", "ok");
      } catch (error) { toast(error.message, "err"); }
    });
    actions.append(retry);
  }
  if (!ACTIVE_STATUSES.has(job.status)) {
    const remove = el("button", "ghost", "Delete");
    remove.type = "button";
    remove.addEventListener("click", async () => {
      if (!confirm(`Delete “${job.title}” and its clip?`)) return;
      try {
        await api(`/api/jobs/${job.id}`, { method: "DELETE" });
        state.jobs = state.jobs.filter((j) => j.id !== job.id);
        renderJobs();
      } catch (error) { toast(error.message, "err"); }
    });
    actions.append(remove);
  }
  item.append(actions);
  return item;
}

/** Live job updates. EventSource cannot set headers, so the token rides the query. */
function connectStream() {
  const url = `/api/jobs/stream${token ? `?token=${encodeURIComponent(token)}` : ""}`;
  const stream = new EventSource(url);
  stream.addEventListener("message", (message) => {
    let event;
    try { event = JSON.parse(message.data); } catch { return; }
    if (event.type === "job.deleted") {
      state.jobs = state.jobs.filter((j) => j.id !== event.jobId);
      renderJobs();
    } else if (event.job) {
      upsertJob(event.job);
      if (["exporting", "transcribing"].includes(event.job.status)) refreshWhisperStatus();
    }
  });
  stream.addEventListener("error", () => {
    // EventSource retries on its own; reload once it is back to resync.
    stream.addEventListener("open", loadJobs, { once: true });
  });
}

async function runSearch(query) {
  if (!query.trim()) { await loadJobs(); return; }
  try {
    const data = await api(`/api/jobs/search?q=${encodeURIComponent(query)}`);
    state.jobs = data.results;
    state.searching = true;
    renderJobs();
  } catch (error) {
    toast(`Search failed: ${error.message}`, "err");
  }
}

// --------------------------------------------------------------------------- //
// transcript viewer
// --------------------------------------------------------------------------- //

const viewer = $("viewer");
const video = $("viewer-video");

async function openViewer(jobId) {
  let job;
  try {
    job = await api(`/api/jobs/${jobId}`);
  } catch (error) {
    toast(error.message, "err");
    return;
  }
  state.viewerJob = jobId;
  $("viewer-title").textContent = job.title || jobId;
  const meta = [
    job.cameraName,
    job.rangeStart ? new Date(job.rangeStart).toLocaleString() : null,
    job.audioSeconds ? formatDuration(job.audioSeconds) : null,
    job.detectedLanguage,
    job.instances?.length ? `via ${job.instances.join(", ")}` : null,
  ].filter(Boolean);
  $("viewer-meta").textContent = meta.join(" · ");

  state.viewerOffset = job.clipOffset || 0;
  if (job.hasClip) {
    video.src = `/api/jobs/${jobId}/clip${token ? `?token=${encodeURIComponent(token)}` : ""}`;
    video.hidden = false;
  } else {
    video.removeAttribute("src");
    video.hidden = true;
  }

  const downloads = $("viewer-downloads");
  downloads.replaceChildren();
  const formats = [["txt", "TXT"], ["srt", "SRT"], ["vtt", "VTT"], ["json", "JSON"]];
  if (job.rangeStart) formats.push(["log", "Timestamped log"]);
  for (const [format, label] of formats) {
    const link = el("a", null, label);
    link.href = `/api/jobs/${jobId}/transcript.${format}${token ? `?token=${encodeURIComponent(token)}` : ""}`;
    link.download = "";
    downloads.append(link);
  }
  const copy = el("a", null, "Copy text");
  copy.href = "#";
  copy.addEventListener("click", async (clickEvent) => {
    clickEvent.preventDefault();
    try {
      await navigator.clipboard.writeText(job.text || "");
      toast("Transcript copied", "ok");
    } catch { toast("Clipboard is blocked by the browser", "err"); }
  });
  downloads.append(copy);

  renderSegments(job);
  $("viewer-find").value = "";
  $("ask-input").value = "";
  $("ask-out").replaceChildren();
  analysisStatus("");
  // Results are stored on the job, so a reopened transcript shows them again
  // without paying for another call.
  if (job.review) renderReview(job.review);
  else if (job.summary) renderSummary(job.summary);
  else $("analysis-out").replaceChildren();
  if (!viewer.open) viewer.showModal();
}

function renderSegments(job) {
  const list = $("viewer-segments");
  list.replaceChildren();
  const segments = job.segments || [];
  if (!segments.length) {
    const item = el("li");
    item.append(el("div", "plain-text", job.text || "(no speech detected)"));
    list.append(item);
    return;
  }
  for (const segment of segments) {
    const item = el("li", "segment");
    item.dataset.start = segment.start;
    item.dataset.end = segment.end;
    const stamp = el("time", null, formatClock(segment.start));
    stamp.dateTime = `PT${Math.round(segment.start)}S`;
    item.append(stamp);
    item.append(el("span", "stext", segment.text));
    item.addEventListener("click", () => {
      if (!video.hidden && video.src) {
        // A preview-backed clip holds the whole previewed range, so shift by
        // where this job's audio started inside it.
        video.currentTime = segment.start + (job.clipOffset || 0);
        video.play().catch(() => { /* autoplay policy */ });
      }
    });
    list.append(item);
  }
}

video.addEventListener("timeupdate", () => {
  const now = video.currentTime - (state.viewerOffset || 0);
  let active = null;
  for (const item of $("viewer-segments").children) {
    const start = Number(item.dataset.start);
    const end = Number(item.dataset.end);
    const isActive = now >= start && now < Math.max(end, start + 0.2);
    item.classList.toggle("active", isActive);
    if (isActive) active = item;
  }
  if (active) active.scrollIntoView({ block: "nearest" });
});

$("viewer-find").addEventListener("input", (inputEvent) => {
  const needle = inputEvent.target.value.trim().toLowerCase();
  for (const item of $("viewer-segments").children) {
    const textNode = item.querySelector(".stext");
    if (!textNode) continue;
    const raw = textNode.textContent;
    if (!needle) {
      textNode.textContent = raw;
      item.hidden = false;
      continue;
    }
    const matches = raw.toLowerCase().includes(needle);
    item.hidden = !matches;
    if (matches) {
      const index = raw.toLowerCase().indexOf(needle);
      textNode.innerHTML =
        escapeHtml(raw.slice(0, index)) +
        `<mark>${escapeHtml(raw.slice(index, index + needle.length))}</mark>` +
        escapeHtml(raw.slice(index + needle.length));
    }
  }
});

$("viewer-close").addEventListener("click", () => viewer.close());
viewer.addEventListener("close", () => {
  video.pause();
  state.viewerJob = null;
});

// --------------------------------------------------------------------------- //
// preview: watch and listen to a range before transcribing it
// --------------------------------------------------------------------------- //

const previewVideo = $("preview-video");
const waveCanvas = $("waveform");
const waveCtx = waveCanvas.getContext("2d");

const preview = {
  id: null,
  peaks: [],
  duration: 0,
  start: null, // absolute Date of the previewed clip's first frame
  hasAudio: false,
  trim: null, // {from, to} in clip-relative seconds
  poll: null,
  drag: null,
};

function previewStatus(text, kind = "") {
  const node = $("preview-status");
  node.className = `preview-status ${kind}`;
  node.textContent = text;
}

function resizeWaveform() {
  const ratio = window.devicePixelRatio || 1;
  const width = waveCanvas.clientWidth || 600;
  waveCanvas.width = Math.round(width * ratio);
  waveCanvas.height = Math.round(80 * ratio);
  waveCtx.setTransform(ratio, 0, 0, ratio, 0, 0);
  drawWaveform();
}

function drawWaveform() {
  const width = waveCanvas.clientWidth || 600;
  const height = 80;
  const mid = height / 2;
  waveCtx.clearRect(0, 0, width, height);
  if (!preview.peaks.length) return;

  // trimmed-out regions are dimmed so the kept range reads as the subject
  const trim = preview.trim;
  const xOf = (seconds) => (seconds / Math.max(0.001, preview.duration)) * width;

  const bars = preview.peaks.length;
  const barWidth = width / bars;
  for (let i = 0; i < bars; i += 1) {
    const seconds = (i / bars) * preview.duration;
    const inTrim = !trim || (seconds >= trim.from && seconds <= trim.to);
    // A floor keeps silence visible as a hairline rather than nothing at all.
    const amplitude = Math.max(1, preview.peaks[i] * (mid - 4));
    waveCtx.fillStyle = inTrim ? "#3d8bfd" : "rgba(154,164,178,0.30)";
    waveCtx.fillRect(i * barWidth, mid - amplitude, Math.max(1, barWidth - 0.5), amplitude * 2);
  }

  if (trim) {
    waveCtx.strokeStyle = "#37d399";
    waveCtx.lineWidth = 1.5;
    for (const edge of [trim.from, trim.to]) {
      const x = xOf(edge);
      waveCtx.beginPath();
      waveCtx.moveTo(x + 0.5, 2);
      waveCtx.lineTo(x + 0.5, height - 2);
      waveCtx.stroke();
    }
  }

  // playhead
  if (previewVideo.duration) {
    const x = xOf(previewVideo.currentTime);
    waveCtx.strokeStyle = "#f2685f";
    waveCtx.lineWidth = 1;
    waveCtx.beginPath();
    waveCtx.moveTo(x + 0.5, 0);
    waveCtx.lineTo(x + 0.5, height);
    waveCtx.stroke();
  }
}

function waveformSeconds(event) {
  const rect = waveCanvas.getBoundingClientRect();
  const ratio = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
  return ratio * preview.duration;
}

function updatePreviewRange() {
  const trim = preview.trim;
  const from = trim ? trim.from : 0;
  const to = trim ? trim.to : preview.duration;
  const length = Math.max(0, to - from);
  $("preview-reset").hidden = !trim;
  $("preview-range").innerHTML = trim
    ? `Trimmed to <strong>${formatDuration(length)}</strong> (${formatClock(from)}–${formatClock(to)} of the preview)`
    : `Full preview · <strong>${formatDuration(preview.duration)}</strong>`;
}

waveCanvas.addEventListener("pointerdown", (event) => {
  if (!preview.peaks.length) return;
  waveCanvas.setPointerCapture(event.pointerId);
  preview.drag = { origin: waveformSeconds(event), moved: false };
});

waveCanvas.addEventListener("pointermove", (event) => {
  if (!preview.drag) return;
  preview.drag.moved = true;
  const current = waveformSeconds(event);
  preview.trim = {
    from: Math.min(preview.drag.origin, current),
    to: Math.max(preview.drag.origin, current),
  };
  drawWaveform();
  updatePreviewRange();
});

waveCanvas.addEventListener("pointerup", (event) => {
  if (!preview.drag) return;
  const seconds = waveformSeconds(event);
  if (!preview.drag.moved) {
    // A plain click seeks rather than trimming.
    previewVideo.currentTime = seconds;
    previewVideo.play().catch(() => {});
  } else if (preview.trim && preview.trim.to - preview.trim.from < 0.5) {
    preview.trim = null; // too small to be deliberate
  }
  preview.drag = null;
  drawWaveform();
  updatePreviewRange();
});
waveCanvas.addEventListener("pointercancel", () => { preview.drag = null; });

previewVideo.addEventListener("timeupdate", drawWaveform);
previewVideo.addEventListener("seeked", drawWaveform);

function stopPreviewPolling() {
  if (preview.poll) {
    clearInterval(preview.poll);
    preview.poll = null;
  }
}

function closePreview() {
  stopPreviewPolling();
  previewVideo.pause();
  previewVideo.removeAttribute("src");
  previewVideo.load();
  preview.id = null;
  preview.peaks = [];
  preview.trim = null;
  $("preview-panel").hidden = true;
}

async function startPreview() {
  const { start, end } = state.range;
  if (!state.selectedCamera || !start || !end) return;
  const button = $("preview-job");
  button.disabled = true;
  $("preview-panel").hidden = false;
  previewVideo.hidden = true;
  waveCanvas.hidden = true;
  preview.peaks = [];
  preview.trim = null;
  previewStatus("exporting the clip from Protect…", "busy");
  $("preview-range").textContent = "";

  try {
    const created = await api("/api/preview", {
      method: "POST",
      body: JSON.stringify({
        cameraId: state.selectedCamera.id,
        start: start.toISOString(),
        end: end.toISOString(),
      }),
    });
    preview.id = created.id;
    preview.start = new Date(created.rangeStart);
    if (created.status === "ready") {
      await loadPreview(created.id);
    } else {
      stopPreviewPolling();
      preview.poll = setInterval(() => loadPreview(created.id), 900);
    }
  } catch (error) {
    previewStatus(error.message, "err");
  } finally {
    button.disabled = false;
  }
}

async function loadPreview(previewId) {
  let record;
  try {
    record = await api(`/api/preview/${previewId}`);
  } catch (error) {
    stopPreviewPolling();
    previewStatus(error.message, "err");
    return;
  }
  if (record.status === "failed") {
    stopPreviewPolling();
    previewStatus(record.error || "Preview failed", "err");
    return;
  }
  if (record.status !== "ready") {
    previewStatus(
      record.status === "processing" ? "reading the audio…" : "exporting the clip from Protect…",
      "busy"
    );
    return;
  }

  stopPreviewPolling();
  preview.id = record.id;
  preview.duration = record.duration || 0;
  preview.peaks = record.peaks || [];
  preview.hasAudio = record.hasAudio;
  preview.start = new Date(record.rangeStart);
  preview.trim = null;

  previewVideo.src = `/api/preview/${record.id}/clip${token ? `?token=${encodeURIComponent(token)}` : ""}`;
  previewVideo.hidden = false;
  waveCanvas.hidden = !preview.peaks.length;
  $("waveform-hint").textContent = preview.peaks.length
    ? "Drag across the waveform to narrow the range. Click to seek."
    : "This clip has no audio track, so there is nothing to transcribe.";
  previewStatus(
    `${formatDuration(preview.duration)} · ${formatBytes(record.clipBytes)}` +
      (record.hasAudio ? "" : " · no audio"),
    record.hasAudio ? "" : "err"
  );
  resizeWaveform();
  updatePreviewRange();
}

/** Transcribe what is on screen, reusing the already-exported preview clip. */
async function transcribePreview() {
  if (!preview.id || !preview.start) return;
  const trim = preview.trim;
  const fromSeconds = trim ? trim.from : 0;
  const toSeconds = trim ? trim.to : preview.duration;
  const start = new Date(preview.start.getTime() + fromSeconds * 1000);
  const end = new Date(preview.start.getTime() + toSeconds * 1000);

  const button = $("preview-transcribe");
  button.disabled = true;
  try {
    const job = await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify({
        cameraId: state.selectedCamera.id,
        previewId: preview.id,
        start: start.toISOString(),
        end: end.toISOString(),
        title: $("opt-title").value.trim(),
        language: $("opt-language").value.trim() || null,
        task: $("opt-task").value,
        prompt: $("opt-prompt").value.trim() || null,
      }),
    });
    toast(`Queued “${job.title}” (${formatDuration((end - start) / 1000)}) — no re-export needed`, "ok");
    upsertJob(job);
    // Fold the trim back into the main selection so the timeline agrees.
    setRange(start, end);
  } catch (error) {
    toast(`Could not queue the job: ${error.message}`, "err");
  } finally {
    button.disabled = false;
  }
}

$("preview-job").addEventListener("click", startPreview);
$("preview-close").addEventListener("click", closePreview);
$("preview-transcribe").addEventListener("click", transcribePreview);
$("preview-reset").addEventListener("click", () => {
  preview.trim = null;
  drawWaveform();
  updatePreviewRange();
});

// --------------------------------------------------------------------------- //
// whisper instance settings
// --------------------------------------------------------------------------- //

const instancesDialog = $("instances-dialog");

/** Read a row's form values back out as an API payload. */
function readRow(row) {
  const value = (field) => row.querySelector(`[data-field="${field}"]`);
  return {
    name: value("name").value.trim(),
    url: value("url").value.trim(),
    kind: value("kind").value,
    model: value("model").value.trim(),
    concurrency: Number(value("concurrency").value) || 1,
    apiKey: value("apiKey").value,
    enabled: value("enabled").checked,
  };
}

function setRowState(row, text, kind = "") {
  const node = row.querySelector('[data-role="state"]');
  node.className = `instance-state ${kind}`;
  node.textContent = text;
}

function buildRow(instance) {
  const row = $("instance-row-template").content.firstElementChild.cloneNode(true);
  const field = (name) => row.querySelector(`[data-field="${name}"]`);
  row.dataset.id = instance?.id ?? "";
  field("name").value = instance?.name ?? "";
  field("url").value = instance?.url ?? "";
  field("kind").value = instance?.kind ?? "auto";
  field("model").value = instance?.model ?? "";
  field("concurrency").value = instance?.concurrency ?? 1;
  field("enabled").checked = instance?.enabled ?? true;
  // The key itself is never sent to the browser; show a placeholder when one is
  // stored so an empty box does not look like "no key".
  field("apiKey").placeholder = instance?.hasApiKey ? "•••••• (unchanged)" : "none";

  if (instance?.status) {
    const status = instance.status;
    setRowState(
      row,
      status.healthy
        ? `${status.kind}${status.realtimeFactor ? ` · ${status.realtimeFactor}×` : ""}`
        : status.detail,
      status.healthy ? "ok" : "err"
    );
  } else if (instance && !instance.enabled) {
    setRowState(row, "disabled");
  } else if (!instance) {
    setRowState(row, "unsaved");
  }

  for (const input of row.querySelectorAll("[data-field]")) {
    input.addEventListener("input", () => row.classList.add("dirty"));
    input.addEventListener("change", () => row.classList.add("dirty"));
  }

  row.querySelector('[data-action="test"]').addEventListener("click", () => testRow(row));
  row.querySelector('[data-action="save"]').addEventListener("click", () => saveRow(row));
  row.querySelector('[data-action="delete"]').addEventListener("click", () => deleteRow(row));
  return row;
}

async function loadInstances() {
  try {
    const data = await api("/api/whisper/instances");
    const editorList = $("instance-editor");
    editorList.replaceChildren();
    for (const instance of data.instances) editorList.append(buildRow(instance));
    $("instance-hint").textContent = data.instances.length
      ? "Changes apply as soon as you save — no container restart."
      : "No instances yet. Add the URL of a Whisper container on your network.";
    return data.instances;
  } catch (error) {
    toast(`Could not load instances: ${error.message}`, "err");
    return [];
  }
}

async function testRow(row) {
  const payload = readRow(row);
  if (!payload.url) {
    setRowState(row, "enter a URL first", "err");
    return;
  }
  setRowState(row, "testing…", "busy");
  try {
    const result = await api("/api/whisper/instances/test", {
      method: "POST",
      body: JSON.stringify({ url: payload.url, kind: payload.kind, apiKey: payload.apiKey }),
    });
    if (!result.reachable) {
      setRowState(row, result.detail || "no Whisper API found", "err");
      return;
    }
    setRowState(row, `reachable · ${result.kind}`, "ok");
    // Offer the models this server actually has, so the field is not guesswork.
    if (result.models?.length) {
      const listId = `models-${row.dataset.id || Math.random().toString(36).slice(2)}`;
      let list = document.getElementById(listId);
      if (!list) {
        list = el("datalist");
        list.id = listId;
        document.body.append(list);
      }
      list.replaceChildren();
      for (const model of result.models) {
        const option = el("option");
        option.value = model;
        list.append(option);
      }
      const modelField = row.querySelector('[data-field="model"]');
      modelField.setAttribute("list", listId);
      if (!modelField.value && result.models.length === 1) {
        modelField.value = result.models[0];
        row.classList.add("dirty");
      }
    }
  } catch (error) {
    setRowState(row, error.message, "err");
  }
}

async function saveRow(row) {
  const payload = readRow(row);
  if (!payload.name || !payload.url) {
    setRowState(row, "name and URL are required", "err");
    return;
  }
  // An untouched password box means "keep the stored key", so drop it.
  if (!payload.apiKey) delete payload.apiKey;
  setRowState(row, "saving…", "busy");
  try {
    if (row.dataset.id) {
      await api(`/api/whisper/instances/${row.dataset.id}`, {
        method: "PATCH",
        body: JSON.stringify(payload),
      });
    } else {
      await api("/api/whisper/instances", { method: "POST", body: JSON.stringify(payload) });
    }
    row.classList.remove("dirty");
    toast(`Saved “${payload.name}”`, "ok");
    await loadInstances();
    await refreshWhisperStatus(true);
  } catch (error) {
    setRowState(row, error.message, "err");
  }
}

async function deleteRow(row) {
  const name = row.querySelector('[data-field="name"]').value || "this instance";
  if (!row.dataset.id) {
    row.remove();
    return;
  }
  if (!confirm(`Remove ${name} from the pool?`)) return;
  try {
    await api(`/api/whisper/instances/${row.dataset.id}`, { method: "DELETE" });
    toast(`Removed “${name}”`, "ok");
    await loadInstances();
    await refreshWhisperStatus(true);
  } catch (error) {
    setRowState(row, error.message, "err");
  }
}

async function openInstances() {
  await loadInstances();
  if (!instancesDialog.open) instancesDialog.showModal();
}

$("open-instances").addEventListener("click", openInstances);
$("instances-close").addEventListener("click", () => instancesDialog.close());
$("instance-add").addEventListener("click", () => {
  const row = buildRow(null);
  $("instance-editor").append(row);
  row.querySelector('[data-field="name"]').focus();
});
instancesDialog.addEventListener("close", () => refreshWhisperStatus());

// --------------------------------------------------------------------------- //
// transcript analysis (summary / review / ask)
// --------------------------------------------------------------------------- //

const analysisDialog = $("analysis-dialog");

async function refreshAnalysisState() {
  try {
    const config = await api("/api/analysis");
    const node = $("analysis-state");
    node.className = `analysis-state ${config.ready ? "ready" : ""}`;
    node.textContent = config.ready
      ? `${config.model}`
      : config.baseUrl
        ? "configured but disabled"
        : "Not configured";
    state.analysisReady = config.ready;
    return config;
  } catch {
    return null;
  }
}

async function openAnalysisSettings() {
  const config = (await refreshAnalysisState()) || {};
  $("an-url").value = config.baseUrl || "";
  $("an-model").value = config.model || "";
  $("an-enabled").checked = Boolean(config.enabled);
  $("an-key").value = "";
  $("an-key").placeholder = config.hasApiKey ? "•••••• (unchanged)" : "none";
  $("an-state").textContent = config.ready ? "ready" : "";
  $("an-state").className = `instance-state ${config.ready ? "ok" : ""}`;
  if (!analysisDialog.open) analysisDialog.showModal();
}

function analysisPayload() {
  const payload = {
    baseUrl: $("an-url").value.trim(),
    model: $("an-model").value.trim(),
    enabled: $("an-enabled").checked,
  };
  // An untouched key box means "keep what is stored".
  const key = $("an-key").value;
  if (key) payload.apiKey = key;
  return payload;
}

$("open-analysis").addEventListener("click", openAnalysisSettings);
$("analysis-close").addEventListener("click", () => analysisDialog.close());

$("an-test").addEventListener("click", async () => {
  const node = $("an-state");
  node.className = "instance-state busy";
  node.textContent = "testing…";
  try {
    const result = await api("/api/analysis/test", {
      method: "POST",
      body: JSON.stringify(analysisPayload()),
    });
    if (!result.reachable) {
      node.className = "instance-state err";
      node.textContent = result.detail || "unreachable";
      return;
    }
    node.className = "instance-state ok";
    node.textContent = `reachable · ${result.models.length} model(s)`;
    const list = $("an-models");
    list.replaceChildren();
    for (const model of result.models) {
      const option = el("option");
      option.value = model;
      list.append(option);
    }
  } catch (error) {
    node.className = "instance-state err";
    node.textContent = error.message;
  }
});

$("an-save").addEventListener("click", async () => {
  try {
    await api("/api/analysis", { method: "PUT", body: JSON.stringify(analysisPayload()) });
    toast("Analysis settings saved", "ok");
    await refreshAnalysisState();
    analysisDialog.close();
  } catch (error) {
    $("an-state").className = "instance-state err";
    $("an-state").textContent = error.message;
  }
});

function analysisStatus(text, kind = "") {
  const node = $("analysis-status");
  node.className = `analysis-status ${kind}`;
  node.textContent = text;
}

/** Jump the player to a [mm:ss] timestamp the model quoted. */
function seekToStamp(stamp) {
  const match = /(\d+):(\d+)/.exec(stamp || "");
  if (!match || video.hidden || !video.src) return;
  video.currentTime =
    Number(match[1]) * 60 + Number(match[2]) + (state.viewerOffset || 0);
  video.play().catch(() => {});
}

function renderSummary(result) {
  const out = $("analysis-out");
  out.replaceChildren();
  if (!result.summary && !(result.points || []).length) {
    out.append(el("p", "muted", "The model returned nothing for this transcript."));
    return;
  }
  out.append(el("h4", null, "Summary"));
  if (result.summary) out.append(el("p", null, result.summary));
  if ((result.points || []).length) {
    const list = el("ul");
    for (const point of result.points) list.append(el("li", null, point));
    out.append(list);
  }
  if (result.speakers) out.append(el("p", "muted", `Voices: ${result.speakers}`));
}

function renderReview(result) {
  const out = $("analysis-out");
  out.replaceChildren();
  out.append(el("h4", null, "Review"));
  if (result.assessment) out.append(el("p", null, result.assessment));

  const flags = result.flags || [];
  if (!flags.length) {
    out.append(el("p", "muted", "Nothing flagged."));
    return;
  }
  for (const flag of flags) {
    const node = el("div", `flag ${flag.severity}`);
    const head = el("div", "flag-head");
    head.append(el("span", "sev", flag.severity));
    head.append(el("span", null, flag.category));
    if (flag.timestamp) {
      const stamp = el("time", null, flag.timestamp);
      stamp.addEventListener("click", () => seekToStamp(flag.timestamp));
      head.append(stamp);
    }
    node.append(head);
    if (flag.quote) node.append(el("div", "flag-quote", `“${flag.quote}”`));
    if (flag.reason) node.append(el("div", "flag-why", flag.reason));
    out.append(node);
  }
  out.append(
    el("p", "muted", "Flags come from an ASR transcript and can be wrong — check the audio.")
  );
}

async function runAnalysis(kind) {
  if (!state.viewerJob) return;
  const label = kind === "summarize" ? "Summarising" : "Reviewing";
  analysisStatus(`${label}…`, "busy");
  for (const id of ["do-summarize", "do-review"]) $(id).disabled = true;
  try {
    const result = await api(`/api/jobs/${state.viewerJob}/${kind}`, { method: "POST" });
    analysisStatus("");
    if (kind === "summarize") renderSummary(result);
    else renderReview(result);
  } catch (error) {
    analysisStatus(error.message, "err");
  } finally {
    for (const id of ["do-summarize", "do-review"]) $(id).disabled = false;
  }
}

$("do-summarize").addEventListener("click", () => runAnalysis("summarize"));
$("do-review").addEventListener("click", () => runAnalysis("review"));

$("ask-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const question = $("ask-input").value.trim();
  if (!question || !state.viewerJob) return;
  const out = $("ask-out");
  out.replaceChildren(el("p", "muted", "Thinking…"));
  try {
    const result = await api(`/api/jobs/${state.viewerJob}/ask`, {
      method: "POST",
      body: JSON.stringify({ question }),
    });
    out.replaceChildren();
    out.append(el("h4", null, question));
    out.append(el("p", null, result.answer || "No answer."));
  } catch (error) {
    out.replaceChildren(el("p", "muted", error.message));
  }
});

// --------------------------------------------------------------------------- //
// wiring
// --------------------------------------------------------------------------- //

$("reload-cameras").addEventListener("click", loadCameras);
$("pill-whisper").addEventListener("click", async () => {
  const states = await refreshWhisperStatus(true);
  // Nothing healthy? The fix is almost always in the instance settings.
  if (states && states.healthy === 0) openInstances();
});
$("pill-protect").addEventListener("click", refreshProtectStatus);
$("camera-filter").addEventListener("input", (event) => {
  state.cameraFilter = event.target.value;
  renderCameras();
});
$("range-start").addEventListener("change", readRangeInputs);
$("range-end").addEventListener("change", readRangeInputs);
$("day-picker").addEventListener("change", (event) => {
  const date = fromLocalInput(`${event.target.value}T00:00:00`);
  if (date) setDay(date);
});
$("day-prev").addEventListener("click", () => {
  const date = new Date(state.day);
  date.setDate(date.getDate() - 1);
  setDay(date);
});
$("day-next").addEventListener("click", () => {
  const date = new Date(state.day);
  date.setDate(date.getDate() + 1);
  setDay(date);
});
$("presets").addEventListener("click", (event) => {
  const minutes = Number(event.target.dataset.minutes);
  if (!minutes) return;
  const end = new Date();
  const start = new Date(end.getTime() - minutes * 60000);
  setDay(new Date(end));
  setRange(start, end);
});
$("submit-job").addEventListener("click", submitJob);
$("upload-input").addEventListener("change", (event) => {
  const [file] = event.target.files;
  if (file) uploadClip(file);
  event.target.value = "";
});

let searchTimer = null;
$("job-search").addEventListener("input", (event) => {
  clearTimeout(searchTimer);
  const value = event.target.value;
  searchTimer = setTimeout(() => runSearch(value), 250);
});

window.addEventListener("resize", () => {
  resizeCanvas();
  if (!$("preview-panel").hidden) resizeWaveform();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.metaKey || event.ctrlKey) && !$("submit-job").disabled) {
    submitJob();
  }
});

async function init() {
  try {
    state.config = { ...state.config, ...(await api("/api/config")) };
    if (state.config.defaultLanguage) $("opt-language").value = state.config.defaultLanguage;
    if (state.config.defaultTask) $("opt-task").value = state.config.defaultTask;
  } catch (error) {
    if (/token/i.test(error.message)) {
      toast("This instance needs an app token. Open it as /?token=YOUR_TOKEN", "err");
    }
  }
  setDay(new Date());
  // Default selection: the last 15 minutes, which is the common case.
  const now = new Date();
  setRange(new Date(now.getTime() - 15 * 60000), now);
  resizeCanvas();
  await Promise.all([
    loadCameras(),
    loadJobs(),
    refreshProtectStatus(),
    refreshWhisperStatus(),
    refreshAnalysisState(),
  ]);
  connectStream();
  // Keep the "now" needle and instance load roughly current.
  setInterval(drawTimeline, 30000);
  setInterval(() => refreshWhisperStatus(), 60000);
}

init();
