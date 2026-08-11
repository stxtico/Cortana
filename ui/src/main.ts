// main.ts - Electron main process (PROMPTS.md A12). Utility window, not the
// character (that's Phase 8/A15) - function over polish, but the visual
// language (blue holographic, cyan accents, dark glass panels) is meant to
// read as the same system she'll eventually sit alongside.
//
// Transport, decided before writing any UI code (see the four points in the
// A12 prompt): no new HTTP/WebSocket server on the Python side. Every panel
// tails the JSONL logs every service already writes
// (logs/{loop,agent,ears,brain,voice,daemon}.jsonl) via log_tail.ts, which
// uses fs.watch() for a real push signal (Node is notified the instant a
// Python service appends a line) plus a slow backstop poll for whatever
// fs.watch misses. That's the "push channel" the live tool-call indicator
// needs, without adding a second transport alongside the file-based one
// services/voice/playback_state.py already established for daemon<->voice
// coordination (A11). The latency panel and memory tab both shell out to the
// real Python (latency_report.py --json, memory.py --json) rather than
// reimplementing the corrected latency math or the sqlite-vec store access
// in TypeScript - see py_bridge.ts.
import { app, BrowserWindow, ipcMain } from "electron";
import * as path from "path";
import * as fs from "fs";
import { execFileSync } from "child_process";
import { readLastLines, JsonlTailer, JsonRecord } from "./log_tail";
import { runPython } from "./py_bridge";
import { createCharacterWindow, startCharacterFeatures } from "./character_main";

const ROOT = path.join(__dirname, "..", "..");
const OLLAMA_ENDPOINT = "http://localhost:11434";
const BACKSTOP_POLL_MS = 700;
const MODEL_POLL_INTERVAL_MS = 5000;
const HISTORY_LINES = 500;
const LATENCY_DEBOUNCE_MS = 600;

// source name -> path relative to ROOT.
const LOG_FILES: Record<string, string> = {
  loop: "logs/loop.jsonl",
  agent: "logs/agent.jsonl",
  ears: "logs/ears.jsonl",
  brain: "logs/brain.jsonl",
  voice: "logs/voice.jsonl",
  daemon: "logs/daemon.jsonl",
};

// Stages whose new lines can move the corrected latency numbers - only these
// trigger a (debounced) latency_report.py re-run, so an idle daemon.jsonl or
// agent.jsonl tick doesn't spawn a Python process for no reason.
const LATENCY_SOURCES = new Set(["ears", "brain", "voice"]);

interface UiConfig {
  panel_opacity: number;
  window_opacity: number;
  blur_px: number;
  accent: string;
}

function loadUiConfig(): UiConfig {
  const defaults: UiConfig = { panel_opacity: 0.82, window_opacity: 1.0, blur_px: 20, accent: "#39e6ff" };
  try {
    // Reused source of truth, not a second TOML parser in JS - same pattern
    // ui's memory/latency panels use for the real thing (py_bridge.ts).
    const code =
      "import tomllib,json; print(json.dumps(tomllib.load(open('config/cortana.toml','rb')).get('ui', {})))";
    const out = execFileSync("uv", ["run", "python", "-c", code], { cwd: ROOT, encoding: "utf8", timeout: 5000 });
    return { ...defaults, ...JSON.parse(out) };
  } catch {
    return defaults; // startup shouldn't hard-fail over a config read - fall back to sane defaults
  }
}

function debounce(fn: () => void, ms: number): () => void {
  let timer: NodeJS.Timeout | null = null;
  return () => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(fn, ms);
  };
}

function createWindow(uiConfig: UiConfig): BrowserWindow {
  const win = new BrowserWindow({
    width: 960,
    height: 720,
    minWidth: 640,
    minHeight: 420,
    frame: false, // custom title bar built in renderer - see index.html/style.css
    transparent: true,
    backgroundColor: "#00000000",
    opacity: uiConfig.window_opacity, // whole-window compositor opacity - distinct from panel_opacity's CSS-level fade, see [ui] in cortana.toml
    resizable: true,
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  win.once("ready-to-show", () => win.show());
  win.loadFile(path.join(ROOT, "ui", "index.html"));

  win.on("maximize", () => win.webContents.send("window:state", { maximized: true }));
  win.on("unmaximize", () => win.webContents.send("window:state", { maximized: false }));

  ipcMain.on("window:minimize", () => win.minimize());
  ipcMain.on("window:maximize-toggle", () => (win.isMaximized() ? win.unmaximize() : win.maximize()));
  ipcMain.on("window:close", () => win.close());
  ipcMain.handle("window:get-ui-config", () => uiConfig);

  return win;
}

function startLogTailing(win: BrowserWindow): () => void {
  const tailers: JsonlTailer[] = [];
  const refreshLatency = debounce(() => sendLatencyUpdate(win), LATENCY_DEBOUNCE_MS);

  for (const [source, relPath] of Object.entries(LOG_FILES)) {
    const filePath = path.join(ROOT, relPath);
    for (const record of readLastLines(filePath, HISTORY_LINES)) {
      win.webContents.send("log-event", { source, record });
    }
    const tailer = new JsonlTailer(filePath, (record: JsonRecord) => {
      win.webContents.send("log-event", { source, record });
      if (LATENCY_SOURCES.has(source)) refreshLatency();
    });
    tailer.start(BACKSTOP_POLL_MS);
    tailers.push(tailer);
  }
  return () => tailers.forEach((t) => t.stop());
}

function startTimerWatch(win: BrowserWindow): () => void {
  // daemon_store/timers.json (PROMPTS.md A11) - not a JSONL log, a plain
  // JSON array store.py-style. Re-read whole on any change and push the
  // still-pending (unfired) ones - small file, no reason to diff it.
  const timersPath = path.join(ROOT, "daemon_store", "timers.json");

  const send = () => {
    let timers: Array<{ id: string; label: string; fire_at: number; fired: boolean }> = [];
    try {
      if (fs.existsSync(timersPath)) timers = JSON.parse(fs.readFileSync(timersPath, "utf8"));
    } catch {
      timers = [];
    }
    win.webContents.send("timers-update", timers.filter((t) => !t.fired));
  };
  send();

  const dir = path.dirname(timersPath);
  fs.mkdirSync(dir, { recursive: true });
  const watcher = fs.watch(dir, (_e, filename) => {
    if (filename === "timers.json") send();
  });
  const poll = setInterval(send, 2000);
  return () => {
    watcher.close();
    clearInterval(poll);
  };
}

function startModelPolling(win: BrowserWindow): () => void {
  const poll = async () => {
    try {
      const res = await fetch(`${OLLAMA_ENDPOINT}/api/ps`, { signal: AbortSignal.timeout(2000) });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as { models?: Array<{ name: string }> };
      win.webContents.send("model-status", { ok: true, models: data.models || [] });
    } catch (e) {
      win.webContents.send("model-status", { ok: false, error: String((e as Error).message || e) });
    }
  };
  poll();
  const interval = setInterval(poll, MODEL_POLL_INTERVAL_MS);
  return () => clearInterval(interval);
}

async function sendLatencyUpdate(win: BrowserWindow): Promise<void> {
  const result = await runPython(ROOT, "scripts/latency_report.py", ["--since", launchedAtIso, "--json"]);
  win.webContents.send("latency-update", result);
}

// Memory tab IPC - shells out to scripts/memory.py --json (real store
// operations, see that script and services/memory/store.py's update_passage()
// added alongside this UI). Delete always passes --yes: the confirmation
// step belongs in the renderer's own dialog, not a blocking terminal
// input() in a process with no real stdin (the same EOFError class of bug
// A10 already hit once with tools/shell.py + ask_user - see CLAUDE.md).
function registerMemoryIpc(): void {
  ipcMain.handle("memory:sessions", () => runPython(ROOT, "scripts/memory.py", ["sessions", "--json"]));
  ipcMain.handle("memory:list", (_event, sessionId?: string) => {
    const args = ["list", "--json", "--limit", "200"];
    if (sessionId) args.push("--session", sessionId);
    return runPython(ROOT, "scripts/memory.py", args);
  });
  ipcMain.handle("memory:edit", (_event, id: number, text: string) =>
    runPython(ROOT, "scripts/memory.py", ["edit", String(id), "--text", text, "--json"])
  );
  ipcMain.handle("memory:delete", (_event, id: number) =>
    runPython(ROOT, "scripts/memory.py", ["delete", String(id), "--yes", "--json"])
  );
}

let launchedAtIso = new Date().toISOString();

app.whenReady().then(() => {
  launchedAtIso = new Date().toISOString();
  const uiConfig = loadUiConfig();
  const win = createWindow(uiConfig);
  registerMemoryIpc();

  // Real race, caught live (not hypothetical): pushing IPC events right after
  // loadFile() fires them before the renderer's script has loaded far enough
  // to call ipcRenderer.on() - Electron's IPC doesn't queue for a listener
  // that isn't registered yet, so the entire initial history replay was
  // silently dropped on the floor. did-finish-load guarantees the page
  // (including its <script> tags) has actually loaded before anything is sent.
  win.webContents.once("did-finish-load", () => {
    const stopLogs = startLogTailing(win);
    const stopTimers = startTimerWatch(win);
    const stopModel = startModelPolling(win);
    void sendLatencyUpdate(win); // real historical data already exists on open - don't wait for the first live event to show it
    win.on("closed", () => {
      stopLogs();
      stopTimers();
      stopModel();
    });
  });

  // The character overlay (PROMPTS.md A15) - a second window in the same
  // Electron app, not a separate process. Same did-finish-load-before-any-IPC
  // discipline as the control panel window above, for the same reason.
  const charWin = createCharacterWindow(ROOT);
  charWin.webContents.once("did-finish-load", () => {
    const stopCharacterFeatures = startCharacterFeatures(charWin, ROOT);
    charWin.on("closed", stopCharacterFeatures);
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
