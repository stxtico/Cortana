// character_renderer.ts - the Live2D character overlay (PROMPTS.md A15).
// PIXI/Live2D load as plain globals via <script> tags in character.html
// (no bundler in ui/, same reason renderer.ts compiles to a plain script -
// see A12's tsconfig.renderer.json split), so this file references them as
// globals rather than importing anything. Deliberately no `export {}` here
// (unlike types.d.ts) - that alone is enough to make tsc emit ES-module
// syntax even under "module": "ES2022", which a plain <script> tag (no
// type="module") can't parse - hit this exact bug once already in A12's
// renderer.ts.
declare const PIXI: any;

const MOUTH_PARAM = "PARAM_MOUTH_OPEN_Y"; // Cubism 2 SDK's standard mouth-openness parameter name

// State -> (motion group, expression). Shizuku's rig (the free placeholder -
// see scripts/fetch_live2d_placeholder.py) has no walk cycle and no
// state-specific motions of its own - it only ships one real motion group
// ("idle", 3 variants) plus tap/pinch/shake interaction reactions. Honest
// placeholder behavior, not a claim of six distinct animations: every state
// plays from the same "idle" motion pool, differentiated by which of the
// four unlabeled expressions (f01-f04) is active - a real commissioned rig
// would give "walking" (and the others) their own motion.
const STATE_EXPRESSION: Record<string, string | null> = {
  idle: null, // whatever expression was already active - idle doesn't force a mood
  listening: "f02",
  thinking: "f03",
  speaking: "f01",
  walking: null,
  working: "f04",
};

const EMOTION_EXPRESSION: Record<string, string> = {
  neutral: "f01",
  amused: "f02",
  skeptical: "f03",
  concerned: "f04",
};

let model: any = null;
let app: any = null;
let currentEmotion = "neutral";
let currentAmplitude = 0;

// Frame-cost instrumentation for the hologram filter (CLAUDE.md rule 3 -
// "instrument before you optimize", and this window is always-on-top and
// always-running, so its per-frame cost is worth tracking on an ongoing
// basis, not just a one-off check). Sampled every tick, reported as a
// rolling avg/max every FRAME_REPORT_MS - not every frame, so the reporting
// itself doesn't add meaningful overhead.
const FRAME_REPORT_MS = 10000;
let frameSum = 0;
let frameMax = 0;
let frameCount = 0;
let frameReportElapsed = 0;

async function init(): Promise<void> {
  const canvas = document.getElementById("stage") as HTMLCanvasElement;
  app = new PIXI.Application({
    view: canvas,
    resizeTo: window,
    backgroundAlpha: 0,
    antialias: true,
  });

  model = await PIXI.live2d.Live2DModel.from("assets/live2d/shizuku/shizuku.model.json");
  app.stage.addChild(model);
  fitModel();
  window.addEventListener("resize", fitModel);

  model.motion("idle");

  const hologramConfig = await window.character.getHologramConfig();
  applyHologram(model, hologramConfig);
  // Matches the control panel's real translucency (panel_opacity, see
  // [ui.hologram]'s comment in cortana.toml) - a uniform PIXI alpha over the
  // whole rendered sprite, applied after the hologram filter so it fades
  // everything (glow, scanlines, glyph rain included) together as one thing.
  model.alpha = hologramConfig.character_opacity;

  // Lip sync has to be reapplied every frame, not set once when a new
  // amplitude value arrives - found genuinely necessary, not built
  // speculatively: the idle motion itself animates PARAM_MOUTH_OPEN_Y (part
  // of the rig's own breathing/talking-idle curve), so a one-shot
  // setParamFloat() got silently overwritten by the very next tick. PIXI's
  // ticker runs callbacks in registration order; the model registers its
  // own update callback when constructed, before this one is added, so this
  // one runs after it each frame and wins.
  app.ticker.add(() => {
    if (model?.internalModel?.coreModel) {
      model.internalModel.coreModel.setParamFloat(MOUTH_PARAM, currentAmplitude);
    }
    updateHologram(app.ticker.deltaMS);
    sampleFrameCost(app.ticker.deltaMS);
  });

  // Hover-based click-through toggle (PLAN.md: click-through by default,
  // "toggled off only when you're interacting with her directly"). The
  // window is created with setIgnoreMouseEvents(true, {forward:true}) -
  // forward:true is what makes these mousemove events actually arrive here
  // at all despite click-through being active; containsPoint() is the
  // library's own real hit-test (accounts for the model's actual bounds/
  // anchor), not a hand-rolled bounding-box approximation.
  let lastHover = false;
  app.view.addEventListener("mousemove", (e: MouseEvent) => {
    // containsPoint() expects world/stage space, which for this single-model
    // canvas is the same space as raw client coordinates - no conversion needed.
    const hovering = model.containsPoint(new PIXI.Point(e.clientX, e.clientY));
    if (hovering !== lastHover) {
      lastHover = hovering;
      window.character.reportHover(hovering);
    }
  });

  window.character.onCursorPosition((x, y) => {
    if (model) model.focus(x, y);
  });
  window.character.onStateChange((state) => setState(state));
  window.character.onEmotionChange((emotion) => setEmotion(emotion));
  window.character.onAmplitude((amplitude) => setMouth(amplitude));
}

function fitModel(): void {
  if (!model || !app) return;
  // Recompute from the model's internal (unscaled) canvas size, not
  // model.width/height - those already reflect any previously-applied
  // scale, so using them here would compound on every resize event.
  const internal = model.internalModel;
  const baseWidth = internal?.width || model.width;
  const baseHeight = internal?.height || model.height;
  const fitScale = Math.min(app.renderer.width / baseWidth, app.renderer.height / baseHeight) * 0.9;
  model.scale.set(fitScale);
  model.anchor.set(0.5, 1);
  model.position.set(app.renderer.width / 2, app.renderer.height);
}

function setState(state: string): void {
  if (!model) return;
  model.motion("idle"); // the rig's only real motion group - see STATE_EXPRESSION's comment
  const forced = STATE_EXPRESSION[state];
  if (forced) {
    model.expression(forced);
  } else {
    model.expression(EMOTION_EXPRESSION[currentEmotion]);
  }
}

function setEmotion(emotion: string): void {
  currentEmotion = emotion;
  const expr = EMOTION_EXPRESSION[emotion];
  if (model && expr) model.expression(expr);
}

function setMouth(amplitude: number): void {
  // Cheap and convincing, not phoneme-accurate (PLAN.md's own framing) -
  // raw RMS amplitude, clamped and scaled. Just stores the target value;
  // the ticker callback registered in init() applies it every frame (see
  // that comment for why a one-shot set doesn't stick).
  currentAmplitude = Math.max(0, Math.min(1, amplitude * 6));
}

function sampleFrameCost(deltaMs: number): void {
  frameSum += deltaMs;
  frameMax = Math.max(frameMax, deltaMs);
  frameCount += 1;
  frameReportElapsed += deltaMs;
  if (frameReportElapsed >= FRAME_REPORT_MS) {
    window.character.reportFrameTiming(frameSum / frameCount, frameMax, frameCount);
    frameSum = 0;
    frameMax = 0;
    frameCount = 0;
    frameReportElapsed = 0;
  }
}

init();
