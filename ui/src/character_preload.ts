// character_preload.ts - bridge for the character overlay window (PROMPTS.md
// A15). Same contextIsolation-only-surface pattern as ui/src/preload.ts.
import { contextBridge, ipcRenderer } from "electron";

export type CharacterState = "idle" | "listening" | "thinking" | "speaking" | "walking" | "working";
export type Emotion = "neutral" | "amused" | "skeptical" | "concerned";

contextBridge.exposeInMainWorld("character", {
  // Gaze tracking (done-when: "follows your cursor with her eyes") - main
  // polls the real global cursor position (screen.getCursorScreenPoint(),
  // works regardless of focus/click-through state) and forwards it already
  // converted to window-local coordinates, since main is the one that knows
  // both the cursor's global position and the window's current bounds at
  // poll time.
  onCursorPosition: (callback: (localX: number, localY: number) => void) => {
    ipcRenderer.on("character:cursor", (_e, x: number, y: number) => callback(x, y));
  },

  onStateChange: (callback: (state: CharacterState) => void) => {
    ipcRenderer.on("character:state", (_e, state: CharacterState) => callback(state));
  },
  onEmotionChange: (callback: (emotion: Emotion) => void) => {
    ipcRenderer.on("character:emotion", (_e, emotion: Emotion) => callback(emotion));
  },
  // Lip sync (PROMPTS.md A15 - "driven by TTS audio amplitude"). A real
  // signal, not a demo oscillator: services/voice/tts.py writes actual RMS
  // amplitude per ~100ms playback sub-block to logs/playback_state.json,
  // main.ts tails that file (fs.watch, same pattern as A12's log panels)
  // and forwards it here.
  onAmplitude: (callback: (amplitude: number) => void) => {
    ipcRenderer.on("character:amplitude", (_e, amplitude: number) => callback(amplitude));
  },

  // Click-through toggle (PLAN.md: click-through by default, "toggled off
  // only when you're interacting with her directly"). The renderer decides
  // hover-over-visible-pixels (it's the only side that knows where the
  // model's opaque pixels actually are); main is the only side that can
  // call setIgnoreMouseEvents().
  reportHover: (isHovering: boolean) => ipcRenderer.send("character:hover", isHovering),
});
