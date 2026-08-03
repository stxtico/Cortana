# Training a real "hey cortana" wake word

**Status: done.** `wake.py` now runs on a trained `hey_cortana` model
(`config/models/wake/hey_cortana.onnx`), not the `hey_jarvis` stand-in. See
"Final result" near the end of this document for the live comparison that
justified the swap. Everything below is the process that got there, kept
because the compatibility fixes and lessons apply directly to training a second
model later (e.g. bare "cortana", see the decision section below).

Original problem: the `hey_jarvis` stand-in had a real false-accept problem on
conversational audio, and confidence score alone couldn't separate real
detections from false ones. A verification gate (STT confirms the phrase before
entering RECORDING, `[audio.wake].verify` in `cortana.toml`) worked around this
temporarily. Training the right model was the actual fix.

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

## Dry-run checklist — every fix, so the full run doesn't rediscover them

Ran a deliberately small dry run first (300 positive samples, 1000 training steps,
40 RIR clips, 60 background clips) specifically to hit these before committing
hours to the full-scale version. In the order they were actually hit:

1. **`webrtcvad` needs a C compiler** — no prebuilt wheel for this platform/Python
   combo. Needs `sudo apt-get install -y build-essential` before `uv pip install`.
2. **`pyarrow` + `numpy` version floor, together** — modern default `pyarrow`
   dropped the `PyExtensionType` API `datasets==2.14.6` needs; modern `pyarrow`
   also requires `numpy>=2`, which then breaks `pyarrow<15` itself. Pin both:
   `pyarrow<15`, `numpy<2`.
3. **`setuptools<82`** — 82+ removed `pkg_resources` outright; `webrtcvad` (and
   others in this stack) still import it.
4. **`torchcodec` + system `ffmpeg`** — `datasets` needs `torchcodec` for audio
   decoding, which needs `ffmpeg` installed system-wide (`sudo apt-get install -y
   ffmpeg`), not just `pip install torchcodec`.
5. **Three removed `torchaudio` APIs, all shimmed in `compat_patch.py`** (`import
   compat_patch` before anything else in any training script):
   - `torchaudio.info()` — removed in torchaudio 2.10+; `openwakeword/data.py`
     calls it directly. Shimmed via `soundfile.info()`.
   - `torchaudio.list_audio_backends()` — removed; `speechbrain==0.5.14` expects
     it. Stubbed to return `["soundfile"]`.
   - `torchaudio.set_audio_backend()` — removed; `torch_audiomentations` calls it
     at **import time**. Stubbed as a no-op.
6. **`torch.load` defaults to `weights_only=True` since PyTorch 2.6** —
   deep-phonemizer's checkpoint (its own official hosted model, trusted source)
   pickles a custom class that trips the new default. Patched `torch.load` to
   default `weights_only=False` in `compat_patch.py`.
7. **`generate_samples` root-module shim** — `train.py` does `from
   generate_samples import generate_samples`, expecting a root-level module (true
   when the notebook was written); `piper-sample-generator` has since restructured
   into a package (`piper_sample_generator/__main__.py`). This is the same root
   cause as the rejected fork's issue #1, but real upstream drift, not
   fork-specific. Fixed with `piper-sample-generator/generate_samples.py`
   re-exporting the function.
8. **`piper-sample-generator`'s own `numpy>=2,<3` requirement conflicts with floor
   #2** — don't `pip install -e ./piper-sample-generator` as a package; install
   `piper-tts` directly and rely on `sys.path` insertion (which `train.py` already
   does) to reach `generate_samples`.
9. **`train.py` never passes `model=` to `generate_samples`** — relies on a
   default the current `piper_sample_generator` no longer provides (required arg,
   no default). Same "Piper v2+ `model=`" issue the rejected fork's patches
   addressed. Fixed by wrapping the shim function with `functools.wraps` and a
   default pointing at the downloaded voice model.
10. **Generated clips are 22050Hz, not 16000Hz** — `en_US-libritts_r-medium`'s
    native rate; `generate_samples` has no target-sample-rate option, and the
    augmentation pipeline hard-assumes 16kHz and raises if it isn't
    (`ValueError: Error! Clip does not have the correct sample rate!`). Added
    `resample_clips.py` as a required post-generation, pre-augmentation step.
11. **A crashed run leaves partial feature `.npy` files that get silently
    "already exists, skipping" on retry** — delete them or pass `--overwrite` to
    `train.py` when resuming after any failure during `--augment_clips`.
12. **`onnxscript` required for torch's modern ONNX export path** — without it,
    `torch.onnx.export` fails at the very last step (`ModuleNotFoundError: No
    module named 'onnxscript'`) after training has already completed. Install it
    up front, not as an afterthought.
13. **`onnxscript` needs modern `protobuf`, which conflicts with the
    `tensorflow-cpu==2.8.1` pin** — resolved by **dropping TensorFlow entirely**
    (`tensorflow-cpu`, `tensorflow_probability`, `onnx_tf`). Confirmed via source
    inspection that TF is only imported inside `train.py`'s
    `convert_onnx_to_tflite()`, gated behind `--convert_to_tflite`, which we never
    pass — we only need `.onnx` (`wake.py` loads via `onnxruntime`). No reason to
    fight that pin at all once you know the tflite path is truly optional.
14. **`train.py`'s `--convert_to_tflite` gate is buggy and runs unconditionally** —
    every CLI flag defaults to the *string* `"False"` (`argparse` quirk:
    `action="store_true"` paired with `default="False"`), and `bool("False")` is
    `True` in Python. Every other flag correctly checks `if args.X is True:`
    (identity, not truthiness) — this one checks `if args.convert_to_tflite:`
    (plain truthiness), so it always runs regardless of whether the flag was
    passed. Confirmed by direct observation (it ran and crashed on a missing
    `onnx_tf` after we'd deliberately removed it). Since this is an editable local
    clone, fixed directly: changed line 908 to `if args.convert_to_tflite is
    True:`, matching the pattern used everywhere else in the file.
15. **The exported `.onnx` uses external-data storage** — `torch.onnx.export`'s
    modern path splits output into a small graph file (`<name>.onnx`, ~14KB here)
    and a separate companion weights file (`<name>.onnx.data`). Copy **both** back
    to Windows — `onnxruntime` fails opening the graph file alone with `External
    data path does not exist`.

**Confirmed after all of the above**: the dry-run `.onnx` (300 positive samples,
1000 steps — deliberately tiny) loads via this repo's actual production
`services/ears/wake.py` code path (not just the training-side openWakeWord, which
has its own unrelated bugs on the git-main branch we cloned), accepts real
microphone audio without crashing, and produces bounded, sane scores (0.0-0.043 on
silence, a synthesized "hey cortana", and live mic "hey cortana" alike — flat and
non-discriminating, consistent with the training run's own reported accuracy 0.5 /
recall 0.0 for a model this undertrained, not a sign of a broken export). Pipeline
mechanics confirmed end to end; model *quality* is what the full-scale run is for.

## Hard-negative save path — verified with a deliberate reject

Before trusting `services/ears/hard_negatives/` for the full run, forced a real
reject rather than just reasoning about the code: temporarily set
`[audio.wake].threshold` to `0.0000001` in `cortana.toml` so any ambient sound
triggers, ran a 20s probe. All 10 triggers correctly rejected (none said
"jarvis") and all 10 saved as real WAV files with matching `manifest.jsonl`
entries. Save path confirmed working — the previously-empty directory was purely
because the capability postdated the run that had genuine false accepts, not a
bug. Deleted the artificially-forced test clips afterward (near-zero threshold
noise isn't representative) and reverted the config.

## Voice diversity: 6 Piper voices, not 1

The notebook's own default only uses one voice (`en_US-libritts_r-medium`,
multi-speaker LibriTTS). Added 5 more spanning accent and gender:

| Voice | Region | Gender |
|---|---|---|
| `en_US-libritts_r-medium` | US | multi-speaker |
| `en_US-amy-medium` | US | F |
| `en_US-ryan-medium` | US | M |
| `en_GB-alan-medium` | GB | M |
| `en_GB-northern_english_male-medium` | GB (northern) | M |
| `en_GB-jenny_dioco-medium` | GB | F |

**These required a real conversion pipeline, not just a download** — the actual
work, since it's not documented anywhere obvious:

1. `piper-sample-generator`'s own v2.0.0 release only ships `.pt` checkpoints for
   **one English voice** (the libritts_r one we already had); the other three are
   German/French/Dutch. No additional English `.pt` files exist there.
2. Found the real source: `rhasspy/piper-checkpoints` on HuggingFace hosts
   PyTorch Lightning `.ckpt` training checkpoints for many more voices (these are
   what produced the deployed `.onnx` voices, several hundred MB each, `dict`
   with optimizer state etc. — not directly loadable by `generate_samples`).
3. Wrote `convert_checkpoint.py`: loads the `.ckpt` via
   `piper_train.vits.lightning.VitsModel.load_from_checkpoint(ckpt_path,
   dataset=None, weights_only=False)` (Lightning's `save_hyperparameters()` means
   no config args need to be passed manually), extracts `model.model_g` (the
   `SynthesizerTrn` generator submodule — confirmed by reading `generate_audio()`
   in `piper_sample_generator/__main__.py`, which calls `model.enc_p`/`emb_g`/
   `dp`/`flow`/`dec` directly, exactly the generator's own submodules, no
   wrapper), and `torch.save()`s it alone — matching exactly what
   `en_US-libritts_r-medium.pt` already was.
   - Needed `weights_only=False` **passed explicitly** to `load_from_checkpoint`,
     not just the `compat_patch.py` default — Lightning passes `weights_only`
     to `torch.load` as an explicit kwarg, which `setdefault()`-style patching
     doesn't override.
   - Needed `pytorch_lightning` installed (one-time, not needed by training
     itself).
4. **Found and fixed a real bug this surfaced**: `generate_audio()` calls
   `model.emb_g(speaker_1)` unconditionally, but `SynthesizerTrn` only creates
   `emb_g` when `n_speakers > 1` (confirmed in `models.py`) — **every
   single-speaker voice would crash** with `AttributeError: 'SynthesizerTrn'
   object has no attribute 'emb_g'`. This is genuinely orthogonal to the
   checkpoint-conversion work — it's a gap in `generate_samples` itself that only
   surfaces once you try a single-speaker voice, which the notebook's own
   single-voice default never does. Patched `generate_audio()` to skip speaker
   embedding when `getattr(model, "n_speakers", 1) <= 1`, mirroring the identical
   branch already in `SynthesizerTrn.forward()`.
5. Verified every voice individually, not just that conversion didn't crash —
   generated a real "hey cortana" clip per voice, transcribed with this repo's
   own `Transcriber`. Two voices failed this check:
   - `en_GB-southern_english_female` has no training checkpoint at all in
     `piper-checkpoints` (only the deployed `.onnx`) — never attempted.
   - `en_GB-alba` converted and generated cleanly (no crash) but consistently
     mispronounced "cortana" across 5 separate clips (`"Hiko Taino"`, `"He
     called Tanner"`, etc.) — not a fluke, a real quality problem specific to
     that voice/checkpoint for this word. Replaced with `en_GB-cori` (no
     `config.json` published for it — dead end) then `en_GB-jenny_dioco`, which
     passed (2 of 4 test clips transcribed exactly, the other 2 recognizably
     close — normal TTS/ASR variance, not systematic failure).
6. `generate_multivoice_positives.py`: splits `n_samples`/`n_samples_val` evenly
   across all 6 voices with unique per-voice filenames, writes directly into
   `train.py`'s expected `positive_train`/`positive_test` directories. Run this
   **before** `train.py --generate_clips` — it'll see enough positives already
   exist and skip that part, while still generating negatives/adversarial text
   normally. Tested at small scale (12 samples, 2/voice) before trusting it.

## Throughput logging — the dry run's slowdown mystery, solved

The dry run showed step rate collapsing from ~140 it/s to ~3 it/s partway
through, unexplained at the time. Added logging to `train.py`'s training loop
(`throughput_log.jsonl`, interval configurable via `OWW_THROUGHPUT_LOG_INTERVAL`,
defaults to every 100 steps) rather than parsing tqdm's carriage-return output,
and re-ran the dry run to capture it cleanly.

**Root cause, found from the log, not guessed**: `train_model()`'s sequence 1
sets `val_steps = np.linspace(steps - int(steps*0.25), steps, 20)` — validation
checkpoints are **20 fixed checks concentrated in the last 25%** of steps,
regardless of how big `steps` is. The dry run's slowdown began at step 750 out of
1000 — exactly the 75% mark. Not a runaway degradation; a fixed-count cost that
becomes a *smaller* fraction of total time as `steps` grows. Sequences 2 and 3
(each `steps/10`) spread their 20 validations across their *entire* range instead
of just the last quarter, so they ran uniformly slower in the dry run — but at
full scale their range is 10x bigger too, so validation stays similarly sparse
relative to sequence 1's tail.

**Revised wall-clock estimate for training, much lower than originally guessed**:
roughly **15-45 minutes** for the full 50,000-step regime (down from the
original 3-3.5 hour guess), because the validation overhead is ~20 checks per
sequence total, not something that scales with step count. The remaining honest
uncertainty: the full run's `batch_n_per_class` for the ACAV100M component is
larger (1024 vs. the dry run's 256) — a 4x bigger batch could slow the
steps/sec rate itself, independent of validation, in a way not tested at that
exact scale. Generation + augmentation remains the more confidently-estimated
cost at ~2-2.5 hours (scales linearly with clip count, already measured).

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

## Full run — actual numbers

Ran with the finalized config: 20,000 positive train / 3,000 val samples split
across the 6 voices, 50,000 training steps, full MIT RIR set (270 clips), 3,400
background clips (2,500 AudioSet + 900 MUSAN). Total wall-clock: **~50 minutes**
end to end (generation+augmentation ~34 min, training ~15 min across all 3
sequences) — far faster than either the original ~5-7h guess or even the
revised ~2.5-3h estimate. The throughput-logging investigation correctly
predicted training would be fast at full scale; generation/augmentation also
came in well under its linear-scaling estimate, plausibly because per-clip
overhead (model loading, etc.) amortizes better at volume than the dry run's
tiny scale suggested.

Immediate sanity checks after training, before the live test: loading via the
actual production `wake.py` code path, silence scored a clean `0.0` (vs. the
dry-run model's noisy ~0.04 baseline), and a synthesized "hey cortana" clip
produced a sharp, well-defined peak (0.81-0.97 across 4 consecutive frames,
then dropping back to near-zero) — real discrimination, not noise, unlike the
dry-run model's flat non-response.

## Final result — live comparison against the hey_jarvis + verification baseline

Same protocol as the baseline test earlier in this session: 3 minutes,
continuous unrelated speech for the first two minutes (no wake phrase), then
several genuine "hey cortana" utterances at the end. Bar to clear: fewer than
the baseline's 2-of-6 false accepts surviving verification, and zero genuine
detections wrongly rejected.

**Result: 0 false accepts survived verification. 0 genuine detections wrongly
rejected.** 12 total triggers formed 6 clear pairs — a genuine detection
immediately followed by a spurious re-trigger from openWakeWord's residual
embedding window (same score to 3 decimals, 3.6-8s later — the same phenomenon
identified with `hey_jarvis` earlier in this session, confirmed general to
openWakeWord rather than specific to either model). All 6 passes matched a
real "hey cortana" utterance in the follow-on transcript (including one
realistic full sentence: "Hey Cortana launch... give me a summary of my
assignments please"); all 6 rejects were the spurious echo saying unrelated
things ("Thank you.", "Yeah.") and were correctly caught by verification. Note
the pair gaps (3.6-8s) exceed `debounce_s` (2.0) — verification, not debounce,
is what's catching these.

Also better on raw discrimination, not just the verified outcome: zero frames
across the full 180s exceeded 0.9 (genuine hits topped out at 0.89) — the
`hey_jarvis` baseline's false accepts regularly hit 0.98-0.99, statistically
indistinguishable in confidence from real hits. This model's confidence
ceiling is lower and cleaner, a real, measured separation between real and
false, not just a verification gate papering over an unusable score.

**Not yet re-examined**: whether the verification gate can be relaxed or
dropped now that the trained model shows real score separation (decision #4
above still applies — more calibration data needed before trusting that,
this was one 3-minute session). Bare "cortana" (decision #1) also remains
unbuilt and carries the same false-accept-rate caveat noted there.
