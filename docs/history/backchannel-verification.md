# Backchannel verification and quality tuning (pre-A4)

Moved verbatim from CLAUDE.md's Done log (2026-08-12 restructure) — see
CLAUDE.md's Done log for the one-line pointer back to this file.

- **Live end-to-end backchannel test - verified for real.** Real wake word, real
  trailing-off speech, real resume, via `python -m services.ears.pipeline` (had to
  rerun with `PYTHONUNBUFFERED=1` - the first attempt's printed transcripts never
  flushed to the captured output before the process ended, a tooling gotcha, not a
  pipeline bug). Confirmed directly from `logs/ears.jsonl` plus the actual printed
  `> ...` output: two full trail-off -> backchannel -> resume cycles, both correctly
  fused into one yielded utterance - one of them chained *two* resumes ("Oh, no."
  correctly did not read as complete on its own, kept waiting, then the real
  continuation arrived and got appended too). Rate limiting verified live: a second
  abandonment 17.5s after the first backchannel (under the 20s `base_cooldown_s`)
  correctly played no audio but still entered `awaiting_resume` and kept listening
  rather than silently giving up. Normal (non-abandoned) utterances still bypass the
  system entirely, confirmed on two more utterances. A same-score-across-consecutive-
  wake-triggers pattern looked like a caching bug at first glance; checked `wake.py`
  directly - the score is freshly computed from the model every call, no caching -
  the repeats just reflect the same phrase being said again, not a bug.

- Backchannel quality: four listening-driven fixes, none unilateral - every one
  regenerated and judged by ear before shipping (`scripts/regenerate_backchannel_pool.py`,
  `demo_level_ramp.py`, `demo_master_gain.py`, `demo_backchannel_vs_master.py`).
  1. **Non-lexical sounds dropped entirely.** First pass's generation prompt biased
     toward "Mm"/"Mhm"/"Hmm" - XTTS has no real pronunciation for these, they came
     out as long, strange vocalizations (1.9-2.2s for two letters) instead of a
     quick word. `_GENERATION_PROMPT` rewritten to require real lexical words
     ("Right.", "Yeah.", "Got it.", "Sure.", "Okay.", "I see.", "Oh?") and forbid
     non-lexical spellings and directives/questions alike - durations back to
     0.79-1.76s.
  2. **`soft` reference + `speed=0.88`, backchannel-only.** `BackchannelPool.
     ensure_filled()` saves the shared XTTS engine's active reference, switches to
     `soft` for the duration of the pool fill, restores it after (verified: active
     reference was `calm` before and after, in every regeneration run) - real
     responses never see the switch. Known gap, not addressed: no lock against a
     concurrent real `synthesize()` call landing mid-refill on the shared engine;
     low-probability given how short a refill is, flagged in `ensure_filled()`'s
     docstring for whenever it's worth revisiting.
  3. **Volume continuity across utterances** (`tts.py`'s `_ramp_gain()` /
     `_record_played_level()`, `[voice.level_ramp]`): without this, every utterance
     synthesized at a fixed level with no memory of the last one, so a soft
     backchannel could be immediately followed by a full-volume response - not how
     people talk. Tracks the RMS + timestamp of the last thing actually played
     (`play_audio()` and `_play_all()` both feed and read this shared state); if the
     next utterance starts within `window_s` (6.0s default), it's capped to at most
     `step_db` (6.0 default) louder than the last played level, computed once per
     utterance (from its first chunk) and held for the whole thing - recomputing per
     chunk would pump the level up/down mid-response instead of ramping smoothly
     turn to turn. Only ever attenuates toward continuity, never boosts. Verified
     with real synthesis/playback, not simulated: backchannel "Right." at -28.6dB ->
     turn 1 capped to -22.4dB (+6.2dB, matching `step_db`) -> turn 2 -19.8dB (barely
     capped, already near natural) -> turn 3 -18.6dB, fully recovered. Two to three
     turns to full recovery, as intended.
  4. **Master output gain** (`[voice].output_gain_db`, `tts.py`'s `_output_gain()`):
     one setting covering every playback path (`play_audio()`, `_play_all()` -
     backchannels and real responses alike), not per-strategy. Applied *after* the
     ramp, not before - the ramp reasons about relative levels between utterances
     via `_record_played_level()`, which records the pre-master-gain level so ramp
     comparisons stay in their own frame regardless of what the master is set to;
     the master is a pure final multiply with no feedback into ramp state. Compared
     -10/-20/-30dB on the same dry-synthesized response (`voice_refs/audition/
     master_gain_demo/`) - listening verdict: **-20dB**.
  Backchannel `volume_db` re-checked against the new master, since the two stack
  additively in dB: the old `-8.0` was calibrated against a 0dB master and measured
  10.6dB under a normal response once `output_gain_db=-20` landed - too buried.
  Also found a backchannel's *dry* level (before any `volume_db` attenuation at all)
  is already ~2.6dB under a response's, just from being short/soft-referenced/slow.
  Compared 0/-2/-4/-6/-8 stacked on the -20dB master (`voice_refs/audition/
  backchannel_vs_master/`) - listening verdict: **`volume_db = -2.0`**, landing
  about 4.6dB under a normal response.
  Two listening items closed this session, not left pending: the original
  temperature/speed param sweep on `calm_14` is superseded by the later
  `units_only`-text sweep and doesn't need a separate verdict; the `calm`-vs-`soft`
  reference choice is resolved by `soft` now being in real production use for
  backchannels specifically (fix 2 above) - no further decision needed on either.

