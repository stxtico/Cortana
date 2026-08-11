// renderer.ts - plain DOM, no framework (matches this project's pattern
// elsewhere of skipping heavy abstractions until scale needs them - agent.py's
// hand-rolled dispatch loop instead of LangChain, memory's three deterministic
// steps instead of Letta). A debug/instrumentation utility window with five
// small panels doesn't need React.

const MAX_ROWS = 500;

function hexToRgb(hex: string): string {
  const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex);
  if (!m) return "57, 230, 255";
  return `${parseInt(m[1], 16)}, ${parseInt(m[2], 16)}, ${parseInt(m[3], 16)}`;
}

async function applyUiConfig(): Promise<void> {
  const cfg = await window.cortana.getUiConfig();
  const root = document.documentElement.style;
  root.setProperty("--panel-opacity", String(cfg.panel_opacity));
  root.setProperty("--blur-px", `${cfg.blur_px}px`);
  root.setProperty("--accent", cfg.accent);
  root.setProperty("--accent-rgb", hexToRgb(cfg.accent));
}
applyUiConfig();

// ---- title bar: frameless window chrome, built here since frame:false
// means Electron gives us no OS controls at all (PROMPTS.md A12) ----
document.getElementById("btn-min")!.addEventListener("click", () => window.cortana.windowMinimize());
document.getElementById("btn-max")!.addEventListener("click", () => window.cortana.windowMaximizeToggle());
document.getElementById("btn-close")!.addEventListener("click", () => window.cortana.windowClose());
window.cortana.onWindowState((state) => {
  const btn = document.getElementById("btn-max")!;
  btn.textContent = state.maximized ? "❐" : "□";
  btn.title = state.maximized ? "Restore" : "Maximize";
});

// ---- tabs ----
document.querySelectorAll<HTMLButtonElement>(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById(`tab-${btn.dataset.tab}`)!.classList.add("active");
    if (btn.dataset.tab === "memory" && !memoryLoaded) loadMemorySessions();
  });
});

function appendRow(container: HTMLElement, el: HTMLElement): void {
  container.appendChild(el);
  while (container.children.length > MAX_ROWS) container.removeChild(container.firstChild!);
  container.scrollTop = container.scrollHeight;
}

function fmtTime(ts: unknown): string {
  try {
    return new Date(ts as string).toLocaleTimeString();
  } catch {
    return typeof ts === "string" ? ts : "";
  }
}

// ---- conversation tab ----
const conversationList = document.getElementById("conversation-list")!;

function handleLoopEvent(record: Record<string, unknown>): void {
  if (record.stage === "user_turn" || record.stage === "assistant_turn") {
    const role = record.stage === "user_turn" ? "user" : "assistant";
    const div = document.createElement("div");
    div.className = `turn ${role}`;
    const meta = document.createElement("div");
    meta.className = "turn-meta";
    meta.textContent = `${role} — ${fmtTime(record.timestamp)}`;
    const text = document.createElement("div");
    text.textContent = (record.text as string) || "";
    div.appendChild(meta);
    div.appendChild(text);
    appendRow(conversationList, div);
  }
}

// ---- tools / activity tab (agent.jsonl tool calls + daemon.jsonl decisions) ----
const toolsList = document.getElementById("tools-list")!;
const pendingTimersEl = document.getElementById("pending-timers")!;

function handleAgentEvent(record: Record<string, unknown>): void {
  const row = document.createElement("div");
  row.className = "event-row";
  const ts = `<span class="ts">${fmtTime(record.timestamp)}</span>`;
  const src = `<span class="src">agent</span>`;
  let body: string;
  switch (record.stage) {
    case "tool_call": {
      const ok = record.ok as boolean;
      body = `<span class="${ok ? "ok" : "fail"}">${ok ? "OK" : "FAIL"}</span> ${record.tool}(${JSON.stringify(
        record.arguments
      )}) — ${record.duration_ms}ms, ${record.result_chars} chars${record.truncated ? " (truncated)" : ""}`;
      break;
    }
    case "tool_unavailable":
      body = `<span class="fail">unavailable</span> ${record.tool}`;
      break;
    case "credential_refused":
      body = `<span class="fail">refused</span> ${record.tool}: ${record.reason}`;
      break;
    case "ask_user_cap":
      body = `<span class="fail">ask_user capped</span> (count ${record.count})`;
      break;
    case "iteration_cap_hit":
      body = `<span class="fail">iteration cap hit</span> (max ${record.max_iterations})`;
      break;
    default:
      body = JSON.stringify(record);
  }
  row.innerHTML = ts + src + body;
  appendRow(toolsList, row);
}

function handleDaemonEvent(record: Record<string, unknown>): void {
  if (record.stage !== "announced" && record.stage !== "suppressed") return;
  const row = document.createElement("div");
  row.className = "event-row daemon";
  const ts = `<span class="ts">${fmtTime(record.timestamp)}</span>`;
  const src = `<span class="src">daemon</span>`;
  const body =
    record.stage === "announced"
      ? `<span class="ok">announced</span> ${record.summary}`
      : `<span class="fail">suppressed</span> (${record.reason}) ${record.summary}`;
  row.innerHTML = ts + src + body;
  appendRow(toolsList, row);
}

window.cortana.onTimersUpdate((timers) => {
  pendingTimersEl.innerHTML = "";
  for (const t of timers) {
    const chip = document.createElement("span");
    chip.className = "timer-chip";
    const secsLeft = Math.max(0, Math.round(t.fire_at - Date.now() / 1000));
    chip.textContent = `${t.label} — ${secsLeft}s`;
    pendingTimersEl.appendChild(chip);
  }
});

// ---- latency tab ----
const latencyDerived = document.getElementById("latency-derived")!;
const latencyCards = document.getElementById("latency-cards")!;
const latencyList = document.getElementById("latency-list")!;

interface StageStats {
  name: string;
  target_ms: number | null;
  status: string;
  n: number;
  median: number | null;
  p95: number | null;
  max: number | null;
}
interface LatencyReport {
  critical_path: StageStats[];
  first_audio_out: { total_ms: number; target_ms: number; status: string } | null;
  first_audio_out_missing: string[] | null;
}

window.cortana.onLatencyUpdate((payload) => {
  const result = payload as { ok: boolean; data?: LatencyReport; error?: string };
  if (!result.ok || !result.data) {
    latencyDerived.innerHTML = `<div class="hint">${result.error || "no data yet"}</div>`;
    return;
  }
  const report = result.data;

  if (report.first_audio_out) {
    const fao = report.first_audio_out;
    const cls = fao.status === "OK" ? "ok" : "over";
    latencyDerived.innerHTML =
      `<div class="big ${cls}">${fao.total_ms.toFixed(0)}ms</div>` +
      `<div class="sub">First audio out (derived, corrected for TTS/LLM double-counting) ` +
      `— target ${fao.target_ms}ms — ${fao.status}</div>`;
  } else {
    const missing = report.first_audio_out_missing || [];
    latencyDerived.innerHTML = `<div class="sub">Not enough data yet for a derived total — waiting on: ${missing.join(", ")}</div>`;
  }

  latencyCards.innerHTML = "";
  for (const s of report.critical_path) {
    const card = document.createElement("div");
    card.className = "card";
    const valueText = s.median === null ? "–" : `${s.median.toFixed(1)}ms`;
    card.innerHTML =
      `<div class="label">${s.name}</div>` +
      `<div class="value status-${s.status}">${valueText}</div>` +
      `<div class="target">target ${s.target_ms}ms — n=${s.n}</div>`;
    latencyCards.appendChild(card);
  }
});

function pushLatencyRow(source: string, stage: string, value: string): void {
  const row = document.createElement("div");
  row.className = "event-row";
  row.innerHTML = `<span class="ts">${fmtTime(new Date().toISOString())}</span><span class="src">${source}</span>${stage} = ${value}`;
  appendRow(latencyList, row);
}

function handleEarsEvent(record: Record<string, unknown>): void {
  const stage = record.stage as string;
  const latency = record.latency_ms as number | undefined;
  if (["wake", "verify", "vad", "stt", "backchannel"].includes(stage) && latency != null) {
    pushLatencyRow("ears", stage, `${latency.toFixed(1)}ms`);
  }
}

function handleBrainEvent(record: Record<string, unknown>): void {
  const ttft = record.ttft_ms as number | undefined;
  if (ttft != null) pushLatencyRow("brain", "ttft", `${ttft.toFixed(1)}ms`);
}

function handleVoiceEvent(record: Record<string, unknown>): void {
  const synth = record.synth_ms as number | undefined;
  if (record.stage === "sentence" && synth != null) pushLatencyRow("voice", "synth", `${synth.toFixed(1)}ms`);
  if (record.stage === "ttfc" && record.ttfc_ms != null) pushLatencyRow("voice", "ttfc(raw)", `${(record.ttfc_ms as number).toFixed(1)}ms`);
}

// ---- memory tab (real edit/delete, not just viewing - PROMPTS.md A12 point 4) ----
let memoryLoaded = false;
const memorySessionsEl = document.getElementById("memory-sessions")!;
const memoryEntriesEl = document.getElementById("memory-entries")!;
let currentSessionId: string | undefined;

async function loadMemorySessions(): Promise<void> {
  memoryLoaded = true;
  memorySessionsEl.innerHTML = "<div class='hint'>Loading...</div>";
  const result = await window.cortana.getMemorySessions();
  memorySessionsEl.innerHTML = "";
  if (!result.ok || !result.data) {
    memorySessionsEl.innerHTML = `<div class="hint">${result.error}</div>`;
    return;
  }
  if (result.data.length === 0) {
    memorySessionsEl.innerHTML = "<div class='hint'>No sessions recorded yet.</div>";
    return;
  }
  for (const s of result.data) {
    const row = document.createElement("div");
    row.className = "session-row";
    row.textContent = `${s.session_id} — ${s.started ? s.started.slice(0, 19) : ""} (${s.count})`;
    row.addEventListener("click", () => {
      document.querySelectorAll(".session-row").forEach((r) => r.classList.remove("selected"));
      row.classList.add("selected");
      currentSessionId = s.session_id;
      loadMemoryEntries(s.session_id);
    });
    memorySessionsEl.appendChild(row);
  }
}

function renderEntryRow(e: { id: number; role: string; text: string }): HTMLElement {
  const row = document.createElement("div");
  row.className = "entry-row";
  row.dataset.id = String(e.id);

  const view = document.createElement("div");
  view.className = "entry-view";
  view.innerHTML =
    `<span class="role">${e.role}</span>${escapeHtml((e.text || "").slice(0, 300))}` +
    `<span class="entry-actions">` +
    `<button class="edit-btn">edit</button>` +
    `<button class="danger delete-btn">delete</button>` +
    `</span>`;
  row.appendChild(view);

  view.querySelector(".edit-btn")!.addEventListener("click", (ev) => {
    ev.stopPropagation();
    openEditor(row, e);
  });
  view.querySelector(".delete-btn")!.addEventListener("click", async (ev) => {
    ev.stopPropagation();
    if (!window.confirm(`Delete entry ${e.id}? This can't be undone.`)) return;
    const result = await window.cortana.deleteMemoryEntry(e.id);
    if (!result.ok || !result.data?.ok) {
      window.alert(`Delete failed: ${result.error || "unknown error"}`);
      return;
    }
    row.remove();
  });

  return row;
}

function openEditor(row: HTMLElement, e: { id: number; text: string }): void {
  row.innerHTML = "";
  const wrap = document.createElement("div");
  wrap.className = "entry-edit";
  const textarea = document.createElement("textarea");
  textarea.value = e.text;
  const saveBtn = document.createElement("button");
  saveBtn.textContent = "save";
  saveBtn.className = "edit-btn";
  const cancelBtn = document.createElement("button");
  cancelBtn.textContent = "cancel";
  cancelBtn.className = "edit-btn";
  wrap.appendChild(textarea);
  wrap.appendChild(saveBtn);
  wrap.appendChild(cancelBtn);
  row.appendChild(wrap);
  textarea.focus();

  cancelBtn.addEventListener("click", () => {
    row.replaceWith(renderEntryRow({ id: e.id, role: (row.dataset.role as string) || "user", text: e.text }));
  });
  saveBtn.addEventListener("click", async () => {
    saveBtn.disabled = true;
    saveBtn.textContent = "saving...";
    const result = await window.cortana.editMemoryEntry(e.id, textarea.value);
    if (!result.ok || !result.data?.ok) {
      window.alert(`Save failed: ${result.error || "unknown error"}`);
      saveBtn.disabled = false;
      saveBtn.textContent = "save";
      return;
    }
    if (currentSessionId) loadMemoryEntries(currentSessionId);
  });
}

function escapeHtml(s: string): string {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}

async function loadMemoryEntries(sessionId: string): Promise<void> {
  memoryEntriesEl.innerHTML = "<div class='hint'>Loading...</div>";
  const result = await window.cortana.getMemoryEntries(sessionId);
  memoryEntriesEl.innerHTML = "";
  if (!result.ok || !result.data) {
    memoryEntriesEl.innerHTML = `<div class="hint">${result.error}</div>`;
    return;
  }
  for (const e of result.data) {
    memoryEntriesEl.appendChild(renderEntryRow(e));
  }
}

// ---- model badge ----
const modelBadge = document.getElementById("model-badge")!;
window.cortana.onModelStatus((status) => {
  if (!status.ok) {
    modelBadge.textContent = "MODEL: UNREACHABLE";
    modelBadge.className = "badge badge-unknown";
    return;
  }
  const models = status.models || [];
  if (models.length === 0) {
    modelBadge.textContent = "MODEL: IDLE (NONE RESIDENT)";
    modelBadge.className = "badge badge-idle";
    return;
  }
  modelBadge.textContent = `MODEL: ${models.map((m) => m.name).join(", ").toUpperCase()}`;
  modelBadge.className = "badge badge-active";
});

// ---- wiring ----
window.cortana.onLogEvent(({ source, record }) => {
  switch (source) {
    case "loop":
      handleLoopEvent(record);
      break;
    case "agent":
      handleAgentEvent(record);
      break;
    case "daemon":
      handleDaemonEvent(record);
      break;
    case "ears":
      handleEarsEvent(record);
      break;
    case "brain":
      handleBrainEvent(record);
      break;
    case "voice":
      handleVoiceEvent(record);
      break;
  }
});
