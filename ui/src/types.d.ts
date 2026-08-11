// types.d.ts - shape of the window.cortana bridge preload.ts installs.
// Renderer-side only; kept separate from preload.ts so the renderer doesn't
// need to import anything Node-flavored to get the typing.
export {};

export interface PyResult<T> {
  ok: boolean;
  data?: T;
  error?: string;
}

export interface LogEventPayload {
  source: string;
  record: Record<string, unknown>;
}

export interface ModelStatus {
  ok: boolean;
  models?: Array<{ name: string }>;
  error?: string;
}

export interface PendingTimer {
  id: string;
  label: string;
  fire_at: number;
  fired: boolean;
}

export interface UiConfig {
  panel_opacity: number;
  blur_px: number;
  accent: string;
}

declare global {
  interface Window {
    cortana: {
      windowMinimize: () => void;
      windowMaximizeToggle: () => void;
      windowClose: () => void;
      onWindowState: (callback: (state: { maximized: boolean }) => void) => void;
      getUiConfig: () => Promise<UiConfig>;

      onLogEvent: (callback: (payload: LogEventPayload) => void) => void;
      onModelStatus: (callback: (payload: ModelStatus) => void) => void;
      onLatencyUpdate: (callback: (payload: PyResult<unknown>) => void) => void;
      onTimersUpdate: (callback: (payload: PendingTimer[]) => void) => void;

      getMemorySessions: () => Promise<PyResult<Array<{ session_id: string; started: string; count: number }>>>;
      getMemoryEntries: (
        sessionId?: string
      ) => Promise<PyResult<Array<{ id: number; session_id: string; timestamp: string; role: string; source: string; text: string }>>>;
      editMemoryEntry: (id: number, text: string) => Promise<PyResult<{ ok: boolean; reembedded: boolean }>>;
      deleteMemoryEntry: (id: number) => Promise<PyResult<{ ok: boolean }>>;
    };
  }
}
