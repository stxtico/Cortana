// preload.js - the only bridge between the renderer (untrusted-ish, plain
// HTML/JS) and the main process's Node/filesystem access. contextIsolation
// stays on (main.js) so the renderer never gets raw ipcRenderer/require -
// only this fixed, small surface.
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("cortana", {
  onLogEvent: (callback) => {
    ipcRenderer.on("log-event", (event, payload) => callback(payload));
  },
  onModelStatus: (callback) => {
    ipcRenderer.on("model-status", (event, payload) => callback(payload));
  },
  getMemorySessions: () => ipcRenderer.invoke("memory:sessions"),
  getMemoryEntries: (sessionId) => ipcRenderer.invoke("memory:list", sessionId),
});
