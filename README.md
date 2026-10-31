# FocusFlow

A Windows accessibility assistant for hand tremor and shaky mouse control. It sits between your mouse and Windows,
steadies the **real pointer**, helps you land on the control you meant, and clicks for you when you hover — and it
measures, from real input, whether any of that actually helps.

Everything runs locally. Nothing leaves the PC.

## Optional ML motion classifier

FocusFlow now includes an **offline transfer-learning pipeline** for classifying pointer recordings as
`steady`, `intentional`, or `tremor_like`. It uses the frozen encoder from Amazon's pretrained
[Chronos-T5-Tiny](https://huggingface.co/amazon/chronos-t5-tiny) time-series model, pools embeddings from
the horizontal and vertical velocity signals, and trains a standardized logistic-regression head.
Seven motion features preserve amplitude and direction information. This is a trained classification
head on pretrained features, **not full encoder fine-tuning**. Chronos is a general time-series model,
not a model pretrained to diagnose tremor; its [embedding API](https://github.com/amazon-science/chronos-forecasting)
supports this downstream use.

The ML commands run on Windows, macOS, and Linux independently of the Windows desktop application.
Use Python 3.11 in a separate environment (PowerShell: replace `.venv-ml/bin/python` with
`.venv-ml\Scripts\python.exe`):

```sh
python3.11 -m venv .venv-ml
.venv-ml/bin/python -m pip install -r requirements-ml.txt
.venv-ml/bin/python -m focusflow.ml generate-demo --output data/ml/demo.csv
.venv-ml/bin/python -m focusflow.ml predict --data data/ml/demo.csv --model artifacts/motion-demo --output artifacts/demo-predictions.json
# Optional: train a fresh head using the bundled pretrained weights, without downloads.
.venv-ml/bin/python -m focusflow.ml train --data data/ml/demo.csv --output artifacts/motion-retrained --base-model artifacts/motion-demo/backbone --offline
.venv-ml/bin/python -m unittest discover -s tests -p test_ml.py
```

Training with the default remote base model downloads pretrained weights from Hugging Face;
recordings are never uploaded. The commands above reuse the bundled weights offline.
Prediction uses only the saved local weights and needs no network. The generated bundle contains
`backbone/model.safetensors`, `classifier.json` (scaling and learned weights), and `metrics.json`.
Copy the **whole model directory** to another machine for inference. `requirements-ml-lock.txt`
records the exact verified package versions. The trained synthetic demo bundle is included in Git,
with the backbone's Apache-2.0 license and attribution. Other generated artifacts, cached downloads,
environments, and recordings are gitignored. Generation and training refuse to overwrite existing outputs.
Training can reuse a downloaded bundle with `--base-model artifacts/motion-demo/backbone --offline`;
`--revision COMMIT_SHA` pins a remote backbone version. The report records the resolved model commit,
dataset SHA-256, dependency versions, and group membership of each split.

No labeled recordings were supplied with this repository, so the generated model is a **synthetic
demonstration**. It is not validated for people with tremor and does not change cursor movement,
dwell timing, or click decisions. Class scores are uncalibrated, and real signals outside the training
distribution can receive incorrect high scores. The existing filters remain the desktop runtime.

### Train on real pointer recordings

Provide a CSV with this header (timestamps in seconds, coordinates in physical screen pixels):

```csv
session_id,group_id,timestamp_s,x,y,dpi_scale,label,source
session-001,participant-001,0.000000,500,400,1.5,steady,recorded
session-001,participant-001,0.016667,501,400,1.5,steady,recorded
```

The two rows illustrate the format; each session needs at least two seconds of samples at 60 Hz or
higher. `dpi_scale` is physical pixels per logical pixel (150% scaling = 1.5). Keep one label, scale,
and participant per session; start a new session after a label or monitor-scale change. Use the same
anonymous `group_id` for **all recordings from the same participant**. Each class needs at least two
independent groups, with substantially more participants needed for meaningful evaluation.

On Windows, explicitly capture sessions with the included recorder. Pause FocusFlow assistance first
to capture unassisted movement, stay on one monitor, and choose the label for the requested task:

```sh
python -m focusflow.ml record --output data/ml/session-001.csv --session session-001 --group participant-001 --label steady --seconds 20
python -m focusflow.ml train --data data/ml/labeled-recordings.csv --output artifacts/motion-real
```

Combine your recordings into `labeled-recordings.csv` with one header, preserving session and participant
IDs. Labels should describe observed movement; `tremor_like` is a signal category, not a diagnosis.
Existing SQLite statistics and CSV analytics exports lack timestamped paths and cannot train this model.

Preprocessing divides paths by DPI scale, resamples contiguous segments to 60 Hz, and creates
non-overlapping two-second velocity windows. It rejects invalid timestamps/coordinates, splits at
gaps over 100 ms, and discards incomplete trailing windows. A seeded 75/25 **group** split keeps
participants out of both sets; scalers and heads fit only training windows. Reports include accuracy,
macro-F1, per-class results, confusion matrices, a majority baseline, and a motion-features-only
logistic-regression baseline. Synthetic scores do not measure real accessibility benefit or establish
that the pretrained model outperforms the simpler baseline.

The generated seed-42 demo bundle in `artifacts/motion-demo` uses 201 training windows and 69 held-out
windows from 90 independent simulated sessions. Its held-out accuracy is **95.65%**, macro-F1 **0.9454**;
the motion-features-only baseline reaches **100%** on this simple synthetic task. These results support
testing the pipeline, not preferring Chronos over the simpler baseline. The self-contained bundle is
approximately 32 MB; the prediction command generates `artifacts/demo-predictions.json` locally.

The reusable Python API is `focusflow.ml.model.MotionClassifier(model_dir).predict(velocity)` where
`velocity` has shape `(N, 2, 120)` in logical pixels per second sampled at 60 Hz; `read_windows` in
`focusflow.ml.data` performs the CSV preprocessing. Load once and run outside the mouse-hook thread.

### Separate synthetic stress evaluation

`generate-stress` adds twelve harder scenario families: sensor noise, slow drift, correlated noise,
noise bursts, intentional movement with oscillations, curved movement, deliberate corrections,
stop-start movement, low-amplitude tremor-like oscillations, variable-frequency chirps, mixed
frequencies, and amplitude modulation. All sessions include randomized parameters, orientation, and
display scale. Each session has an independent deterministic random stream; seed/index IDs have a
separate `stress-v1` namespace. Reusing a seed intentionally reproduces the same sessions, not new data.
The `scenario` CSV column is audit metadata and never enters the feature vectors.

```sh
.venv-ml/bin/python -m focusflow.ml generate-stress --output data/ml/stress-v1.csv --groups 240 --seed 2026
.venv-ml/bin/python -m focusflow.ml evaluate-stress --data data/ml/stress-v1.csv --reference-data data/ml/demo.csv --model artifacts/motion-demo --output artifacts/motion-stress-v1 --seed 42
.venv-ml/bin/python -m focusflow.ml predict --data data/ml/stress-v1.csv --model artifacts/motion-stress-v1/model --output artifacts/motion-stress-v1/reloaded-predictions.json
```

Use fresh output paths for another run: generation and evaluation reject existing outputs. Neither
command modifies the demo dataset or bundled model. If the original demo CSV is unavailable, reproduce
it with `generate-demo --groups 90 --seed 42` at a new path and pass that as `--reference-data`.
The evaluator verifies its SHA-256 against the original training report. Since the old feature baseline
was not saved, it reconstructs that head using **only the original recorded training groups** and
checks that its original held-out accuracy and macro-F1 reproduce the saved report. The original
Chronos head is loaded as saved. Original and challenge dataset IDs must not overlap.

Stress sessions are split approximately **60% training / 20% validation / 20% challenge-test**, stratified
by class and grouped by participant ID. All windows from a session and all sessions from a participant
stay in one partition. At least five independent groups per class are required; the split must contain
every class in each partition. Both new heads retain `C=0.1` and the existing feature schema: seven
engineered features, or 512 frozen Chronos features plus those same seven. Scalers and heads fit only
the stress training partition. Validation is reported separately; no parameter selection or refitting
uses validation or challenge data. All four classifiers see exactly the same held-out windows.

Outputs in the new evaluation directory:

- `metrics.json`: dataset/model hashes, versions, full group/session split manifests, validation and
  challenge accuracy, macro-F1, per-class precision/recall/F1, confusion matrices, and scenario diagnostics.
- `predictions.json`: matching window IDs, true labels, and all four models' predictions and scores.
- `original-feature-baseline.json` and `stress-feature-baseline.json`: the reconstructed and new baseline heads.
- `model/`: the stress-trained Chronos head and a local backbone copy, compatible with the existing `predict` command.

Tremor-like false-positive rate is **non-tremor windows predicted tremor-like / all non-tremor windows**;
the report includes counts and attribution to steady versus intentional signals. A scenario containing
only tremor-like labels has an undefined rate (`null`). Confusion matrices use true classes as rows and
predicted classes as columns, in `steady, intentional, tremor_like` order. Macro-F1 always averages all
three classes, including in single-class scenario diagnostics; overall partition metrics are the main comparison.

Measured synthetic run (generator seed 2026, split seed 42): 240 sessions / 720 windows, split into
144/48/48 sessions and 432/144/144 windows. The challenge set has 48 windows per class.

| Classifier | Validation accuracy | Challenge accuracy | Challenge macro-F1 | Tremor-like false positives |
|---|---:|---:|---:|---:|
| Original Chronos head | 63.19% | 66.67% | 0.6493 | 15/96 (15.63%) |
| Original feature baseline (reconstructed) | 82.64% | 80.56% | 0.8033 | 15/96 (15.63%) |
| Stress-trained Chronos head | 95.83% | 95.14% | 0.9514 | 3/96 (3.13%) |
| Stress-trained feature baseline | 94.44% | 95.14% | 0.9507 | 0/96 (0%) |

The stress-trained Chronos head has higher tremor-like recall (91.67% versus 85.42%) but more false
positives than the stress-trained baseline. Its remaining false positives are two intentional-with-
oscillation windows and one stop-start window; three low-amplitude and one amplitude-modulated
tremor-like windows are missed. No winner is selected or installed into the cursor runtime.

These results describe a single synthetic split. Challenge sessions are independent draws from the
same scenario families as training, not unseen scenario types or real participants. Windows within a
session are correlated. Labels follow the simulation: deliberate movement with oscillations remains
`intentional`, while noisy stationary input is `steady`; low-amplitude signals can be ambiguous. The
results are not clinical validation or evidence of real-world accessibility benefit. Existing datasets,
models, commands, seven-feature ordering, and cursor behaviour are preserved.

## Run it

```
pip install -r requirements.txt      # comtypes, pystray, pillow   (Python 3.10+, Windows 10/11)
python -m focusflow                  # or double-click focusflow.pyw (no console window)
```

FocusFlow lives in the system tray (green ring = assisting, grey bars = paused) and opens its window on start.
Closing the window hides it to the tray; **Quit** is in the tray menu.

| Shortcut | Action |
|---|---|
| Ctrl+Alt+F | pause / resume assistance |
| **Ctrl+Alt+Shift+F** | **emergency stop** — all assistance off *and* the mouse hook removed (Ctrl+Alt+F resumes) |
| Ctrl+Alt+D | show / hide the window |
| Ctrl+Alt+P | next profile |
| Ctrl+Alt+L | Live Lab |
| Ctrl+Alt+K | on-screen keyboard |
| Ctrl+Alt+O | settings |
| Ctrl+Alt+Z | undo the last undoable action (asks first) |
| Ctrl+Alt+N / M | switch navigation: next target / select |

Shortcuts are `RegisterHotKey` registrations (nothing else about the keyboard is observed) and are editable in
`%APPDATA%\FocusFlow\settings.json`.

## The five sections

1. **Pointer Assist** — jitter reduction and its strength, target attraction, dwell clicking, target adaptation,
   snap & click protection, tremor sensitivity. Every control is dwell-clickable.
2. **Adaptive Profiles** — *Precision* (minimal), *Comfort* (balanced; the original PointerLab filter, unchanged),
   *Maximum Assistance*; save your own; per-application profiles that apply while that program is in front.
3. **Accessibility Tools** — large high-contrast cursor, pointer trails, target highlighting, dwell on-screen
   keyboard, voice commands, switch navigation. Each is independent.
4. **Live Lab** — raw vs stabilised pointer path from real mouse input, the gap between them, speed, direction
   changes, jitter events, detected target boundaries, dwell progress, active profile and strength. Also launches the
   measured experiment (below).
5. **Performance Analytics** — everyday pointer statistics, per-mode experiment comparison, session history,
   CSV / JSON / PDF export. A browser dashboard with the same data is at `http://127.0.0.1:47321/`.

## How the pointer stabilisation works

PointerLab's `pointer.ts` was ported to `focusflow/pointer.py` (`AdaptivePointer`, `ErraticDetector`,
`MotionTracker`, `nearbyTarget`, `recordMiss`). The algorithms and **all constants are the originals** (3 Hz base
cut-off + 0.09 × speed, 2.5 Hz resistance to brief reversals near a button, 0.65 px dead-band, 8 px maximum lag, 150 ms
gap reset, 12 Hz settling, 3 px segments, 6 short reversals in 1.2 s, 36 px / 8 px ambiguity for nearby targets,
3 misses to adapt).

A low-level mouse hook receives every move. While jitter reduction is on, the hook **swallows** the move and the
pipeline places the cursor itself with `SetCursorPos` (verified here: `SetCursorPos` does not re-enter the hook).
Because a swallowed move does not move the OS cursor, the pipeline integrates the OS-computed deltas into its own raw
position, filters that, and a 125 Hz thread keeps *settling* the pointer onto the raw position after you stop.
Clicks land where the filtered pointer is — the same rule as the original lab ("the displayed pointer selects the
target").

### What differs from the TypeScript original (and why)

| Change | Reason |
|---|---|
| Distance constants × display scale | The original is in CSS px; Windows reports physical px. Scale is read per monitor under the pointer (150 % → lag cap 12 px = the same visual 8 px). Scale 1 reproduces the original exactly. |
| `cutoff_scale` (smoothing strength 0–10) | The original had one fixed curve. Strength 5 = 1.0 = the original; each +3 halves the cut-off. The curve shape and the lag cap are untouched. |
| Filter fed at ≤ 250 Hz | A browser delivers ~60–120 `pointermove`/s; a gaming mouse reports 1000+. Faster feeding changes the filter's speed estimate. |
| Raw position integrated from OS deltas | Browser events were absolute and un-swallowed; native moves are not. Resynchronises after any pause > 150 ms or a > 500 px jump (tablets, remote desktop, other apps moving the cursor). |
| `MotionTracker.corrective` | New counter: reversals ≤ 24 px, for the "corrective movements" metric. Direction-change rule unchanged. |
| `Attractor` | **New, not in the original.** A bounded pull toward one unambiguous target, applied as an *offset* so the lag bound is untouched; only while slow; never over text; drains the moment you move deliberately. |
| `nearbyTarget` radius | The original's 36 px / 8 px rule is kept for attribution of misses; the *redirect* radius is a separate, profile-controlled setting. |

The erratic-movement detector only ever **recommends** stronger smoothing, and is described everywhere as a heuristic,
not a medical assessment.

### Target adaptation (native version of "enlarge after 3 nearby misses")

FocusFlow cannot resize other programs' buttons, so adaptation enlarges the target's *hit area*: after 3 nearby,
unambiguous misses in a row (a success clears the streak) clicks within 2 × the normal radius are corrected onto that
target. It never applies to text/document surfaces, is logged, shown with a thicker outline, and is undone from
Pointer Assist.

## Experiments and analytics

Live Lab → pick a mode → **Start task**: ten activations in a fixed route over five numbered targets, using your real
mouse and/or dwell. Modes: *Normal mouse operation*, *Jitter reduction only*, *Dwell clicking only*,
*Target adaptation only*, *Full FocusFlow assistance* (each masks the others; your profile still decides what is
enabled inside a mode). Recorded per run: raw and stabilised distance, direction changes, corrective movements,
successful and incorrect clicks, near-misses, accidental activations (wrong control), jitter flags, completion time.
Only completed runs are saved. Everything is in SQLite (`%APPDATA%\FocusFlow\activity.db`) and exports to CSV, JSON and
PDF (a dependency-free PDF writer). "Sample data" is flagged and kept separate from real numbers.

## Dwell on minimise / maximise / close

Window caption buttons are dwell targets in every window, including FocusFlow's own and DPI-unaware programs.
Two facts shaped how:

* **A synthesised mouse click does not press a non-client caption button** (verified here on Character Map and on test
  windows: client-area clicks arrive, caption-button clicks do nothing). So dwell does not click these buttons; it
  sends the same system command Windows would (`SC_MINIMIZE`, `SC_MAXIMIZE` / `SC_RESTORE`, `SC_CLOSE`). A program
  that asks "save changes?" on close still asks.
* **UI Automation can report the wrong button** for DPI-unaware windows, so Windows' own `WM_NCHITTEST` decides which
  button the pointer is on, and a short sideways scan finds its exact edges (`focusflow/caption.py`).

Safety: only when the pointer is *inside* the button (never snapped to, attracted to, or used for near-miss redirects)
and **Close waits 1.5× the dwell time**. Programs that draw their own title bar and answer `HTCAPTION` over their
buttons are left to UI Automation, and are *not* covered by the system-command route (see limitations).
`python tests/e2e_caption_dwell.py` runs the whole thing against a real window.

## Accessibility tools

* **Large cursor / trails / highlighting** are drawn on a click-through overlay at the pointer; your system cursor
  settings are not modified, so turning them off is instant and total.
* **On-screen keyboard** — dwell keys that *send* characters to the program you were using (`WS_EX_NOACTIVATE`, so it
  never steals focus). FocusFlow only sends keys; it never reads them.
* **Voice commands** — Windows' built-in recogniser (SAPI), fixed command grammar, on-device, nothing recorded.
* **Switch navigation** — Next/Select hotkeys step through the controls of the window in front (found via UI
  Automation) with an optional auto-scan.

## Safety and reliability

* Emergency stop (above) and tray item; the engine fails *open*: an exception in the hook handler passes the event
  through untouched, and an exception in the pointer path switches stabilising off and tells you.
* Multi-monitor virtual desktop bounds and per-monitor DPI are refreshed twice a second.
* The hook ignores input injected by other programs (accessibility tools, remote desktop) and FocusFlow's own.
* Settings migrate from the previous version (a one-time `settings.v1.bak` is written); the SQLite database is
  versioned (`PRAGMA user_version`) and upgraded in place — **no event is ever dropped**. Launcher-era "close this
  program" undo records are disarmed so they can never act on a recycled PID.
* The local server binds to 127.0.0.1 exclusively, checks the Host header, needs a per-install token on every API
  call and sends no CORS headers.

## Privacy

Stored locally in `%APPDATA%\FocusFlow\` (`FOCUSFLOW_HOME` relocates it). No screenshots and no keystroke capture:
the only hooks are the mouse hook and `RegisterHotKey`. The log stores control labels (e.g. "OK"), never field
contents (switch it off in Settings). Voice audio is never saved.

## Removed

Study Mode, the notes launcher, the timer / Pomodoro window, the launcher dock and its shortcuts, and the
experimental v1 "damping" (replaced by the real filter). Old settings are migrated; old history is preserved.

## Tests

```
python -m unittest discover -s tests            # 150+ tests (live tests skipped unless enabled)
node --test tests/core.test.js                  # 8 tests: the browser extension's JS logic
set FOCUSFLOW_LIVE_TESTS=1 && python -m unittest tests.test_hook    # 4 LIVE tests — see below
```

Covered: filter behaviour on small jitter, rapid movement, intentional reversals, sudden stops, long gaps and
display scale; detector/tracker/adaptation rules; the attractor; the pipeline; engine click policy and feature masks;
profiles; settings and database migration; exports (including PDF structure); the experiment recorder; voice grammar;
switch navigation; the HTTP API; every UI page, the overlay, the keyboard and a full task run.

**The live tests move your real pointer for ~7 seconds** (real hook, real `SetCursorPos`, relative `SendInput`).
Keep your hand off the mouse while they run — real movement leaks into the measurement and can fail them.

## Measured on this development PC (not a clinical claim)

* Injected 8 Hz tremor of ±3 px at 250 Hz through the live hook: the real cursor travelled **22–26 % less** than the
  raw input at the default strength and **43–57 % less** at strength 9 (path length, 3 runs each).
* A 400 px sweep at ~1000 px/s: the real cursor was within 1.5 px of the raw end position ~22 ms after the last input;
  lag during the sweep stays at or under the 8 px (display-scaled) cap.
* CPU, one core, measured per thread: ~4–6 % idle with the window hidden; ~3 % with Pointer Assist open; ~25 % while
  Live Lab is open *and* the pointer moves continuously (it is a diagnostic page). Resident memory ≈ 80 MB. UI
  Automation probing of other programs adds to this depending on how heavy their accessibility trees are.

## Verified vs. still needs physical testing

**Verified on Windows 11 (150 % scaling), by automated tests and live runs:** the low-level hook delivering absolute
positions for relative input; swallow + `SetCursorPos` replacement with no echo; tremor reduction and prompt settling
on the real cursor; assistance off ⇒ 1 : 1 pointer; engine behaviour with mocked Windows calls; settings/database
migration on a copy of real data; every page, overlay layer, the keyboard window and the task flow rendering and
working; exports; the API; the voice recogniser attaching to the real microphone and the grammar loading.

**Not verified — needs a person and real hardware:**

* A *physical* mouse, trackpad or high-polling-rate gaming mouse. The tests inject relative input, which is not the
  same as hardware. Real tremor has irregular timing and amplitude the synthetic signal does not.
* **Voice recognition of actual speech.** The recogniser starts and reports "listening", but feeding it audio in this
  session did not produce events, so spoken commands are untested. Needs a microphone and someone speaking.
* **Multiple monitors at mixed DPI** (per-monitor scale is implemented and read, but only one display was available).
* The on-screen keyboard typing into other programs and switch scanning against many real applications (the key
  sending uses `SendInput` Unicode events; compatibility with elevated/administrator windows is not expected).
* Whether the default strengths *feel* right for a given person. Use the Live Lab and experiments to tune them.
* Chrome itself with the extension (its stable build blocks `--load-extension`; load it manually).

## Known limitations

* Programs running **as administrator** receive no assistance from a non-elevated FocusFlow (Windows hook rules).
* Synthetic page clicks cannot open pop-ups (`target=_blank`) because the browser requires a real user gesture.
* Chromium only exposes its page UI Automation tree if accessibility is on, so page content relies on the extension.
* Near-miss redirect is deliberately conservative (unambiguous nearest target, never on text) and can be switched off.
* Dwell on caption buttons of apps with a fully custom title bar (some Electron/Office-style frames) is not
  guaranteed: it needs the app to answer `WM_NCHITTEST` honestly. Native, UWP/WinUI and standard Chromium frames do.
* Windows pointer acceleration is applied *before* FocusFlow sees movement; FocusFlow does not change it.
