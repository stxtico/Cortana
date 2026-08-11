// preload.ts - the only bridge between the renderer (plain HTML/TS, no Node
// access) and the main process. contextIsolation stays on (main.ts) so the
// renderer never gets raw ipcRenderer/require - only this fixed surface.
import { contextBridge, ipcRenderer } from "electron";

contextBridge.exposeInMainWorld("cortana", {
  // window chrome (frameless - PROMPTS.md A12's visual direction)
  windowMinimize: () => ipcRenderer.send("window:minimize"),
  windowMaximizeToggle: () => ipcRenderer.send("window:maximize-toggle"),
  windowClose: () => ipcRenderer.send("window:close"),
  onWindowState: (callback: (state: { maximized: boolean }) => void) => {
    ipcRenderer.on("window:state", (_event, payload) => callback(payload));
  },
  getUiConfig: () => ipcRenderer.invoke("window:get-ui-config"),

  // log/data feeds
  onLogEvent: (callback: (payload: { source: string; record: Record<string, unknown> }) => void) => {
    ipcRenderer.on("log-event", (_event, payload) => callback(payload));
  },
  onModelStatus: (callback: (payload: unknown) => void) => {
    ipcRenderer.on("model-status", (_event, payload) => callback(payload));
  },
  onLatencyUpdate: (callback: (payload: unknown) => void) => {
    ipcRenderer.on("latency-update", (_event, payload) => callback(payload));
  },
  onTimersUpdate: (callback: (payload: unknown) => void) => {
    ipcRenderer.on("timers-update", (_event, payload) => callback(payload));
  },

  // memory tab
  getMemorySessions: () => ipcRenderer.invoke("memory:sessions"),
  getMemoryEntries: (sessionId?: string) => ipcRenderer.invoke("memory:list", sessionId),
  editMemoryEntry: (id: number, text: string) => ipcRenderer.invoke("memory:edit", id, text),
  deleteMemoryEntry: (id: number) => ipcRenderer.invoke("memory:delete", id),
});
