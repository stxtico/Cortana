// character_main.ts - main-process side of the character overlay
// (PROMPTS.md A15). Kept separate from main.ts (which owns the control
// panel window) - two windows, one Electron app, same split-by-concern
// pattern as log_tail.ts/py_bridge.ts.
import { BrowserWindow, Rectangle, ipcMain, screen, globalShortcut } from "electron";
import * as fs from "fs";
import * as path from "path";

const CURSOR_POLL_MS = 33; // ~30fps - smooth enough for eye tracking, cheap enough to poll forever
const AMPLITUDE_BACKSTOP_MS = 150;
const WALK_SPEED_PX_S = 500;
const WALK_STEP_MS = 16;
const WINDOW_WIDTH = 320;
const WINDOW_HEIGHT = 480;
const EDGE_MARGIN = 40;
const WALK_HOTKEY = "Alt+Shift+W"; // manual trigger - real backend triggers for *when* to walk are a later integration step, same deferral pattern as A8/A9/A10's voice-answer paths

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

export function startCharacterFeatures(win: BrowserWindow, root: string): () => void {
  const hoverHandler = (_e: Electron.IpcMainEvent, isHovering: boolean) => {
    if (win.isDestroyed()) return;
    if (isHovering) {
      win.setIgnoreMouseEvents(false);
    } else {
      win.setIgnoreMouseEvents(true, { forward: true });
    }
  };
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
  // A12's log panels, not a separate transport.
  const statePath = path.join(root, "logs", "playback_state.json");
  fs.mkdirSync(path.dirname(statePath), { recursive: true });
  let lastAmplitude = -1;
  const sendAmplitude = () => {
    if (win.isDestroyed()) return;
    try {
      const data = JSON.parse(fs.readFileSync(statePath, "utf8"));
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
    clearInterval(cursorInterval);
    clearInterval(amplitudeBackstop);
    amplitudeWatcher.close();
    screen.removeListener("display-added", staySafe);
    screen.removeListener("display-removed", staySafe);
    screen.removeListener("display-metrics-changed", staySafe);
    if (hotkeyRegistered) globalShortcut.unregister(WALK_HOTKEY);
  };
}
