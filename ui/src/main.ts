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
//
// Launcher (later addition): this file is also now the thing that starts
// Cortana - the Electron app supervises services/brain/loop.py and
// services/daemon/daemon.py, not the other way around, and not a separate
// third process. Decided this way over the alternatives, explicitly:
//
// electron-builder vs a plain launcher script compiled to exe: neither, in
// the end - electron-builder packages the Electron/Node side into a
// redistributable installer (asar-bundled, Program Files, an uninstaller),
// which fights this project's actual shape: config/cortana.toml is hand-
// edited constantly (CLAUDE.md rule 2), logs/ and the memory store are live
// data next to the code, and the Python side is a real uv-managed venv, not
// something that bundles into an installer at all. Chasing electron-builder
// here would mean fighting to keep ROOT pointed at the real checkout instead
// of inside a packaged bundle, for a single-user, single-machine tool that
// was never going to be redistributed. A *separate* plain launcher script
// (Python or otherwise) was the other alternative - rejected because it
// would duplicate what Electron already does: main.ts already shells out to
// `uv run python` for config reads (loadUiConfig() below, long before this
// comment existed), already owns a real OS-level surface (windows, and now
// a Tray icon - see createTray()), and starting a *second* resident
// supervisor process just to launch a *third* would be an extra moving part
// for no benefit. The actual "executable you run on startup" is simply
// Electron itself, pointed at this already-built app - see the Startup-
// folder registration note in docs/history/ for the exact command.
//
// How the Python side gets invoked: spawnPythonServices() below, `uv run
// python -m services.brain.loop` / `-m services.daemon.daemon`, cwd=ROOT,
// bare "uv" on PATH (the same resolution loadUiConfig() already proves
// works from this exact process every session - no separate absolute-path
// lookup invented for the long-lived spawns).
//
// Ollama / missing-model handling: checkOllamaAndModel() is informational
// only and never blocks spawning the services - Ollama lazy-loads a model on
// first real request regardless, and the control panel's existing model-
// status poll (startModelPolling()) already shows live reachability once
// the window is up. What that poll can't tell you - "is [models].primary
// even pulled at all," as opposed to "not currently resident" (normal at
// cold start) - is the one thing this adds, via a one-time /api/tags check,
// surfaced as a native OS notification rather than a blocking dialog.
//
// How you stop it: the Tray icon's "Quit Cortana" - the only normal way to
// stop everything. Closing the control panel or character window does NOT
// kill the backend anymore (see the removed window-all-closed handler at
// the bottom of this file) - that's the entire point of running this at
// login instead of a terminal you'd otherwise have to keep open.
import { app, BrowserWindow, ipcMain, Tray, Menu, nativeImage, Notification } from "electron";
import * as path from "path";
import * as fs from "fs";
import { execFileSync, spawn, ChildProcess } from "child_process";
import { readLastLines, JsonlTailer, JsonRecord } from "./log_tail";
import { runPython } from "./py_bridge";
import { createCharacterWindow, startCharacterFeatures } from "./character_main";

const ROOT = path.join(__dirname, "..", "..");
// Fallback only - overwritten from [models].endpoint the instant
// loadModelsConfig() runs in whenReady(), same "read from config, this is
// just the pre-config-load default" reasoning as loadUiConfig()'s defaults
// object. Was a hardcoded const before the launcher work - main.ts is now
// also the thing that has to know whether Ollama is actually reachable
// before spawning services/brain/loop.py, not just poll it for the badge.
let OLLAMA_ENDPOINT = "http://localhost:11434";
const BACKSTOP_POLL_MS = 700;
const MODEL_POLL_INTERVAL_MS = 5000;
const HISTORY_LINES = 500;
const LATENCY_DEBOUNCE_MS = 600;
const LAUNCHER_LOG = path.join(ROOT, "logs", "launcher.jsonl");

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

// Same JSON-lines-to-logs/, one-file-per-service discipline every Python
// service already follows (CLAUDE.md rule 3) - main.ts is now a real
// service in the "starts/stops other processes" sense, not just a UI, so it
// gets the same instrumentation rather than being the one component that
// only prints to a console nobody's watching at login.
function logLauncher(record: Record<string, unknown>): void {
  fs.mkdirSync(path.dirname(LAUNCHER_LOG), { recursive: true });
  fs.appendFileSync(LAUNCHER_LOG, JSON.stringify({ timestamp: new Date().toISOString(), ...record }) + "\n");
}

interface ModelsConfig {
  endpoint: string;
  primary: string;
}

function loadModelsConfig(): ModelsConfig {
  const defaults: ModelsConfig = { endpoint: "http://localhost:11434", primary: "" };
  try {
    const code =
      "import tomllib,json; c=tomllib.load(open('config/cortana.toml','rb'))['models']; print(json.dumps({'endpoint': c['endpoint'], 'primary': c['primary']}))";
    const out = execFileSync("uv", ["run", "python", "-c", code], { cwd: ROOT, encoding: "utf8", timeout: 5000 });
    return { ...defaults, ...JSON.parse(out) };
  } catch {
    return defaults; // same "don't hard-fail startup over a config read" precedent as loadUiConfig()
  }
}

// ---- Python service supervision (launcher) ----
// Spawned via bare "uv" on PATH, not an absolute-resolved path - the exact
// same pattern loadUiConfig()'s execFileSync("uv", ...) already uses and
// already proves works from this Electron process every single session;
// introducing a second, different resolution strategy just for the
// long-lived spawns below would be inconsistency for no real benefit.
let loopProcess: ChildProcess | null = null;
let daemonProcess: ChildProcess | null = null;

function killProcessTree(proc: ChildProcess | null, label: string): void {
  if (!proc || proc.pid == null || proc.exitCode !== null) return;
  // Windows-native recursive kill (/T = tree, /F = force) - the same lesson
  // services/brain/agent_safety.py's terminate_process_tree() already
  // learned the hard way (CLAUDE.md's A21 entry): a bare process.kill() on
  // Windows only ever reaches the top-level PID, orphaning "uv run python"'s
  // real child (the actual python.exe), the exact process that matters.
  // This is a launcher with no terminal/job-object wrapping it at all
  // (login-started, not run from a shell) - there is no safety net to fall
  // back on if this isn't done explicitly.
  try {
    execFileSync("taskkill", ["/pid", String(proc.pid), "/T", "/F"]);
  } catch {
    // Already exited between the exitCode check and here, or taskkill
    // itself failed - either way, nothing more to do; don't crash the
    // launcher over a best-effort cleanup step.
  }
  logLauncher({ stage: "killed", process: label, pid: proc.pid });
}

function spawnPythonServices(): void {
  loopProcess = spawn("uv", ["run", "python", "-m", "services.brain.loop"], { cwd: ROOT, stdio: "ignore" });
  logLauncher({ stage: "spawned", process: "loop", pid: loopProcess.pid ?? null });
  loopProcess.on("exit", (code) => logLauncher({ stage: "exited", process: "loop", code }));

  daemonProcess = spawn("uv", ["run", "python", "-m", "services.daemon.daemon"], { cwd: ROOT, stdio: "ignore" });
  logLauncher({ stage: "spawned", process: "daemon", pid: daemonProcess.pid ?? null });
  daemonProcess.on("exit", (code) => logLauncher({ stage: "exited", process: "daemon", code }));

  // Realistic scope (explicit instruction): no auto-restart on crash. A
  // crashed loop/daemon process is logged above and the Tray tooltip (see
  // createTray()) will read "not running" on its next refresh - visible,
  // not silently swallowed - but this launcher supervises, it doesn't
  // self-heal. That's a real, deliberate scope boundary, not an oversight.
}

// Informational only, never blocks spawning the services below - Ollama
// lazy-loads a model on first real request regardless, and the control
// panel's existing model-status poll (startModelPolling()) already surfaces
// live reachability once the window is up. This is the one thing that poll
// can't tell you: whether [models].primary has actually been `ollama pull`ed
// at all, distinct from "not currently resident" (which is normal at cold
// start, not an error). /api/tags (installed models) is a different
// endpoint from /api/ps (currently-loaded models) for exactly this reason.
async function checkOllamaAndModel(models: ModelsConfig): Promise<void> {
  try {
    const res = await fetch(`${models.endpoint}/api/tags`, { signal: AbortSignal.timeout(3000) });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = (await res.json()) as { models?: Array<{ name: string }> };
    const installed = (data.models || []).map((m) => m.name);
    const pulled = installed.includes(models.primary);
    logLauncher({ stage: "ollama_check", ok: true, primary: models.primary, pulled, installed_count: installed.length });
    if (!pulled) {
      new Notification({
        title: "Cortana",
        body: `${models.primary} isn't pulled yet - run: ollama pull ${models.primary}`,
      }).show();
    }
  } catch (e) {
    const message = String((e as Error).message || e);
    logLauncher({ stage: "ollama_check", ok: false, error: message });
    new Notification({
      title: "Cortana",
      body: `Ollama isn't reachable at ${models.endpoint} - the voice loop will start anyway and retry on first use.`,
    }).show();
  }
}

function createTray(): Tray {
  const icon = nativeImage.createFromPath(path.join(ROOT, "ui", "assets", "tray-icon.ico"));
  const tray = new Tray(icon);
  tray.setToolTip("Cortana");
  const menu = Menu.buildFromTemplate([
    { label: "Cortana", enabled: false },
    { type: "separator" },
    {
      label: "Quit Cortana",
      click: () => {
        logLauncher({ stage: "tray_quit" });
        killProcessTree(loopProcess, "loop");
        killProcessTree(daemonProcess, "daemon");
        app.quit();
      },
    },
  ]);
  tray.setContextMenu(menu);
  return tray;
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
  logLauncher({ stage: "launcher_start" });

  const modelsConfig = loadModelsConfig();
  OLLAMA_ENDPOINT = modelsConfig.endpoint;
  void checkOllamaAndModel(modelsConfig); // async, non-blocking - see that function's own docstring for why

  spawnPythonServices();
  createTray();

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

// Deliberately no window-all-closed -> app.quit() anymore. This is now a
// tray-resident launcher (createTray() above) - closing the control panel
// and/or the character window should not take down services/brain/loop.py
// and services/daemon/daemon.py, which is the entire point of running this
// at login instead of a terminal. "Quit Cortana" in the tray menu is the
// only normal way to stop everything (see this file's docstring/CLAUDE.md
// for the full "how do I stop it" reasoning) - closing windows just closes
// windows, same as minimizing.

// Safety net, not the primary path: covers app.quit() being triggered some
// other way (e.g. an OS session-ending signal) without going through the
// tray Quit handler above, so the two Python children are never left
// orphaned by a code path that forgot to call killProcessTree().
app.on("before-quit", () => {
  killProcessTree(loopProcess, "loop");
  killProcessTree(daemonProcess, "daemon");
});
