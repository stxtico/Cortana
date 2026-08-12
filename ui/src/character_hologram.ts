// character_hologram.ts - Halo 2A-Cortana-style holographic shader overlay on
// the Live2D model (pre-A16 follow-up to A15's placeholder rig). Plain global
// script, same reasoning as character_renderer.ts (no bundler in ui/, no
// export {} so tsc emits an unwrapped script a <script> tag can parse) -
// character_renderer.ts calls applyHologram()/updateHologram() as globals,
// no import needed. PIXI itself is declared once, in character_renderer.ts -
// both files share one global scope (no imports/exports in either), so a
// second `declare const PIXI` here would be a duplicate declaration.

interface HologramConfig {
  enabled: boolean;
  character_opacity: number;
  scanline_density: number;
  scanline_opacity: number;
  drift_speed: number;
  data_texture_opacity: number;
  data_texture_mode: string; // "multiply" (default) or "additive" - see FRAGMENT_SRC's uDataMode branch
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

// Glyph rain's character set, baked into an offscreen-canvas texture atlas
// once at startup (getGlyphAtlas()) - digits, a handful of uppercase letters
// chosen for visual variety at small size (avoiding lookalikes like O/0,
// I/1), and a few geometric marks so it doesn't read as pure alphanumeric
// text. 32 glyphs on an 8x4 grid - a size PXI can hand to the shader as a
// second sampler alongside the model's own texture.
const GLYPHS = "0123456789ABDEFHKLMNPRTXZ+-/\\|<>".split("");
const ATLAS_COLS = 8;
const ATLAS_ROWS = Math.ceil(GLYPHS.length / ATLAS_COLS);
const ATLAS_CELL_PX = 48; // atlas resolution per glyph - comfortably above any on-screen cell size so downsampling stays crisp rather than mushy

interface GlyphAtlas {
  texture: any;
  cols: number;
  rows: number;
  count: number;
}

let glyphAtlasCache: GlyphAtlas | null = null;

function getGlyphAtlas(): GlyphAtlas {
  if (glyphAtlasCache) return glyphAtlasCache;
  const canvas = document.createElement("canvas");
  canvas.width = ATLAS_COLS * ATLAS_CELL_PX;
  canvas.height = ATLAS_ROWS * ATLAS_CELL_PX;
  const ctx = canvas.getContext("2d")!;
  ctx.fillStyle = "#000000";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = "#ffffff";
  ctx.font = `700 ${Math.floor(ATLAS_CELL_PX * 0.74)}px "Consolas", "Cascadia Mono", monospace`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  GLYPHS.forEach((ch, i) => {
    const col = i % ATLAS_COLS;
    const row = Math.floor(i / ATLAS_COLS);
    ctx.fillText(ch, col * ATLAS_CELL_PX + ATLAS_CELL_PX / 2, row * ATLAS_CELL_PX + ATLAS_CELL_PX / 2 + ATLAS_CELL_PX * 0.04);
  });
  glyphAtlasCache = { texture: PIXI.Texture.from(canvas), cols: ATLAS_COLS, rows: ATLAS_ROWS, count: GLYPHS.length };
  return glyphAtlasCache;
}

// One combined pass rather than several chained filters - chromatic split,
// rim glow, scanlines, glyph rain and tint all read from the same handful
// of texture samples, and this window is always-on-top/always-running (the
// prompt's own concern), so one render-to-texture pass beats four or five.
// Alpha is derived only from the model's own sampled alpha (plus the rim
// glow's own bounded contribution) - never set to 1 outright - so pixels
// with no character underneath stay transparent and the desktop keeps
// showing through around her.
const FRAGMENT_SRC = `
precision highp float;
varying vec2 vTextureCoord;
uniform sampler2D uSampler;
uniform sampler2D uGlyphAtlas;
uniform highp vec4 inputSize;   // auto-provided by PIXI: xy = framebuffer px size, zw = 1/size - highp to match the precision PIXI's default vertex shader declares these at (mismatched precision on a shared uniform is a hard link error in WebGL, not just a warning)
uniform highp vec4 inputClamp;  // auto-provided by PIXI: xy = min texcoord, zw = max texcoord

uniform float uTime;
uniform float uScanDensity;
uniform float uScanOpacity;
uniform float uDriftSpeed;
uniform float uDataOpacity;
uniform float uDataMode; // 0.0 = additive (glyphs glow as light on top), 1.0 = multiply (glyphs modulate her own brightness, default)
uniform float uColumnWidth;    // px, width of one rain column (and, scaled, its row height)
uniform float uFallSpeed;      // rows/sec baseline - each column varies around this
uniform float uGlyphSwapRate;  // glyph identity changes/sec, independent of the fall rate
uniform float uTrailLength;    // rows in the fading trail behind the lead glyph
uniform float uGlyphCols;
uniform float uGlyphRows;
uniform float uGlyphCount;
uniform vec3  uTintColor;
uniform float uTintStrength;
uniform vec3  uRimColor;
uniform float uRimIntensity;
uniform float uRimWidth;
uniform float uChromaOffset;

vec2 clampUV(vec2 uv) {
  return clamp(uv, inputClamp.xy, inputClamp.zw);
}

float hash(vec2 p) {
  return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453123);
}

void main(void) {
  vec2 texel = inputSize.zw;

  // ---- chromatic separation: split R/B horizontally; alpha bleeds with
  // whichever channel sampled a bit of her, so the fringe pokes just past
  // the real silhouette instead of being clipped to it ----
  vec2 off = vec2(uChromaOffset * texel.x, 0.0);
  vec4 cR = texture2D(uSampler, clampUV(vTextureCoord + off));
  vec4 cG = texture2D(uSampler, vTextureCoord);
  vec4 cB = texture2D(uSampler, clampUV(vTextureCoord - off));
  // Force premultiplied alpha on every sample before using it for anything -
  // Live2D's texture atlas leaves real, non-zero RGB baked into fully
  // transparent (alpha 0) margins (ordinary for straight-alpha art), and
  // without this, that garbage color survives the math below and gets
  // additively blended onto the desktop despite alpha staying 0 (WebGL's
  // premultiplied blend adds src.rgb unconditionally - alpha only controls
  // how much of dst it keeps, not whether src.rgb is added at all).
  cR.rgb *= cR.a;
  cG.rgb *= cG.a;
  cB.rgb *= cB.a;
  vec3 rgb = vec3(cR.r, cG.g, cB.b);
  float a = max(cG.a, max(cR.a, cB.a));

  // ---- rim glow: ring-sample alpha around this pixel; lights up where
  // neighbours are solid but this pixel mostly isn't (just outside her
  // edge), fades to nothing a short distance further out ----
  float ring = 0.0;
  const int RIM_SAMPLES = 8;
  for (int i = 0; i < RIM_SAMPLES; i++) {
    float ang = (float(i) / float(RIM_SAMPLES)) * 6.28318530718;
    vec2 ringOff = vec2(cos(ang), sin(ang)) * uRimWidth * texel;
    ring += texture2D(uSampler, clampUV(vTextureCoord + ringOff)).a;
  }
  ring /= float(RIM_SAMPLES);
  float rim = ring * (1.0 - a) * uRimIntensity;
  rgb += uRimColor * rim;
  a = clamp(max(a, rim), 0.0, 1.0);

  // ---- scanlines: darken in horizontal bands, drifting slowly downward.
  // Multiplicative only - stays exactly zero wherever rgb already is. This
  // is the only thing uDriftSpeed drives - the glyph rain below has its own
  // independent fall speed, so the two motions never fight or compound. ----
  float scan = sin(vTextureCoord.y * uScanDensity * 6.28318530718 - uTime * uDriftSpeed * 0.4) * 0.5 + 0.5;
  rgb *= (1.0 - uScanOpacity * scan * scan);

  // ---- glyph rain: narrow columns, each with its own speed/phase, one
  // discrete head glyph descending per column with a fading trail behind
  // it, glyphs swapping identity independently of the fall itself ----
  {
    float colWidthUV = uColumnWidth * texel.x;
    float rowHeightUV = uColumnWidth * 1.3 * texel.y; // slightly taller than wide, monospace-ish - an internal ratio, not separately configurable
    float colIndex = floor(vTextureCoord.x / colWidthUV);
    float colLocalX = fract(vTextureCoord.x / colWidthUV);
    float rowIndex = floor(vTextureCoord.y / rowHeightUV);
    float rowLocalY = fract(vTextureCoord.y / rowHeightUV);

    // Each column gets its own speed (0.6x-1.4x the base) and phase from a
    // per-column hash, so columns never fall in lockstep.
    float colSeed = hash(vec2(colIndex, 11.3));
    float colSpeed = mix(0.6, 1.4, colSeed) * uFallSpeed;
    float colPhase = hash(vec2(colIndex, 47.9));

    // The head's row position is a STEPPED value (floor()'d) - it holds for
    // a whole row's worth of time then jumps by exactly one row, which is
    // what makes this read as a sequence of characters advancing rather
    // than a texture sliding smoothly. mod() against a cycle length (the
    // column's visible height plus the trail plus a gap) makes each column
    // an endlessly-repeating single stream with a real gap between passes,
    // instead of a constant back-to-back field.
    float rowsInView = 1.0 / rowHeightUV;
    float cycleLen = rowsInView + uTrailLength + rowsInView * 0.5;
    float headRow = floor(mod(uTime * colSpeed + colPhase * cycleLen, cycleLen) - uTrailLength);

    float dist = headRow - rowIndex;
    float rainBrightness = 0.0;
    if (dist >= 0.0 && dist < uTrailLength) {
      rainBrightness = 1.0 - dist / uTrailLength; // 1.0 at the lead glyph, fading toward 0 at the trail's end
    }

    // Glyph identity swaps on its own cadence (uGlyphSwapRate), independent
    // of the fall stepping above - a cell can flicker to a different
    // character while it's still sitting in the same row/trail position.
    float swapStep = floor(uTime * uGlyphSwapRate);
    float glyphSeed = hash(vec2(colIndex, rowIndex) + vec2(swapStep * 0.53, swapStep * 0.71));
    float glyphIndex = floor(glyphSeed * uGlyphCount);
    float gcol = mod(glyphIndex, uGlyphCols);
    float grow = floor(glyphIndex / uGlyphCols);
    vec2 atlasUV = (vec2(gcol, grow) + vec2(colLocalX, rowLocalY)) / vec2(uGlyphCols, uGlyphRows);
    float ink = texture2D(uGlyphAtlas, atlasUV).r;

    float glow = ink * rainBrightness;
    if (uDataMode > 0.5) {
      // Multiply: brighten where a glyph stroke sits, and (more weakly)
      // darken the space around it within the same lit cell - modulates
      // her own surface rather than adding a separate layer of light, same
      // reasoning as the earlier banded-flicker version this replaces.
      rgb *= 1.0 + (glow - 0.15 * rainBrightness) * uDataOpacity;
    } else {
      // Additive (legacy-slot, kept for comparison): glyphs glow as colored
      // light on top, gated to her actual surface (cG.a) so it never shows
      // in the rim halo.
      rgb += uRimColor * glow * uDataOpacity * cG.a;
    }
  }

  // ---- overall holographic tint - self-limiting to already-lit pixels
  // since luma is derived from rgb itself, so it can't introduce color on a
  // fully transparent pixel ----
  float luma = max(rgb.r, max(rgb.g, rgb.b));
  rgb = mix(rgb, uTintColor * luma + uTintColor * 0.12 * cG.a, uTintStrength);

  gl_FragColor = vec4(rgb, a);
}
`;

class HologramFilter extends (PIXI as any).Filter {
  constructor(config: HologramConfig) {
    const atlas = getGlyphAtlas();
    super(undefined, FRAGMENT_SRC, {
      uTime: 0,
      uScanDensity: config.scanline_density,
      uScanOpacity: config.scanline_opacity,
      uDriftSpeed: config.drift_speed,
      uDataOpacity: config.data_texture_opacity,
      uDataMode: config.data_texture_mode === "additive" ? 0.0 : 1.0,
      uColumnWidth: config.data_texture_column_width,
      uFallSpeed: config.data_texture_fall_speed,
      uGlyphSwapRate: config.data_texture_glyph_swap_rate,
      uTrailLength: config.data_texture_trail_length,
      uGlyphAtlas: atlas.texture,
      uGlyphCols: atlas.cols,
      uGlyphRows: atlas.rows,
      uGlyphCount: atlas.count,
      uTintColor: hexToVec3(config.tint_color),
      uTintStrength: config.tint_strength,
      uRimColor: hexToVec3(config.rim_color),
      uRimIntensity: config.rim_intensity,
      uRimWidth: config.rim_width,
      uChromaOffset: config.chromatic_offset,
    });
    // Padding expands the render target beyond the model's own tight
    // bounding box - without it, ring/chroma samples (and the resulting rim
    // glow) clamp hard at her silhouette instead of bleeding into the
    // transparent margin already reserved around her (fitModel()'s 0.9 scale).
    this.padding = Math.ceil(config.rim_width * 3 + config.chromatic_offset + 8);
  }
}

function hexToVec3(hex: string): [number, number, number] {
  const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex);
  if (!m) return [57 / 255, 230 / 255, 255 / 255];
  return [parseInt(m[1], 16) / 255, parseInt(m[2], 16) / 255, parseInt(m[3], 16) / 255];
}

let activeFilter: HologramFilter | null = null;
let elapsedS = 0;

function applyHologram(model: any, config: HologramConfig): void {
  if (!config.enabled) {
    model.filters = null;
    activeFilter = null;
    return;
  }
  activeFilter = new HologramFilter(config);
  model.filters = [activeFilter];
}

// Called once per tick from character_renderer.ts's existing ticker (same
// place lip sync's mouth param gets reapplied every frame) - keeps one
// ticker registration for the window instead of a second one here.
function updateHologram(deltaMs: number): void {
  if (!activeFilter) return;
  elapsedS += deltaMs / 1000;
  activeFilter.uniforms.uTime = elapsedS;
}
