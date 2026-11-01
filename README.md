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
demonstration**. It is not validated for people with tremor. Offline commands do not change cursor movement,
dwell timing, or click decisions. Class scores are uncalibrated, and real signals outside the training
distribution can receive incorrect high scores. Optional live integration is described below.

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
tremor-like windows are missed. The evaluation command does not enable live cursor adjustments.

These results describe a single synthetic split. Challenge sessions are independent draws from the
same scenario families as training, not unseen scenario types or real participants. Windows within a
session are correlated. Labels follow the simulation: deliberate movement with oscillations remains
`intentional`, while noisy stationary input is `steady`; low-amplitude signals can be ambiguous. The
results are not clinical validation or evidence of real-world accessibility benefit. Existing datasets,
models, commands and seven-feature ordering are preserved. Cursor behaviour is unchanged by default.

### Optional live Windows ML

Pointer Assist → **Local ML (experimental)** has an enable switch, Shadow/Experimental selection,
model-folder picker, prediction, model state, inference latency, and the currently added smoothing.
ML starts **disabled**; its default mode is **Shadow**. Enabling shadow classification leaves pointer
output unchanged. Missing optional packages or weights produce an error status and retain normal assistance.

Install the ML packages into the **same Python environment that launches the desktop app**. Python 3.11
is the environment used for the local ML tests and benchmark; native Windows behavior still requires testing:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-ml.txt
.\.venv\Scripts\python.exe -m focusflow --show
```

The default model directory is `artifacts/motion-stress-v1/model`, relative to the project, independent of
the working directory. It reuses the existing stress-trained `classifier.json` and its `backbone/`.
Generated stress artifacts are gitignored: when moving to another PC, copy that existing **whole model
directory** or select its location with **Choose model folder**. Live ML never downloads weights,
re-trains, substitutes the original demo classifier, or modifies the model files. The original offline
commands still work; `resample_velocity` is shared between CSV and live preprocessing, and the seven
engineered features and trained 519-feature head are unchanged.

Acquisition uses timestamped **integrated raw input** from the existing pointer pipeline, before filtering
or attraction, rather than polling the filtered OS cursor. The hook only appends a decimated scalar tuple
to an optional 1,024-sample ring. The 125 Hz pointer thread supplies stationary samples during idle time.
The engine submits a snapshot at most twice per second to a queue with **one pending job**; a new snapshot
replaces an older pending snapshot. Only the dedicated worker imports ML dependencies, resamples a rolling
two-second window to 60 Hz, computes features, and runs the saved classifier. CPU inference uses one Torch
thread. Sampling gaps over 100 ms, teleports and DPI changes require a fresh valid window. Coordinates
are divided by DPI scale exactly as in training. Raw data stays in bounded RAM buffers; it is not written
to the database, logged, or transmitted. Disabling ML clears acquisition and queued data and invalidates
in-flight results; an already-running native inference cannot be forcibly interrupted.

To try adjustment, first check that Shadow reaches **ready** with fresh predictions, then explicitly select
**Experimental**. Its policy is deliberately limited:

- Three distinct `tremor_like` predictions with score ≥0.85 spanning at least one second activate an adjustment.
  These overlapping windows and uncalibrated scores are a heuristic, not independent confidence estimates.
- Extra smoothing ramps at 0.5 strength levels/second, adds at most two levels above the user's setting, and
  never exceeds strength 10. Scores below 0.55 or a steady label unlatch and decay the adjustment at one level/second.
- An intentional label, the existing intentional-motion heuristic, raw speed ≥250 logical pixels/second,
  dragging, disabled jitter reduction or an experiment task suppresses the adjustment. The filter's original
  eight-pixel scaled lag cap, dwell rules, attraction and click policy are untouched.
- Inference exceeding 250 ms or results more than one second old are unavailable. This also applies while
  an over-budget inference is still running. Failure/absence returns to baseline on the next engine tick.
  Each applied adjustment has a 100 ms lease, independently expired by the hook/pointer thread if engine
  refreshes stop; this is a bounded fallback, not a hard real-time guarantee.
- Manual smoothing/profile changes latch a manual override. Pause/emergency stop invalidates outstanding
  predictions and clears the adjustment synchronously. Resuming assistance restores the ordinary filter;
  toggle ML off/on to explicitly re-arm experimental adjustment. Temporary UI suspension also clears ML state.

Global settings are `ml_enabled`, `ml_mode` and `ml_model_path`; they are not saved into pointer profiles.
No ML prediction writes `smoothing_strength`. Keep the normal emergency shortcut **Ctrl+Alt+Shift+F** available.

Reproduce the tests and worker benchmark:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
node --test tests/*.test.js
.\.venv\Scripts\python.exe -m focusflow.ml.benchmark_live --output artifacts/live-benchmark-new.json
```

Measured here with the saved stress model on macOS 26.6.2 ARM64, Python 3.11.1, one Torch thread, three
warm-up windows and 30 measured windows at 2 Hz: **19.63 ms median / 21.81 ms p95 / 21.88 ms maximum**
preprocessing+inference latency. Observed queue-to-result p95 was **23.49 ms**; all 30 results were accepted
with zero pending-job drops. The benchmark process consumed **0.552 CPU seconds over 15.144 wall seconds**,
or **3.65% of one logical core**, including its synthetic-input/polling harness. Initial model loading took
3.41 seconds and is excluded from steady-state CPU/latency. Full measurements are in
`artifacts/live-ml-benchmark-verified.json`. The benchmark waveform was classified intentional; this timing
workload is not a labeled accuracy evaluation.

These timings exclude the Windows hook, desktop UI, and engine. Windows-only tests are skipped on other
platforms, not counted as successful hardware validation. Actual Windows/GIL scheduling, physical mouse
behavior, perceived latency, and live classification accuracy remain unverified. The model is trained on
synthetic data, can misclassify movement, and has no clinical validation. Shadow mode is the default for this reason.

## Run it

```
pip install -r requirements.txt      # comtypes, pystray, pillow   (Python 3.10+, Windows 10/11)
python -m focusflow                  # or double-click focusflow.pyw (no console window)
```

FocusFlow lives in the system tray (green ring = assisting, grey bars = paused) and opens its window on start.
Closing the window hides it to the tray; **Quit** is in the tray menu.

### Windows startup diagnostics

The module entry point, `focusflow.pyw`, and `run.bat` now initialize persistent diagnostics **before**
importing the Windows API, application, or Tk UI. Errors remain visible in PowerShell and are appended
to `%APPDATA%\FocusFlow\focusflow.log` (or `FOCUSFLOW_HOME\focusflow.log` when configured). If that
directory cannot be written, logging falls back to `%TEMP%\FocusFlow\focusflow.log`; the selected path
is printed at startup. Clearing a terminal does not erase this file. Python fatal-error handling also
writes to the log. A GUI-only launch displays a failure dialog when a caught startup exception occurs.

The old application redirected all stderr away from the console after `App` construction began, which
can make a genuine exception appear to be a silent exit. That behavior was reproduced in a subprocess.
It does **not** establish which exception caused a particular Windows failure. No Windows failure log
was available when these changes were made, so neither Python 3.15 nor the mutex is a confirmed root cause.

In PowerShell, from the updated project folder:

```powershell
Set-Location 'C:\Users\vardh\Downloads\FocusFlow-main\FocusFlow-main'
python -c "import sys; print(sys.executable); print(sys.version)"
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install --upgrade -r requirements.txt
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m focusflow --diagnose
$LASTEXITCODE
.\run.bat --show
$LASTEXITCODE
Get-Content "$env:APPDATA\FocusFlow\focusflow.log" -Tail 150
```

`run.bat` prefers `.venv\Scripts\python.exe`, prints the selected interpreter, and otherwise uses
`python` on PATH. It preserves a failure's exit code and pauses so double-clicked errors can be read.
Set `$env:FOCUSFLOW_NO_PAUSE='1'` to suppress that pause in automation. `--show` overrides a hidden-window
startup preference for this launch without changing settings. `--selftest` checks dependencies and
application imports. `--diagnose` additionally exercises unique/duplicate mutex creation, a hidden Tk
root, read-only cursor APIs and UI Automation initialization. It does not install input hooks, move the
pointer, modify settings, or open the activity database; passing it alone does not verify the visible tray
or working pointer assistance.

The log records interpreter path/version, process ID, architecture, package versions, mutex outcome,
Tk initialization, server startup, window construction, tray setup, hook registration and mainloop entry.
A duplicate application mutex now reports an explicit message and exit code 2. Mutex creation failures
raise the real Windows error instead of being accepted as a first instance. The mutex remains held for
the application's lifetime and is closed on exit. Pointer assistance starts only after the UI, tray,
emergency hotkey and mouse hook initialize; startup failure cleans up partially started services.

For Python 3.15, requirements select **Pillow >=13**, the first version listed with 3.15 support in
[Pillow's support table](https://pillow.readthedocs.io/en/stable/installation/python-support.html).
This does not certify the full desktop stack on Python 3.15. Actual comtypes/pystray/native API checks
run against the installed interpreter. To compare with Python 3.11 without replacing the 3.15 environment,
install Python 3.11 with Tcl/Tk support, then use a separate environment explicitly:

```powershell
py -3.11 -m venv .venv311
.\.venv311\Scripts\python.exe -m pip install --upgrade pip
.\.venv311\Scripts\python.exe -m pip install -r requirements.txt
.\.venv311\Scripts\python.exe -m focusflow --diagnose
.\.venv311\Scripts\python.exe -m focusflow --show
```

Windows verification commands (close the running application before the desktop tests):

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_startup.py -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_ui_smoke.py -v
# The following opt-in tests move the real pointer; keep your hand off the mouse.
$env:FOCUSFLOW_LIVE_TESTS='1'
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_hook.py -v
Remove-Item Env:FOCUSFLOW_LIVE_TESTS
```

The UI smoke test builds every page and exercises the native tray. The live tests check pointer
stabilization and caption actions. Also visually check the main window/tray, Pause/Resume, and
Ctrl+Alt+Shift+F emergency stop with your hardware. **The current startup changes have been tested with
portable failure-injection tests on macOS, not a real Windows session or Python 3.15.** Windows-only
tests are explicitly skipped elsewhere; their presence is not evidence that they passed on Windows.

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
2. **Adaptive Profiles** — *Precision* (minimal), *Comfort* (balanced speed-adaptive smoothing),
   *Maximum Assistance*; save your own; per-application profiles that apply while that program is in front.
3. **Accessibility Tools** — large high-contrast cursor, pointer trails, target highlighting, dwell on-screen
   keyboard, voice commands, switch navigation. Each is independent.
4. **Live Lab** — raw vs stabilised pointer path from real mouse input, the gap between them, speed, direction
   changes, jitter events, detected target boundaries, dwell progress, active profile and strength. Also launches the
   measured experiment (below).
5. **Performance Analytics** — everyday pointer statistics, per-mode experiment comparison, session history,
   CSV / JSON / PDF export. A browser dashboard with the same data is at `http://127.0.0.1:47321/`.

## Target adaptation and smoother jitter reduction

In **Pointer Assist**, enable **Learn from nearby misses** and **Visibly enlarge repeatedly missed targets**.
The configured threshold defaults to **three nearby misses on the same target**, not one. A successful
manual hit resets that target's streak. Snap and dwell do not need to be enabled. **Undo adaptation** restores
normal targets and clears the streaks. In a browser, reload the FocusFlow extension and refresh the page
after updating these files; web buttons are enlarged using their actual DOM elements.

Native desktop controls use a larger, labelled, click-through **proxy over the original button** and the
existing expanded click area; FocusFlow does not rewrite other applications' layouts. Only a clearly
nearest, fresh target is eligible. Ambiguous targets, text-selection surfaces and stale geometry remain
unassisted. A UI Automation provider that does not expose a control cannot be adapted reliably.

The fix separates physical-click attribution from dwell hysteresis, refreshes stationary UI Automation
probes before the 400 ms click-cache expiry, and shows adapted controls while the cursor is still beside
them. Browser enlargement now retains its original transform, restores styles on disable/undo, and
honours the expanded hit area independently of the visual toggle.

Jitter smoothing estimates speed from **filtered signed velocity**, so repeated back-and-forth movement
no longer raises the cutoff as if it were deliberate travel. Strength settings, the 8 reference-pixel lag
cap, settling, emergency stop and optional ML modes remain available. In a deterministic 250 Hz synthetic
±3 px horizontal oscillation, default-strength peak-to-peak output/raw ratios changed from 0.802 to 0.653
at 8 Hz and from 0.791 to 0.513 at 12 Hz. These are synthetic filter measurements, not clinical results or
verification of Windows mouse responsiveness.

Regression checks: `python -m unittest discover -s tests -v` and `node --test tests/*.test.js`.
On Windows, also check three nearby misses with snap/dwell off, a subsequent click in the enlarged area,
Undo, and the emergency-stop hotkey. Native window rendering and physical pointer behaviour require a
Windows desktop; mocked engine and DOM tests do not establish that verification.

## How the pointer stabilisation works

PointerLab's `pointer.ts` was ported to `focusflow/pointer.py` (`AdaptivePointer`, `ErraticDetector`,
`MotionTracker`, `nearbyTarget`, `recordMiss`). The timing and distance constants are retained (3 Hz base
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
| Distance constants × display scale | The original is in CSS px; Windows reports physical px. Scale is read per monitor under the pointer (150 % → lag cap 12 px = the same visual 8 px). Scale 1 uses reference-pixel distances. |
| `cutoff_scale` (smoothing strength 0–10) | The original had one fixed curve. Strength 5 = baseline scale 1.0; each +3 halves the cut-off. The curve shape and the lag cap are untouched. |
| Signed velocity speed estimate | Low-pass each velocity component before taking magnitude, so alternating tremor does not weaken its own filtering. |
| Filter fed at ≤ 250 Hz | A browser delivers ~60–120 `pointermove`/s; a gaming mouse reports 1000+. Faster feeding changes the filter's speed estimate. |
| Raw position integrated from OS deltas | Browser events were absolute and un-swallowed; native moves are not. Resynchronises after any pause > 150 ms or a > 500 px jump (tablets, remote desktop, other apps moving the cursor). |
| `MotionTracker.corrective` | New counter: reversals ≤ 24 px, for the "corrective movements" metric. Direction-change rule unchanged. |
| `Attractor` | **New, not in the original.** A bounded pull toward one unambiguous target, applied as an *offset* so the lag bound is untouched; only while slow; never over text; drains the moment you move deliberately. |
| `nearbyTarget` radius | The original's 36 px / 8 px rule is kept for attribution of misses; the *redirect* radius is a separate, profile-controlled setting. |

The erratic-movement detector only ever **recommends** stronger smoothing, and is described everywhere as a heuristic,
not a medical assessment.

### Target adaptation (native version of "enlarge after 2 nearby misses")

FocusFlow cannot resize other programs' buttons, so adaptation enlarges the target's *hit area*: after 2 nearby,
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
node --test tests/*.test.js                  # 8 tests: the browser extension's JS logic
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
