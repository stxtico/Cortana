// renderer.js - plain DOM, no framework (matches this project's "no heavy
// abstraction unless the scale needs it" pattern elsewhere - agent.py's
// hand-rolled dispatch loop instead of LangChain, memory's three
// deterministic steps instead of Letta). This is a debug utility window with
// five small panels, not an app that needs React.

const MAX_ROWS = 500;

// ---- tabs ----
document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById(`tab-${btn.dataset.tab}`).classList.add("active");
    if (btn.dataset.tab === "memory" && !memoryLoaded) loadMemorySessions();
  });
});

function appendRow(container, el) {
  container.appendChild(el);
  while (container.children.length > MAX_ROWS) container.removeChild(container.firstChild);
  container.scrollTop = container.scrollHeight;
}

function fmtTime(ts) {
  try {
    return new Date(ts).toLocaleTimeString();
  } catch (e) {
    return ts || "";
  }
}

// ---- conversation tab ----
const conversationList = document.getElementById("conversation-list");

function handleLoopEvent(record) {
  if (record.stage === "user_turn" || record.stage === "assistant_turn") {
    const role = record.stage === "user_turn" ? "user" : "assistant";
    const div = document.createElement("div");
    div.className = `turn ${role}`;
    const meta = document.createElement("div");
    meta.className = "turn-meta";
    meta.textContent = `${role} - ${fmtTime(record.timestamp)}`;
    const text = document.createElement("div");
    text.textContent = record.text || "";
    div.appendChild(meta);
    div.appendChild(text);
    appendRow(conversationList, div);
  }
}

// ---- tools tab ----
const toolsList = document.getElementById("tools-list");

function handleAgentEvent(record) {
  const row = document.createElement("div");
  row.className = "event-row";
  const ts = `<span class="ts">${fmtTime(record.timestamp)}</span>`;
  const src = `<span class="src">agent</span>`;
  let body;
  switch (record.stage) {
    case "tool_call": {
      const cls = record.ok ? "ok" : "fail";
      body = `<span class="${cls}">${record.ok ? "OK" : "FAIL"}</span> ${record.tool}(${JSON.stringify(
        record.arguments
      )}) - ${record.duration_ms}ms, ${record.result_chars} chars${record.truncated ? " (truncated)" : ""}`;
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

// ---- latency tab ----
const latencyCards = document.getElementById("latency-cards");
const latencyList = document.getElementById("latency-list");
const cardValues = {
  wake: null, verify: null, vad: null, stt: null,
  ttft: null, tokps: null, tts_synth: null,
};
const CARD_LABELS = {
  wake: "Wake (ms)", verify: "Verify (ms)", vad: "VAD (ms)", stt: "STT (ms)",
  ttft: "LLM TTFT (ms)", tokps: "LLM tok/s", tts_synth: "TTS synth (ms)",
};

function renderCards() {
  latencyCards.innerHTML = "";
  for (const [key, label] of Object.entries(CARD_LABELS)) {
    const card = document.createElement("div");
    card.className = "card";
    const val = cardValues[key];
    card.innerHTML = `<div class="label">${label}</div><div class="value">${val === null ? "-" : val}</div>`;
    latencyCards.appendChild(card);
  }
}
renderCards();

function pushLatencyRow(source, stage, value) {
  const row = document.createElement("div");
  row.className = "event-row";
  row.innerHTML = `<span class="ts">${fmtTime(new Date().toISOString())}</span><span class="src">${source}</span>${stage} = ${value}`;
  appendRow(latencyList, row);
}

function handleEarsEvent(record) {
  if (["wake", "verify", "vad", "stt"].includes(record.stage) && record.latency_ms != null) {
    cardValues[record.stage] = record.latency_ms.toFixed(1);
    renderCards();
    pushLatencyRow("ears", record.stage, record.latency_ms.toFixed(1) + "ms");
  }
}

function handleBrainEvent(record) {
  if (record.ttft_ms != null) {
    cardValues.ttft = record.ttft_ms.toFixed(1);
    cardValues.tokps = record.tokens_per_sec != null ? record.tokens_per_sec.toFixed(1) : cardValues.tokps;
    renderCards();
    pushLatencyRow("brain", "ttft", record.ttft_ms.toFixed(1) + "ms");
  }
}

function handleVoiceEvent(record) {
  if (record.stage === "sentence" && record.synth_ms != null) {
    cardValues.tts_synth = record.synth_ms.toFixed(1);
    renderCards();
    pushLatencyRow("voice", "synth", record.synth_ms.toFixed(1) + "ms");
  }
}

// ---- memory tab ----
let memoryLoaded = false;
const memorySessions = document.getElementById("memory-sessions");
const memoryEntries = document.getElementById("memory-entries");

async function loadMemorySessions() {
  memoryLoaded = true;
  memorySessions.innerHTML = "<div class='hint'>Loading...</div>";
  const result = await window.cortana.getMemorySessions();
  memorySessions.innerHTML = "";
  if (!result.ok) {
    memorySessions.innerHTML = `<div class="hint">${result.error}</div>`;
    return;
  }
  if (result.data.length === 0) {
    memorySessions.innerHTML = "<div class='hint'>No sessions recorded yet.</div>";
    return;
  }
  for (const s of result.data) {
    const row = document.createElement("div");
    row.className = "session-row";
    row.textContent = `${s.session_id} - ${s.started ? s.started.slice(0, 19) : ""} (${s.count})`;
    row.addEventListener("click", () => {
      document.querySelectorAll(".session-row").forEach((r) => r.classList.remove("selected"));
      row.classList.add("selected");
      loadMemoryEntries(s.session_id);
    });
    memorySessions.appendChild(row);
  }
}

async function loadMemoryEntries(sessionId) {
  memoryEntries.innerHTML = "<div class='hint'>Loading...</div>";
  const result = await window.cortana.getMemoryEntries(sessionId);
  memoryEntries.innerHTML = "";
  if (!result.ok) {
    memoryEntries.innerHTML = `<div class="hint">${result.error}</div>`;
    return;
  }
  for (const e of result.data) {
    const row = document.createElement("div");
    row.className = "entry-row";
    row.innerHTML = `<span class="role">${e.role}</span>${(e.text || "").slice(0, 200)}`;
    memoryEntries.appendChild(row);
  }
}

// ---- model badge ----
const modelBadge = document.getElementById("model-badge");
window.cortana.onModelStatus((status) => {
  if (!status.ok) {
    modelBadge.textContent = "model: unreachable";
    modelBadge.className = "badge badge-unknown";
    return;
  }
  if (status.models.length === 0) {
    modelBadge.textContent = "model: idle (none resident)";
    modelBadge.className = "badge badge-idle";
    return;
  }
  const names = status.models.map((m) => m.name).join(", ");
  modelBadge.textContent = `model: ${names}`;
  modelBadge.className = "badge badge-active";
});

// ---- wiring ----
window.cortana.onLogEvent(({ source, record }) => {
  switch (source) {
    case "loop": handleLoopEvent(record); break;
    case "agent": handleAgentEvent(record); break;
    case "ears": handleEarsEvent(record); break;
    case "brain": handleBrainEvent(record); break;
    case "voice": handleVoiceEvent(record); break;
  }
});
