// character_main.ts - main-process side of the character overlay
// (PROMPTS.md A15). Kept separate from main.ts (which owns the control
// panel window) - two windows, one Electron app, same split-by-concern
// pattern as log_tail.ts/py_bridge.ts.
import { BrowserWindow, Rectangle, ipcMain, screen, globalShortcut } from "electron";
import * as fs from "fs";
import * as path from "path";
import { execFileSync } from "child_process";

const CURSOR_POLL_MS = 33; // ~30fps - smooth enough for eye tracking, cheap enough to poll forever
const AMPLITUDE_BACKSTOP_MS = 150;
const WALK_SPEED_PX_S = 500;
const WALK_STEP_MS = 16;
const WINDOW_WIDTH = 320;
const WINDOW_HEIGHT = 480;
const EDGE_MARGIN = 40;
const WALK_HOTKEY = "Alt+Shift+W"; // manual trigger - real backend triggers for *when* to walk are a later integration step, same deferral pattern as A8/A9/A10's voice-answer paths

interface HologramConfig {
  enabled: boolean;
  character_opacity: number;
  scanline_density: number;
  scanline_opacity: number;
  drift_speed: number;
  data_texture_opacity: number;
  data_texture_mode: string;
  data_texture_column_width: number;
  data_texture_fall_speed: number;
  data_texture_glyph_swap_rate: number;
  data_texture_trail_length: number;
  tint_color: string;
  tint_strength: number;
  rim_color: string;
  rim_intensity: number;
  rim_width: number;
  chromatic_offset: number;
}

const HOLOGRAM_DEFAULTS: HologramConfig = {
  enabled: true,
  character_opacity: 0.82,
  scanline_density: 220.0,
  scanline_opacity: 0.35,
  drift_speed: 0.6,
  data_texture_opacity: 0.4,
  data_texture_mode: "multiply",
  data_texture_column_width: 20.0,
  data_texture_fall_speed: 2.5,
  data_texture_glyph_swap_rate: 3.0,
  data_texture_trail_length: 6.0,
  tint_color: "#39e6ff",
  tint_strength: 0.22,
  rim_color: "#39e6ff",
  rim_intensity: 0.9,
  rim_width: 3.0,
  chromatic_offset: 1.5,
};

// Same "shell out to real Python tomllib, don't hand-roll a second TOML
// parser in JS" pattern main.ts's loadUiConfig() already established -
// [ui.hologram] is this window's own config table, read independently since
// character_main.ts doesn't import from main.ts.
function loadHologramConfig(root: string): HologramConfig {
  try {
    const code =
      "import tomllib,json; d=tomllib.load(open('config/cortana.toml','rb')); print(json.dumps(d.get('ui', {}).get('hologram', {})))";
    const out = execFileSync("uv", ["run", "python", "-c", code], { cwd: root, encoding: "utf8", timeout: 5000 });
    return { ...HOLOGRAM_DEFAULTS, ...JSON.parse(out) };
  } catch {
    return HOLOGRAM_DEFAULTS; // startup shouldn't hard-fail over a config read - fall back to sane defaults
  }
}

interface WanderConfig {
  enabled: boolean;
  interval_min_s: number;
  interval_max_s: number;
  distance_min_px: number;
  distance_max_px: number;
}

const WANDER_DEFAULTS: WanderConfig = {
  enabled: true,
  interval_min_s: 120.0,
  interval_max_s: 360.0,
  distance_min_px: 60.0,
  distance_max_px: 220.0,
};

// Same shell-out pattern as loadHologramConfig() - [ui.wander] is its own
// table, read independently.
function loadWanderConfig(root: string): WanderConfig {
  try {
    const code =
      "import tomllib,json; d=tomllib.load(open('config/cortana.toml','rb')); print(json.dumps(d.get('ui', {}).get('wander', {})))";
    const out = execFileSync("uv", ["run", "python", "-c", code], { cwd: root, encoding: "utf8", timeout: 5000 });
    return { ...WANDER_DEFAULTS, ...JSON.parse(out) };
  } catch {
    return WANDER_DEFAULTS;
  }
}

function defaultPosition(display: Electron.Display): { x: number; y: number } {
  return {
    x: Math.round(display.workArea.x + display.workArea.width - WINDOW_WIDTH - EDGE_MARGIN),
    y: Math.round(display.workArea.y + display.workArea.height - WINDOW_HEIGHT),
  };
}

export function createCharacterWindow(root: string): BrowserWindow {
  const { x, y } = defaultPosition(screen.getPrimaryDisplay());
  const win = new BrowserWindow({
    width: WINDOW_WIDTH,
    height: WINDOW_HEIGHT,
    x,
    y,
    transparent: true,
    backgroundColor: "#00000000",
    frame: false,
    alwaysOnTop: true,
    skipTaskbar: true, // she's an overlay, not a taskbar app
    resizable: false,
    hasShadow: false,
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "character_preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  win.setAlwaysOnTop(true, "screen-saver");
  // Click-through by default (PLAN.md - "without this she blocks clicks on
  // whatever is behind her and you will hate it within an hour"). forward:true
  // is what lets mousemove events still reach the renderer despite clicks
  // passing through - required for the hover-to-toggle mechanism below.
  win.setIgnoreMouseEvents(true, { forward: true });
  win.once("ready-to-show", () => win.show());

  // Holographic overlay config ([ui.hologram]) - read once at startup, same
  // as main.ts's [ui] read for the control panel, handed to the renderer via
  // one IPC round trip rather than re-shelling-out per request.
  const hologramConfig = loadHologramConfig(root);
  ipcMain.handle("character:get-hologram-config", () => hologramConfig);

  win.loadFile(path.join(root, "ui", "character.html"));
  return win;
}

function rectsOverlap(a: Rectangle, b: Rectangle): boolean {
  return a.x < b.x + b.width && a.x + a.width > b.x && a.y < b.y + b.height && a.y + a.height > b.y;
}

function animateX(win: BrowserWindow, fromX: number, toX: number, y: number): Promise<void> {
  return new Promise((resolve) => {
    if (win.isDestroyed()) {
      resolve();
      return;
    }
    const distance = Math.abs(toX - fromX);
    const durationMs = Math.max(150, (distance / WALK_SPEED_PX_S) * 1000);
    const start = Date.now();
    const step = () => {
      if (win.isDestroyed()) {
        resolve();
        return;
      }
      const t = Math.min(1, (Date.now() - start) / durationMs);
      win.setPosition(Math.round(fromX + (toX - fromX) * t), Math.round(y));
      if (t < 1) {
        setTimeout(step, WALK_STEP_MS);
      } else {
        resolve();
      }
    };
    step();
  });
}

// Walking between monitors (PLAN.md): animate to the current display's near
// edge, jump instantly to the target display's corresponding edge ("sell
// the [bezel] gap... rather than trying to render continuous motion"), then
// animate in from that edge to a resting position.
export async function walkToDisplay(win: BrowserWindow, targetDisplayId: number): Promise<void> {
  const target = screen.getAllDisplays().find((d) => d.id === targetDisplayId);
  if (!target) return;

  const bounds = win.getBounds();
  const currentDisplay = screen.getDisplayMatching(bounds);
  if (currentDisplay.id === target.id) return;

  const goingRight = target.workArea.x > currentDisplay.workArea.x;
  win.webContents.send("character:state", "walking");

  const exitX = goingRight
    ? currentDisplay.workArea.x + currentDisplay.workArea.width
    : currentDisplay.workArea.x - bounds.width;
  await animateX(win, bounds.x, exitX, bounds.y);

  const entryX = goingRight ? target.workArea.x - bounds.width : target.workArea.x + target.workArea.width;
  win.setPosition(Math.round(entryX), Math.round(bounds.y));

  const restX = goingRight
    ? target.workArea.x + EDGE_MARGIN
    : target.workArea.x + target.workArea.width - bounds.width - EDGE_MARGIN;
  await animateX(win, entryX, restX, bounds.y);

  win.webContents.send("character:state", "idle");
}

// Idle wandering ([ui.wander]): a short walk on the SAME monitor, not the
// cross-monitor jump walkToDisplay() does - reuses the same animateX()
// primitive and the same "walking" -> "idle" state bookend, just a single
// continuous move instead of the exit/jump/enter sequence a monitor change
// needs. A random direction and distance, clamped to the current display's
// workArea so she never wanders into a bezel gap or off a single-monitor
// setup's only screen.
async function wanderNearby(win: BrowserWindow, distanceMinPx: number, distanceMaxPx: number): Promise<void> {
  if (win.isDestroyed()) return;
  const bounds = win.getBounds();
  const display = screen.getDisplayMatching(bounds);
  const distance = distanceMinPx + Math.random() * Math.max(0, distanceMaxPx - distanceMinPx);
  const direction = Math.random() < 0.5 ? -1 : 1;
  const minX = display.workArea.x;
  const maxX = display.workArea.x + display.workArea.width - bounds.width;
  const targetX = Math.max(minX, Math.min(maxX, Math.round(bounds.x + direction * distance)));
  if (targetX === bounds.x) return; // clamped to zero movement (already pinned against this edge) - skip this cycle rather than force a walk of no distance

  win.webContents.send("character:state", "walking");
  await animateX(win, bounds.x, targetX, bounds.y);
  win.webContents.send("character:state", "idle");
}

// Computer-use performance layer (PROMPTS.md A18) - walks the character
// window horizontally toward targetX (absolute desktop pixels), reusing the
// same X-only, linear-in-time animateX() A15 already built rather than a
// second movement system for the window. The eased path in "cursor moves
// along an eased path" is the OS cursor's own (tools/_computer_input.py's
// move_cursor_eased()), a distinct thing from this window-position walk.
// Horizontal-only by explicit decision (this session, not assumed): a
// character walking along a plane reads as physically present; floating
// vertically to hover next to a target near the top of the screen would
// trade that for targeting precision the performance layer doesn't actually
// need - the eased cursor path already covers "give the user time to see
// and abort." Same workArea clamping wanderNearby() already does, so a
// target on a different monitor doesn't walk her into a bezel gap chasing
// an X she can't actually reach this way.
export async function walkToward(win: BrowserWindow, targetX: number): Promise<void> {
  if (win.isDestroyed()) return;
  const bounds = win.getBounds();
  const display = screen.getDisplayMatching(bounds);
  const minX = display.workArea.x;
  const maxX = display.workArea.x + display.workArea.width - bounds.width;
  const clampedX = Math.max(minX, Math.min(maxX, Math.round(targetX - bounds.width / 2)));
  if (clampedX === bounds.x) return;
  await animateX(win, bounds.x, clampedX, bounds.y);
}

// Schedules wander attempts on a randomised interval within
// [interval_min_s, interval_max_s]. isSuppressed() is checked at fire time,
// not at schedule time - a cycle that lands mid-conversation is skipped
// entirely (not queued for the instant she goes quiet) and the next
// interval is drawn fresh, so autonomous wandering never reads as "waiting
// to pounce the moment you stop talking."
function startIdleWander(win: BrowserWindow, cfg: WanderConfig, isSuppressed: () => boolean): () => void {
  let timer: NodeJS.Timeout | null = null;
  let stopped = false;

  const scheduleNext = () => {
    if (stopped || !cfg.enabled) return;
    const span = Math.max(0, cfg.interval_max_s - cfg.interval_min_s);
    const delayMs = (cfg.interval_min_s + Math.random() * span) * 1000;
    timer = setTimeout(async () => {
      if (!stopped && !win.isDestroyed() && !isSuppressed()) {
        await wanderNearby(win, cfg.distance_min_px, cfg.distance_max_px);
      }
      scheduleNext();
    }, delayMs);
  };
  scheduleNext();

  return () => {
    stopped = true;
    if (timer) clearTimeout(timer);
  };
}

export function startCharacterFeatures(win: BrowserWindow, root: string): () => void {
  const hoverHandler = (_e: Electron.IpcMainEvent, isHovering: boolean) => {
    if (win.isDestroyed()) return;
    if (isHovering) {
      win.setIgnoreMouseEvents(false);
    } else {
      win.setIgnoreMouseEvents(true, { forward: true });
    }
  };

  // Hologram filter frame-cost (CLAUDE.md rule 3 - instrument before
  // optimizing, and this window runs continuously) - character_renderer.ts's
  // ticker reports a rolling avg/max every ~10s, appended here as one
  // structured JSONL record per report, same convention as every other
  // services/*.jsonl log.
  const renderLogPath = path.join(root, "logs", "character_render.jsonl");
  fs.mkdirSync(path.dirname(renderLogPath), { recursive: true });
  const frameTimingHandler = (
    _e: Electron.IpcMainEvent,
    avgMs: number,
    maxMs: number,
    sampleCount: number
  ) => {
    const record = { timestamp: new Date().toISOString(), avg_frame_ms: avgMs, max_frame_ms: maxMs, sample_count: sampleCount };
    fs.appendFile(renderLogPath, JSON.stringify(record) + "\n", () => {});
  };
  ipcMain.on("character:frame-timing", frameTimingHandler);
  ipcMain.on("character:hover", hoverHandler);

  // Gaze tracking (done-when: "follows your cursor with her eyes") - the
  // real global cursor position, polled from the main process since
  // screen.getCursorScreenPoint() works regardless of focus or
  // click-through state (the renderer alone has no way to see the cursor
  // when it's not over the window at all). Converted to window-local
  // coordinates here, not in the renderer, since main already knows both
  // the cursor's global position and the window's current bounds.
  const cursorInterval = setInterval(() => {
    if (win.isDestroyed()) return;
    const cursor = screen.getCursorScreenPoint();
    const bounds = win.getBounds();
    win.webContents.send("character:cursor", cursor.x - bounds.x, cursor.y - bounds.y);
  }, CURSOR_POLL_MS);

  // Lip sync (PROMPTS.md A15) - tails services/voice/playback_state.py's
  // real amplitude field, same fs.watch-plus-backstop-poll pattern as
  // A12's log panels, not a separate transport. Also reads the same file's
  // "active" field for idle-wander suppression below - one read, two uses,
  // not two separate watchers on the same file.
  const statePath = path.join(root, "logs", "playback_state.json");
  fs.mkdirSync(path.dirname(statePath), { recursive: true });
  let lastAmplitude = -1;
  let playbackActive = false;
  const sendAmplitude = () => {
    if (win.isDestroyed()) return;
    try {
      const data = JSON.parse(fs.readFileSync(statePath, "utf8"));
      playbackActive = Boolean(data.active);
      const amplitude = typeof data.amplitude === "number" ? data.amplitude : 0;
      if (amplitude !== lastAmplitude) {
        lastAmplitude = amplitude;
        win.webContents.send("character:amplitude", amplitude);
      }
    } catch {
      // no file yet, or a torn read mid-write (playback_state.py writes
      // atomically via tmp+rename, but a read can still land between watch
      // notification and the rename completing) - skip this tick, not fatal
    }
  };
  const amplitudeWatcher = fs.watch(path.dirname(statePath), (_e, filename) => {
    if (filename === "playback_state.json") sendAmplitude();
  });
  const amplitudeBackstop = setInterval(sendAmplitude, AMPLITUDE_BACKSTOP_MS);

  // Idle-wander suppression, second half: services/ears/listening_state.py's
  // "active" field (true from wake-trigger through the end of an utterance
  // or a resume window - see that module's docstring for exactly which
  // pipeline states count). Same file-tailing shape as playback_state.json
  // above, on its own watcher since it's a different file.
  const listeningStatePath = path.join(root, "logs", "listening_state.json");
  let listeningActive = false;
  const sendListeningState = () => {
    try {
      const data = JSON.parse(fs.readFileSync(listeningStatePath, "utf8"));
      listeningActive = Boolean(data.active);
    } catch {
      // no file yet, or a torn read mid-write - leave listeningActive at its
      // last known value rather than assuming either state
    }
  };
  sendListeningState();
  const listeningWatcher = fs.watch(path.dirname(listeningStatePath), (_e, filename) => {
    if (filename === "listening_state.json") sendListeningState();
  });
  const listeningBackstop = setInterval(sendListeningState, AMPLITUDE_BACKSTOP_MS);

  const wanderConfig = loadWanderConfig(root);
  const stopWander = startIdleWander(win, wanderConfig, () => playbackActive || listeningActive);

  // Computer-use walk request/status signal (PROMPTS.md A18) - same
  // fs.watch-plus-backstop-poll pattern as the two watchers above, Python
  // writing the REQUEST side (services/character/walk_signal.py) and this
  // process writing the STATUS side back. No fake reach animation: Shizuku's
  // rig has one real motion group and four expressions (STATE_EXPRESSION in
  // character_renderer.ts) - "working" is the same already-existing,
  // already-honest state used elsewhere, not an approximation of a reach
  // gesture the rig can't actually do.
  const walkRequestPath = path.join(root, "logs", "computer_walk_request.json");
  const walkStatusPath = path.join(root, "logs", "computer_walk_status.json");
  fs.mkdirSync(path.dirname(walkRequestPath), { recursive: true });
  let lastWalkRequestId: string | null = null;
  let walkInFlight = false;

  const writeWalkStatus = (requestId: string, state: string) => {
    // Same atomic tmp+rename playback_state.py's docstring documents for the
    // same reason, just with the two processes' reader/writer roles reversed
    // for this specific file - Python polls this one back.
    const tmp = `${walkStatusPath}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify({ request_id: requestId, state }));
    fs.renameSync(tmp, walkStatusPath);
  };

  const handleWalkRequest = async () => {
    if (win.isDestroyed() || walkInFlight) return;
    let request: { request_id: string; action: string; target_x?: number } | null = null;
    try {
      request = JSON.parse(fs.readFileSync(walkRequestPath, "utf8"));
    } catch {
      return; // no file yet, or a torn read mid-write - try again next tick
    }
    if (!request || request.request_id === lastWalkRequestId) return;
    lastWalkRequestId = request.request_id;
    walkInFlight = true;
    try {
      if (request.action === "idle") {
        win.webContents.send("character:state", "idle");
      } else if (request.action === "walk" && typeof request.target_x === "number") {
        win.webContents.send("character:state", "walking");
        await walkToward(win, request.target_x);
        win.webContents.send("character:state", "working");
      }
      writeWalkStatus(request.request_id, "arrived");
    } finally {
      walkInFlight = false;
    }
  };
  const walkRequestWatcher = fs.watch(path.dirname(walkRequestPath), (_e, filename) => {
    if (filename === "computer_walk_request.json") void handleWalkRequest();
  });
  const walkRequestBackstop = setInterval(() => void handleWalkRequest(), AMPLITUDE_BACKSTOP_MS);

  // Hot-plug safety (PLAN.md: "Handle display hot-plug... or she'll end up
  // stranded off-screen"). Real displays, not a simulation - re-checks
  // whichever display config is live at the moment each event fires.
  const staySafe = () => {
    if (win.isDestroyed()) return;
    const bounds = win.getBounds();
    const onScreen = screen.getAllDisplays().some((d) => rectsOverlap(bounds, d.workArea));
    if (!onScreen) {
      const { x, y } = defaultPosition(screen.getPrimaryDisplay());
      win.setPosition(x, y);
    }
  };
  screen.on("display-added", staySafe);
  screen.on("display-removed", staySafe);
  screen.on("display-metrics-changed", staySafe);

  // Manual walk trigger - cycles to the next display in screen.getAllDisplays()
  // order. The *decision* of when to walk (following the active window,
  // reacting to a call, etc.) is a real backend-integration step this
  // doesn't attempt - same "mechanism built, wiring to live triggers
  // deferred" pattern as the rest of this project's tool integrations.
  const hotkeyRegistered = globalShortcut.register(WALK_HOTKEY, () => {
    if (win.isDestroyed()) return;
    const displays = screen.getAllDisplays();
    if (displays.length < 2) return;
    const current = screen.getDisplayMatching(win.getBounds());
    const currentIndex = displays.findIndex((d) => d.id === current.id);
    const next = displays[(currentIndex + 1) % displays.length];
    void walkToDisplay(win, next.id);
  });
  if (!hotkeyRegistered) {
    console.error(`[character] failed to register global hotkey ${WALK_HOTKEY} (already in use by another app?)`);
  }

  return () => {
    ipcMain.removeListener("character:hover", hoverHandler);
    ipcMain.removeListener("character:frame-timing", frameTimingHandler);
    clearInterval(cursorInterval);
    clearInterval(amplitudeBackstop);
    amplitudeWatcher.close();
    clearInterval(listeningBackstop);
    listeningWatcher.close();
    clearInterval(walkRequestBackstop);
    walkRequestWatcher.close();
    stopWander();
    screen.removeListener("display-added", staySafe);
    screen.removeListener("display-removed", staySafe);
    screen.removeListener("display-metrics-changed", staySafe);
    if (hotkeyRegistered) globalShortcut.unregister(WALK_HOTKEY);
  };
}
