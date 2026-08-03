# Training a real "hey cortana" wake word

`wake.py` currently runs on `hey_jarvis`, one of openWakeWord's bundled pretrained
models — a stand-in, not the target phrase. Calibration against live background
speech (`scripts/wake_calibration.py`, see session logs) showed the stand-in has a
real false-accept problem on conversational audio, and confidence score alone can't
separate real detections from false ones. A verification gate (STT confirms the
phrase before entering RECORDING, `[audio.wake].verify` in `cortana.toml`) works
around this for now. The actual fix is training the right model instead of tuning
the wrong one.

This document is a plan, not a completed pipeline — nothing here has been run yet.

## Why synthetic, not recorded samples

openWakeWord's own recommended approach (see its
[GitHub repo](https://github.com/dscripka/openWakeWord) and the
`notebooks/automatic_model_training.ipynb` notebook it ships) is to generate
**thousands of synthetic positive examples via TTS** rather than hand-record a
small set of real utterances. A handful of live recordings badly overfits to one
voice, one room, one mic. Synthetic generation gets you:

- Many TTS voices → speaker diversity a single person can't record themselves
- Programmatic pitch/speed/prosody variation on top of that
- Room impulse response (RIR) convolution + background noise mixing → acoustic
  diversity (different rooms, different noise floors) without physically moving
  around the house
- All of it scriptable, so re-running with more variety later is cheap

The model itself is a small classifier trained on top of openWakeWord's bundled,
frozen speech-embedding model — the embedding model doesn't get retrained, only a
lightweight head on top of it does. That's why this is tractable on a single
consumer GPU (or even CPU) rather than a full model-training undertaking.

## What the pipeline needs

**Software** (beyond what's already in `pyproject.toml`):
- openWakeWord's training extras — check the current `requirements` in the
  upstream repo, this has historically included `torch`/`torchaudio`,
  `torch-audiomentations`, `audiomentations`, `torchmetrics`, `speechbrain`, and
  the author's separate `piper-sample-generator` tool for bulk TTS generation via
  Piper voices. Confirm exact package names/versions against the repo before
  installing — don't assume the list above is current.
- A TTS engine capable of generating many distinct voices quickly (Piper is what
  the upstream notebook is built around).

**Data:**
- **Positive samples**: thousands of TTS-generated "hey cortana" clips across many
  voices, plus programmatic pitch/rate variation.
- **Negative samples**: a large, diverse set of audio that does *not* contain the
  phrase — general speech, music, ambient noise. Needs to be big enough that the
  classifier learns "not the phrase" broadly, not just "not silence." The upstream
  notebook points at specific pre-built negative datasets — check what it currently
  recommends rather than sourcing this ad hoc.
- **Hard negatives**: phonetically similar phrases. Our own calibration data is
  directly useful here — the false-accept transcripts in
  `logs/wake_calibration.jsonl` (background conversational speech that scored
  0.8-0.99 on the `hey_jarvis` stand-in) and the fact that saying "hey cortana"
  itself triggered `hey_jarvis` twice in calibration are exactly the kind of
  confusable audio a hard-negative set should include.
- **RIR / background noise** for augmentation — room impulse response datasets are
  standard in this space (e.g. the kind of survey datasets published for acoustic
  research); pick one the upstream notebook already integrates with rather than
  sourcing separately.

**Compute:** the classifier head is small — the RTX 3080 Ti here is more than
enough, and CPU-only training is plausible too, just slower. The bulk of wall-clock
time is generating and augmenting thousands of synthetic clips, not the training
step itself.

## Decisions to make before running this

1. **Wake phrase exact wording** — "hey cortana" vs. "cortana" alone vs. an
   alternative if "cortana" proves acoustically difficult (short, common-ish
   syllables can be harder to key on reliably — CLAUDE.md/PLAN.md already flag this
   as worth testing).
2. **Voice diversity target** — how many distinct TTS voices/accents to cover; more
   matters more than volume-per-voice.
3. **Evaluation set** — before promoting a trained model over the `hey_jarvis`
   stand-in, it needs the same treatment `wake_calibration.py` already gives:
   a live-mic session mixing genuine wake-phrase utterances with normal
   conversation and background media, scored the same way (confidence
   distribution, real-vs-false breakdown by transcript). Don't skip this just
   because the model was "trained properly" — a bad negative set produces the same
   false-accept problem a stand-in model has.
4. **Threshold + verification**: even a well-trained model should probably keep the
   STT verification gate on initially, and only relax it once calibration data
   shows the trained model's score genuinely separates real from false at some
   threshold — same evidence bar as the stand-in, no assumptions carried over.

## Once a model exists

Drop the trained `.onnx` file somewhere under the repo (e.g.
`config/models/wake/hey_cortana.onnx`) and point `[audio.wake].model` in
`cortana.toml` at that path. `wake.py`'s `_resolve_model_path()` already falls back
to treating `model` as a direct path if it isn't one of the bundled names, so no
code change should be needed — just the config value and re-running calibration.
