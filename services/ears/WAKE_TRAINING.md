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

## What the pipeline needs — verified against the actual notebook, not assumed

Two upstream notebooks exist, and they are a real fork in the road, not just detail
variants:

### Path A: `notebooks/automatic_model_training.ipynb` — the proper pipeline

This is what the bundled `hey_jarvis`-class models were trained with. The notebook
states outright: **"automated model training is only supported on linux systems"**
(Piper TTS's training-side tooling is the blocker). This is not a soft
recommendation — **it will not run on native Windows.** Options: WSL2 with CUDA
passthrough on this machine, or Google Colab (a maintained community notebook
claims 75-90 min end-to-end on Colab Pro; the official notebook targets a free-tier
T4 and quotes ~10 minutes for just the synthetic-clip-generation step, not the full
pipeline).

**Downloads**, all real, sourced from the notebook and its dataset pages:
- `openwakeword_features_ACAV100M_2000_hrs_16bit.npy` — **17.3GB**, pre-computed
  embeddings (not raw audio) over ~2000 hours of negative audio
- `validation_set_features.npy` — ~11 hours, smaller
- MIT room impulse response set (HuggingFace `davidscripka/MIT_environmental_impulse_responses`)
- One AudioSet tar (`bal_train09.tar`, HuggingFace `agkphysics/AudioSet`)
- FMA small split (~1 hour)
- A Piper TTS voice model (`en_US-libritts_r-medium.pt`, small)

**Packages**: an old, version-pinned stack — TensorFlow 2.8.1, TensorFlow
Probability 0.16.0, `speechbrain==0.5.14`, `torch-audiomentations==0.11.0`,
`audiomentations==0.33.0`, `piper-phonemize`, `webrtcvad`, plus several more
pinned exactly. Pinned-old + Linux-only is a real combination to hit dependency
resolution friction on, which is part of why Colab (where this exact stack is
already known to install) is lower-risk than hand-building a fresh WSL2
environment.

**Known failure mode, not hypothetical**: [issue #110](https://github.com/dscripka/openWakeWord/issues/110)
on the upstream repo documents a user's first attempt scoring accuracy 0.627
(target 0.7), recall 0.257 (target 0.5), false-positives/hour 1.06 (target 0.2) —
"does not work effectively at all." Training here is iterative; budget for more
than one run before a model is usable, and build the evaluation step (below) in
from the start rather than trusting the first output.

### Path B: `notebooks/training_models.ipynb` — the lighter, explicitly-lower-quality path

Generates negative features on the fly from much smaller samples (FMA-large
~200 clips, FSD50k noise ~1000 clips, Common Voice ~5000 clips) instead of the
17GB precomputed set, and isn't Linux-locked the same way. The notebook's own text
calls this "for demonstration purposes" and says real robustness needs much more
data than it uses by default — a maintainer-stated quality ceiling, not just
caution. (Same issue #110 thread has a user reporting *better* results from this
path than Path A with ~10,000 samples, so "lighter" isn't strictly "worse" in
practice — but it's not the path the project's own pretrained models were built
on either.)

### The actual decision

Given the target is a real wake word this assistant depends on daily, not a demo,
**Path A is the chosen method.** Environment: **WSL2 locally**, not Colab — this
machine's own RTX 3080 Ti, driven directly rather than babysitting a browser tab.
WSL2 was already installed (Ubuntu 26.04) with working GPU passthrough (`nvidia-smi`
confirmed inside WSL2). Ubuntu 26.04 only ships Python 3.14 by default, which is
too new for the pinned `tensorflow-cpu==2.8.1`; the fix is an isolated Python 3.10
environment via `uv` (same tool this whole project already uses, just targeting
Linux) rather than touching system Python.

All training work (venv, cloned repos, datasets, generated clips) lives under
`~/wake-training` in **WSL's native Linux filesystem, not `/mnt/c`** — the
Windows-filesystem boundary is much slower for the tens of GB and thousands of
small synthetic-clip files this involves. Only the finished `.onnx` gets copied
back to `C:\dev\cortana` at the end.

A community fork (`lgpearson1771/openwakeword-trainer`) was evaluated as a
lower-friction alternative and **rejected after a provenance check**: all 6 commits
landed in a single 30-minute window 5.5 months ago and nothing since (`pushed_at`
== `created_at`), single author, 14 stars, and all 3 open issues are from different
external users reporting unresolved failures — including a confirmed crash in the
core clip-generation step (`ModuleNotFoundError: No module named 'generate_samples'`)
and an independently-confirmed-broken RIR download, reported by two different users
six weeks apart, still unfixed. No evidence anyone has gotten a working model out
of it. Not used.

Path B (`training_models.ipynb`) is worth keeping in mind as a faster iteration
loop for early phrase/voice-diversity experiments, not as the final training
method.

### Data source verification — done before starting, not discovered mid-run

Every URL/dataset call in the actual notebook (not a summary of it) was tested
directly — and re-tested against the **actual pinned `datasets==2.14.6`** used for
training, not the newer `datasets` a first verification pass happened to use. That
distinction mattered: it flipped one earlier finding.

| Source | Status | Note |
|---|---|---|
| Piper voice model (`en_US-libritts_r-medium.pt`, v2.0.0 release) | ✅ works | |
| openWakeWord v0.5.1 release assets (embedding/melspectrogram models) | ✅ works | initial test showed 404s — curl-loop artifact against GitHub's signed redirect URLs, not real; confirmed individually |
| `openwakeword_features_ACAV100M_2000_hrs_16bit.npy` (17.3GB) | ✅ works | |
| `validation_set_features.npy` (176MB) | ✅ downloaded | |
| MIT RIR (`davidscripka/MIT_environmental_impulse_responses`) | ✅ works | needed system `ffmpeg` (`datasets` needs `torchcodec` for audio decoding) |
| FMA (`rudraml/fma`) | ✅ **actually works fine** | a first pass against newer `datasets` (5.0.1) hit `"Dataset scripts are no longer supported"` and looked dead — but that's a modern-`datasets`-only deprecation. Re-tested against the actual pinned `datasets==2.14.6` (which predates the deprecation) and it loads correctly, real rows, hundreds of metadata columns. **Downloaded MUSAN anyway** (see below) as supplementary background/noise data, not as a required FMA replacement |
| AudioSet (`bal_train09.tar` direct file) | ❌ **dead** | `agkphysics/AudioSet` was restructured to parquet+config format (`data/bal_train/*.parquet`); the flat `.tar` path is gone (404, not a redirect artifact) |
| AudioSet via `datasets.load_dataset(..., "balanced", ...)` | ❌ **also broken, differently** | works against modern `datasets` (5.0.1) but fails against the pinned `datasets==2.14.6` with `TypeError: must be called with a dataclass type or instance` — the old library can't parse the repo's current config-card metadata. Version-dependent breakage on both ends |
| **AudioSet — actual fix** | ✅ works | bypass the `datasets` library for this one source entirely: `curl` a `data/bal_train/NN.parquet` file directly (verified `00.parquet`, 656MB, 500 rows), read with plain `pyarrow.parquet.read_table()`. Version-independent since it skips the library's config/script system altogether |

MUSAN (original OpenSLR source, `https://www.openslr.org/resources/17/musan.tar.gz`,
confirmed `200 OK`, 11.1GB) is downloading as extra background/noise variety since
it was already identified before the FMA re-test corrected course — no harm in
having both.

### Environment setup, as actually built (not just planned)

`~/wake-training/.venv` (Python 3.10 via `uv`) with the notebook's exact pins,
**plus compatibility floors this specific combination needed** (`requirements.txt`
records all of it):

- `numpy<2`, `pyarrow<15` — modern default `pyarrow` removed the `PyExtensionType`
  API `datasets==2.14.6` needs; modern `pyarrow` versions also drag in `numpy>=2`,
  which then breaks `pyarrow<15` itself. Both floors are needed together.
- `protobuf<3.20` — `tensorflow-cpu==2.8.1`'s generated `_pb2.py` files predate the
  upb-based protobuf implementation; anything protobuf>=3.20-ish breaks it
  (`TypeError: Descriptors cannot be created directly`).
- `setuptools<82` — 82+ removed `pkg_resources` outright, which `webrtcvad` (and
  likely others in this stack) still imports.
- `torchcodec` — needed by `datasets` for audio decoding; needs system `ffmpeg`
  (not installed by default in a fresh WSL2 Ubuntu).

`~/wake-training/compat_patch.py` — `import compat_patch` before anything else in
any training script. Patches things no version pin can fix, because the removed
APIs simply don't exist anymore in any current torchaudio:
- `torchaudio.info()` — removed in torchaudio 2.10+; `openwakeword/data.py` calls
  it directly (`get_clip_duration` etc.) for `.num_channels`/`.sample_rate`/
  `.num_frames`. Shimmed via `soundfile.info()`.
- `torchaudio.list_audio_backends()` — removed; `speechbrain==0.5.14` expects it.
  Stubbed to return `["soundfile"]`.
- `torchaudio.set_audio_backend()` — removed; `torch_audiomentations` calls it at
  **import time**. Stubbed as a no-op.

`~/wake-training/piper-sample-generator/generate_samples.py` — a shim module.
`openwakeword/train.py` does `from generate_samples import generate_samples`,
expecting a root-level module (true when the notebook was written). The upstream
`rhasspy/piper-sample-generator` repo has since been restructured into a proper
package (`piper_sample_generator/__main__.py`, function nested inside) — **this is
the exact same root cause as the rejected fork's issue #1**
(`ModuleNotFoundError: No module named 'generate_samples'`), except it's a real
upstream drift between `openwakeword` and `piper-sample-generator`, not something
specific to that fork. The shim just re-exports the function from its new location.
Also: `piper-sample-generator`'s own `pyproject.toml` wants `numpy>=2,<3`, directly
conflicting with the `numpy<2` floor above — sidestepped by installing `piper-tts`
directly rather than pip-installing `piper-sample-generator` as a package; only
`sys.path` insertion (which `train.py` already does) is actually needed to reach
`generate_samples`.

**End-to-end synthesis confirmed working**: generated 3 real "hey cortana" clips
via GPU, copied one back across to Windows and ran it through this repo's own
`services/ears/stt.py` `Transcriber` (`large-v3-turbo`) as an independent sanity
check — transcribed as `'Hey Cortana.'`, exact match.

**Hard negatives**: `scripts/wake_calibration.py` now saves the actual audio for
every rejected verification as a WAV file under `services/ears/hard_negatives/`
(with `manifest.jsonl` recording the wake score and verify transcript for each) —
real waveforms, ready to feed into whichever path is used. Earlier calibration
sessions in this project's history produced false accepts whose *text* is known
(background conversational speech that scored 0.8-0.99 and verify-transcribed as
"Hey, Jarvis") but whose *audio* was never saved — the capability didn't exist yet
at the time, and the in-memory clips are gone. Don't try to recover them; just
accumulate real ones from here forward.

## Decisions to make before running this

1. **Wake phrase exact wording** — **"hey cortana" is the primary phrase** for the
   first trained model.

   A **bare "cortana"** model has been raised as a possible second model later, but
   it should be understood as a real tradeoff before building it, not a strict
   upgrade: the project is *named* Cortana, and "cortana" as a single word is going
   to come up constantly in ordinary conversation about the project itself — "how's
   Cortana doing," "let's fix that Cortana bug," reading these very docs aloud. Every
   one of those is a live-fire false-accept opportunity in a way "hey cortana"
   structurally isn't (the "hey" prefix is a second, independent phonetic gate — a
   false accept needs to match *both* "hey" immediately preceding the name *and* the
   name itself, not just the name in isolation somewhere in a sentence). Expect a
   bare "cortana" model's real-world false-accept rate to be substantially higher
   than "hey cortana" for exactly this reason, and expect it to get *worse* the more
   this project is actively discussed near the mic. If it's built at all, calibrate
   it separately and expect a stricter verification gate (or a shorter, denser
   confirmation phrase requirement) to matter more for it than it does for "hey
   cortana."
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
