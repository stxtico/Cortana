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
  window_opacity: number; // applied at the BrowserWindow level in main.ts, not read by the renderer directly
  blur_px: number;
  accent: string;
}

export interface HologramConfig {
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
    character: {
      onCursorPosition: (callback: (localX: number, localY: number) => void) => void;
      onStateChange: (callback: (state: string) => void) => void;
      onEmotionChange: (callback: (emotion: string) => void) => void;
      onAmplitude: (callback: (amplitude: number) => void) => void;
      reportHover: (isHovering: boolean) => void;
      getHologramConfig: () => Promise<HologramConfig>;
      reportFrameTiming: (avgMs: number, maxMs: number, sampleCount: number) => void;
    };
  }
}
