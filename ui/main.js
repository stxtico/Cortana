// main.js - Electron main process (PROMPTS.md A12). Utility window, not the
// character (that's Phase 8/A15) - function over polish, this is where the
// rest of the system gets debugged from.
//
// Data plumbing, deliberately reusing what already exists instead of adding a
// new transport:
//   - Conversation/tools/latency panels tail the same JSONL logs every Python
//     service already writes (logs/*.jsonl) - no WebSocket/HTTP server on the
//     Python side, same "plain file, not a socket" choice
//     services/voice/playback_state.py already made for cross-process
//     coordination. See log_tail.js.
//   - Memory tab shells out to `uv run scripts/memory.py ... --json` (the
//     --json flag added alongside this UI) rather than reading the sqlite-vec
//     store directly from Node - one real implementation of "what's in
//     memory," not a second one reimplemented in JS.
//   - Active-model indicator polls Ollama's own /api/ps directly - the real
//     source of truth for what's actually resident, not a static config
//     value.

const { app, BrowserWindow, ipcMain } = require("electron");
const path = require("path");
const { execFile } = require("child_process");
const { readLastLines, JsonlTailer } = require("./log_tail");

const ROOT = path.join(__dirname, "..");
const OLLAMA_ENDPOINT = "http://localhost:11434";
const POLL_INTERVAL_MS = 500;
const MODEL_POLL_INTERVAL_MS = 5000;
const HISTORY_LINES = 500;

// source name -> path relative to ROOT. daemon.jsonl deliberately not
// included - A12's spec is conversation/tools/latency/memory/model-active,
// daemon activity wasn't asked for and adding it would be scope creep past
// what was requested.
const LOG_FILES = {
  loop: "logs/loop.jsonl",
  agent: "logs/agent.jsonl",
  ears: "logs/ears.jsonl",
  brain: "logs/brain.jsonl",
  voice: "logs/voice.jsonl",
};

function createWindow() {
  const win = new BrowserWindow({
    width: 900,
    height: 700,
    title: "Cortana - Control Panel",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  win.loadFile(path.join(__dirname, "index.html"));
  startLogTailing(win);
  startModelPolling(win);
}

function startLogTailing(win) {
  const tailers = [];
  for (const [source, relPath] of Object.entries(LOG_FILES)) {
    const filePath = path.join(ROOT, relPath);
    for (const record of readLastLines(filePath, HISTORY_LINES)) {
      win.webContents.send("log-event", { source, record });
    }
    tailers.push(
      new JsonlTailer(filePath, (record) => {
        win.webContents.send("log-event", { source, record });
      })
    );
  }
  const interval = setInterval(() => {
    for (const t of tailers) t.poll();
  }, POLL_INTERVAL_MS);
  win.on("closed", () => clearInterval(interval));
}

function startModelPolling(win) {
  const poll = async () => {
    try {
      const res = await fetch(`${OLLAMA_ENDPOINT}/api/ps`, { signal: AbortSignal.timeout(2000) });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      win.webContents.send("model-status", { ok: true, models: data.models || [] });
    } catch (e) {
      win.webContents.send("model-status", { ok: false, error: String(e.message || e) });
    }
  };
  poll();
  const interval = setInterval(poll, MODEL_POLL_INTERVAL_MS);
  win.on("closed", () => clearInterval(interval));
}

// Shells out to the real memory inspector CLI (scripts/memory.py, PROMPTS.md
// A7) rather than talking to the sqlite-vec store directly - see module
// docstring above.
function runMemoryCli(args) {
  return new Promise((resolve) => {
    execFile(
      "uv",
      ["run", "scripts/memory.py", ...args],
      { cwd: ROOT, timeout: 10000 },
      (err, stdout, stderr) => {
        if (err) {
          resolve({ ok: false, error: (stderr || err.message).trim() });
          return;
        }
        try {
          resolve({ ok: true, data: JSON.parse(stdout) });
        } catch (e) {
          resolve({ ok: false, error: `bad JSON from memory.py: ${e.message}` });
        }
      }
    );
  });
}

ipcMain.handle("memory:sessions", () => runMemoryCli(["sessions", "--json"]));
ipcMain.handle("memory:list", (event, sessionId) => {
  const args = ["list", "--json", "--limit", "200"];
  if (sessionId) args.push("--session", sessionId);
  return runMemoryCli(args);
});

app.whenReady().then(createWindow);

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
