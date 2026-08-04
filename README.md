# FrameByFrame-ASWAXS

> in your own analysis

Version 6 uses PySide6/Qt 6 exclusively for every first-party GUI. Matplotlib
uses `backend_qtagg`, and the application selects `QT_API=pyside6` before
loading Matplotlib, pyqtgraph, or pyFAI GUI code. PyQt5 and PyQt6 must not be
loaded into the FrameByFrame process.

FrameByFrame-ASWAXS is a GUI-first processing platform for turning raw
SAXS/WAXS/ASAXS HDF5 detector frames into analysis-ready I(q) curves,
provenance-rich analysis HDF5 files, XAnoS-compatible text exports, and
quality-control plots.

The goal is to make beamline reduction feel like guided scientific software,
not a folder-and-script exercise. A user can choose raw HDF5 files, define the
sequence, assign detector calibration and monitor/PV normalization, run a task
queue, inspect the final I(q), evaluate frame stability, export XAnoS-format
data, and send ASAXS results into downstream component extraction.

## Platform Story

The platform is designed around the full post-acquisition workflow:

1. Select raw HDF5 files or a raw measurement folder.
2. Choose SAXS-only or ASAXS/XAnoS reduction.
3. Confirm the sequence: energies, groups, and frames.
4. Select PONI, mask, monitor/PV normalization, and thickness metadata.
5. Define SAXS output names or ASAXS sample/solvent pairs.
6. Run one task or a queue with group-based multicore reduction, smoothed ETA,
   and detector-level frame progress.
7. Review final curves and the QC reports saved during averaging.
8. Export XAnoS-compatible `.dat` files or open downstream XAnoS tools.

This makes FrameByFrame-ASWAXS useful both for beamtime batch reduction and for
post-processing already-collected datasets.

## Design Principles

- **Raw data are read-only.** Raw HDF5 files are treated as immutable
  experimental records. All derived data are written to `Extracted`/output
  folders, analysis HDF5 files, and XAnoS-format exports.
- **GUI-first workflow.** The main path is the guided Qt dashboard, task
  builder, queue, plotter, and tools menu. Command-line scripts remain available
  for testing and compatibility.
- **Energy-aware ASAXS.** Each energy row have its own q grid; viewer,
  stitching, and export code use the matching q row.
- **Traceable derived outputs.** The analysis HDF5 stores reduced curves,
  propagated uncertainty, detector geometry, source-file history, averaging-time
  QC reports, and full-task timing records.
- **Fresh reduction by default.** Restart behavior is task-scoped and avoids
  deleting sibling task results in shared output folders.
- **Downstream compatibility.** XAnoS-format `.dat` files are written for ASAXS
  and SAXS-only reductions. ASAXS keeps energy-indexed filenames; SAXS-only uses
  clean sample/output names.

## What FrameByFrame-ASWAXS Provides

- Guided task builder with clickable steps.
- Queue table for adding, editing, deleting, reordering, and running tasks.
- Single-detector and dual-detector support for Pil300K and Eig1M.
- SAXS-only and ASAXS reduction modes.
- Dynamic multicore reduction by `(energy, group)`, with PONI and mask loaded
  once per worker.
- Per-task detector progress, smoothed frame-rate ETA, and recorded total run
  time.
- Detector stitching for SAXS/WAXS data, including q-gap scaling without adding
  artificial points.
- HDF5 I(q) viewer for SAXS, WAXS, combined, final, and unstitched curves.
- Publication-style Matplotlib plotting with interactive zoom and coordinates.
- Viewer-side background subtraction and sample/background pair outputs.
- Frame-stability QC calculated from already-reduced frames before each group
  average and saved in the analysis HDF5.
- HDF5 structure/metadata viewer.
- pyFAI setup GUI launcher for PONI/mask work.
- XAnoS Components integration for completed ASAXS tasks.
- External-tool linkers for XAnoS, XModFit, and sample-position planning without
  permanently modifying those upstream tools.

## Project Layout

```text
ASWAXS_reduction_pipeline_v6/
  run_gui.py                  root launcher; run without entering scripts/
  pyproject.toml              installable application definition
  docs/                       user and architecture documentation
  scripts/                    compatibility and utility launchers
  src/aswaxs_live/
    app/                      dashboard, launcher, theme, and Qt support
    reduction/                integration, QC, stitching, and analysis output
    workflows/                task queue and beamline message orchestration
    tools/                    independent GUIs and external-app linkers
  outputs/                    ignored local analysis output
```

The package ownership rules and single-owner policy are described in the
[architecture guide](docs/architecture.md). Each feature has one authoritative
module path; launchers and subprocesses connect to those packages directly.

## Start the Application

Version 6 is standardized on Python 3.10 and uses the existing `pydm-env`
environment for FrameByFrame, XAnoS, and XModFit. Install the unified
requirements from the project root:

```powershell
conda activate pydm-env
python -m pip install -r requirements.txt
python -m pip install -e .
python run_gui.py
```

The shared environment contains both PySide6 and PyQt5, but they are never
loaded in the same process. FrameByFrame selects PySide6; XAnoS and XModFit are
started as separate subprocesses with `QT_API=pyqt5`. Because all three use the
same `python.exe`, no interpreter environment variables are needed in this
configuration.

For a copied checkout that already has its dependencies installed:

```powershell
python run_gui.py
```

For a permanent command, install the checkout once from its root:

```powershell
python -m pip install -e .
```

After that, start the GUI from any directory with:

```text
framebyframe-aswaxs
```

Shorter compatibility aliases are also available:

```text
framebyframe
```

The older ASWAXS command remains available too:

```text
aswaxs
```

The older `python scripts/run_gui.py` route remains available for compatibility.

XAnoS and XModFit remain separate sibling source installations. They are always
launched in isolated subprocesses, so their Qt5 imports cannot mix with the
FrameByFrame Qt6 process. With the shared Python 3.10 `pydm-env` environment,
the linkers use the current `sys.executable` automatically. The per-tool
`FRAMEBYFRAME_XANOS_PYTHON` and `FRAMEBYFRAME_XMODFIT_PYTHON` overrides remain
available for unusual installations. Exported data paths are forwarded to the
isolated hosts for automatic loading.

The main dashboard `Tools` menu opens the HDF5 I-q viewer, HDF5 structure and
metadata viewer, and the pyFAI PONI/mask setup GUI. These tools no longer require
opening their individual scripts.

User-facing tools are organized under `src/aswaxs_live/tools`, with one
subfolder per application. The Tools menu also opens the integrated **Online
1-D Reducer**, which receives the beamline `SUB`/JSON/`image_path` contract,
uses the FrameByFrame live reducer for read-only azimuthal integration, and
updates detector and optionally stitched SAXS/WAXS analysis HDF5 outputs. It
does not perform ASAXS correction or component extraction. It can also be launched
standalone with:

```bash
framebyframe-online
```

or from a source checkout:

```bash
python scripts/run_online_reducer.py
```

### SAXS Frame Stability QC

Each raw detector image is integrated only once during a current batch
reduction. When an `(energy, group)` contains more than one frame, stability QC
runs from the normalized frame-resolved `I(q)` and propagated `sigma(q)` already
in memory immediately before averaging. The report is stored under the detector
reduction process at:

```text
/entry/process_01_reduction/frame_stability_qc
```

Groups containing one frame are marked `not_applicable_single_frame`; the
software does not run meaningless frame-stability statistics for them.

The HDF5 I-q viewer has a separate `Frame Stability QC` tab. When the stored
`qc_complete` marker is present, selecting a detector/energy/group loads the
saved report directly without reopening raw HDF5 files or repeating pyFAI
integration. Previous/next arrows and `Alt+Left`/`Alt+Right` browse continuously
through the available QC series. Reports include:

- frame overlays and an I_i(q)/I_1(q) heatmap;
- invariant-like and low-q intensity ratios;
- reduced chi-square and CorMap-style longest-run probability;
- optional Guinier Rg/I(0) and peak-position/FWHM trends;
- Good, Acceptable, and Bad labels plus a conservative initial stable-frame
  averaging recommendation.

The QC recommendation is advisory. It does not modify raw HDF5, replace the
stored group average, or alter downstream ASAXS results. For an older analysis
HDF5 without saved QC, the viewer retains a read-only legacy fallback that
re-integrates the selected provenance frames.

The scientific basis, metric interpretation, limitations, and complete paper
citations are included in the [Frame Stability QC guide](docs/frame_stability_qc.md),
which is also available from the GUI `Help` menu.

## Create PONI and Mask Files

This project includes the HDF5-to-pyFAI setup GUI, so V6 can create calibration
files as well as run live reduction.

```powershell
python .\ASWAXS_reduction_pipeline_v6\scripts\run_pyfai_gui.py
```

Or start with an HDF5 file already loaded:

```powershell
python .\ASWAXS_reduction_pipeline_v6\scripts\run_pyfai_gui.py `
  --file "path\to\AgBH_or_sample.h5"
```

The GUI workflow is: load HDF5 image, export an EDF bridge, launch
`pyFAI-calib2`, save the `.poni`, launch `pyFAI-drawmask`, then import/save the
mask. The live reducer uses those files through the `--poni` and `--mask`
parameters.

## Run a Small Real-Data Smoke Test

From `C:\Users\jiajtian\Documents\Playground`:

```powershell
python .\ASWAXS_reduction_pipeline_v6\scripts\run_reducer.py `
  --manifest .\ASWAXS_reduction_pipeline\outputs\FC_AuSiO2NP_60uL_min_Eig1M_reduced_no_fluorescence\sequence_manifest.csv `
  --poni "Y:\aswaxs\bera\Apr2026\Commissioning\FC_AgBH_11_919keV\Eig1M\calib.poni" `
  --mask "Y:\aswaxs\bera\Apr2026\Commissioning\FC_AgBH_11_919keV\Eig1M\mask.msk" `
  --output-dir .\ASWAXS_reduction_pipeline_v6\outputs\real_data_smoke `
  --sample-name FC_AuSiO2NP_ASWAXS_60uL_min `
  --gc-group 1 --air-group 2 --empty-group 3 --water-group 4 --sample-group 5 `
  --analysis-mode asaxs `
  --limit-energies 1 `
  --limit-frames-per-group 2
```

The output directory contains:

- `live_events.jsonl`: ordered stage-trigger log.
- `<sample_name>_analysis.h5`: analysis/provenance HDF5 written by the current pipeline helpers.
- `group_summary.csv`: group-average summary table.

Current batch analysis HDF5 files also contain averaging-time frame-stability
QC. A GUI-run task records its latest complete wall time under
`/entry/reduction_timing`, including preparation, detector reduction, QC,
averaging, finalization, stitching, and export.

The V6 default is HDF5-only for reduced curves. Legacy `.dat` curve files are
written only when `--write-text-output` is enabled.
This smoke command intentionally replays only 2 frames from each group. Remove
`--limit-energies` and `--limit-frames-per-group` when you want every collected
frame written into the live single-frame table.

Each single-frame 1D reduction is appended immediately to
`/entry/realtime/process_01_reduction/frames` inside the same analysis HDF5 file.
Group-average and ASAXS result groups are appended later as their trigger
conditions are met.

The live frame table includes a `qc_status` dataset:

- `pending_group_qc`: this frame has been reduced to 1D, but its full
  `(energy, group)` has not reached the group-average trigger yet.
- `accepted`: this frame was kept when the group average was calculated.
- `rejected_total_intensity`: this frame was dropped from the group average by
  the total-intensity outlier filter.

## Watch a Live Acquisition Folder

For real acquisition, run this script in a second terminal while Bluesky writes
raw HDF5 files into the sample folder. The watcher assigns files by arrival order:

```text
energy 1, group 1, frame 1
energy 1, group 1, frame 2
...
energy 1, group 2, frame 1
...
energy 2, group 1, frame 1
```

Example:

```powershell
python .\ASWAXS_reduction_pipeline_v6\scripts\run_reducer.py `
  --watch-dir "\\chemmat-c51\data_rw\aswaxs\bera\Apr2026\Commissioning\FC_AuSiO2NP_ASWAXS_60uL_min\Eig1M" `
  --pattern "*.h5" `
  --num-energies 15 `
  --num-groups 5 `
  --num-frames 100 `
  --poni ".\FC_ASWAXS\FC_AgBH_11_919keV\Eig1M\calib.poni" `
  --mask ".\FC_ASWAXS\FC_AgBH_11_919keV\Eig1M\mask.msk" `
  --output-dir .\ASWAXS_reduction_pipeline_v6\outputs\live_run `
  --sample-name FC_AuSiO2NP_ASWAXS_60uL_min `
  --analysis-mode asaxs `
  --gc-group 1 --air-group 2 --empty-group 3 --water-group 4 --sample-group 5
```

The watcher waits for file size to stop changing, opens the HDF5 file read-only,
checks that `entry/data/data` exists, then starts the reduction trigger chain.
The detector type is normally inferred from the acquisition file and folder
name. Use `--detector Eig1M` or `--detector Pil300K` only as a manual override
for unusual files where auto-detection is ambiguous.
When `--once` is used, the watcher performs a single pass over files already in
the folder and does not use poll or settle timing. In the GUI, checking
`Watcher once` disables `Poll seconds` and `Settle seconds`.

Existing-output behavior has two modes:

- `resume` is the default. If the reducer is stopped and started again with the
  same output directory/sample analysis HDF5, it reads the existing live frame
  table, skips already reduced source files, advances to the next sequence
  position, and rebuilds any unfinished group from the saved frame curves.
- `restart` starts from scratch in the same location. It removes the existing
  analysis HDF5 before writing new results and replaces `live_events.jsonl`
  from the first new log line. Use `--restart` from the command line, or choose
  `restart` in Window 0.

For normal SAXS-only reduction, use `--analysis-mode saxs`. In that mode the
role options such as `--gc-group` and `--sample-group` are not required unless
you want them recorded in metadata.

V5 does not automatically use relaxed recursive watching for SAXS mode. In queue
mode, `data_dir` should be the detector folder for the completed measurement.
Use `--recursive-watch` only for a special test case where the queued directory
intentionally contains nested raw HDF5 files.

## Beamtime Folder Layout

V5 assumes the GUI root folder is the beamtime date folder, for example:

```text
Tianbo/
  2026Jun/
    Sample_A/
      Pil300K/
      Eig1M/
    Sample_B/
      Pil300K/
      Eig1M/
    Extracted/
      Sample_A/
        Pil300K/
        Eig1M/
        Sample_A_analysis.h5
      Sample_B/
        Pil300K/
        Eig1M/
        Sample_B_analysis.h5
```

In the GUI, set `Beamtime date folder` to `Tianbo/2026Jun`. Raw detector folders
are derived as `<root>/<sample>/<detector>`, and analysis folders are derived as
`<root>/Extracted/<sample>/<detector>`.

For offline reduction of one or more already-collected samples, enable
`use sample task list`, set `Task source` to `sample list`, and enter one
sample name per row in the `Sample list` table. A single sample is just a table
with one row. In this mode the old `Sample name` field is disabled and not used.
The reducer processes this table in row order. If the first sample's detector
folder does not exist yet, it waits on that sample instead of jumping to the
next row. When the sample list is finished, the GUI-launched reducer stops
instead of waiting forever like online Kafka mode.

## Bluesky/Kafka Measurement Done Queue

In v6, a Bluesky plan, callback, or Kafka bridge should append/publish a
lightweight `measurement_done` message when one measurement is complete. The
message is not treated as data. It becomes a reduction job and supplies beamline
context:

```json
{"event": "measurement_done", "uid": "...", "scan_id": 123, "sample_name": "sampleA", "detector": "Pil300K", "analysis_mode": "saxs", "measurement_type": "normal_saxs", "data_dir": "C:/path/to/sampleA/Pil300K"}
```

Start the reducer worker in queue mode:

```powershell
python .\ASWAXS_reduction_pipeline_v6\scripts\run_reducer.py `
  --measurement-done-queue .\ASWAXS_reduction_pipeline_v6\outputs\beamline\measurement_done_queue.jsonl `
  --pattern "*.h5" `
  --num-energies 1 `
  --num-groups 1 `
  --num-frames 1 `
  --poni ".\FC_ASWAXS\FC_AgBH_11_919keV\Pil300K\calib.poni" `
  --mask ".\FC_ASWAXS\FC_AgBH_11_919keV\Pil300K\mask.msk" `
  --output-dir .\ASWAXS_reduction_pipeline_v6\outputs\beamline `
  --sample-name sampleA `
  --analysis-mode saxs `
  --detector Pil300K
```

For a local test, append one reduction job manually:

```powershell
python .\ASWAXS_reduction_pipeline_v6\scripts\write_measurement_done.py `
  --queue .\ASWAXS_reduction_pipeline_v6\outputs\beamline\measurement_done_queue.jsonl `
  --data-dir "C:\path\to\sampleA\Pil300K" `
  --uid "test-uid" `
  --scan-id 1 `
  --sample-name sampleA `
  --detector Pil300K `
  --analysis-mode saxs
```

To bridge saved Kafka-like messages into the reducer queue for testing:

```powershell
python .\ASWAXS_reduction_pipeline_v6\scripts\run_kafka_queue_bridge.py `
  --queue .\ASWAXS_reduction_pipeline_v6\outputs\beamline\measurement_done_queue.jsonl `
  --replay-jsonl .\messages_from_beamline.jsonl
```

For a real Bluesky Kafka stream, run the bridge in a beamline environment with
`bluesky-kafka` installed:

```powershell
python .\ASWAXS_reduction_pipeline_v6\scripts\run_kafka_queue_bridge.py `
  --queue .\ASWAXS_reduction_pipeline_v6\outputs\beamline\measurement_done_queue.jsonl `
  --bootstrap-servers "kafka-host:9092" `
  --topic "aswaxs.bluesky.documents"
```

The reducer logs the queue message as `measurement_done_received` in
`live_events.jsonl`, adds `data_dir` to its active job list, waits until matching
HDF5 files are readable, then assigns sequence order and reduces the measurement.
For dual-detector acquisition, keep the v2 pattern of one reducer process per
detector. Point both reducers at the same queue if convenient, but launch them
with `--detector Pil300K` and `--detector Eig1M`; each reducer ignores queue
messages for the other detector.
Detector aliases from beamline metadata are normalized before this comparison:
`SAXS`, `SPDS`, and `Pil300K` are treated as Pil300K jobs; `WAXS`, `WPDS`, and
`Eig1M` are treated as Eig1M jobs. In a dual-detector run, seeing one reducer
skip the other detector's queue message is normal; it should still process the
matching message for its detector.

The V6 GUI can launch this queue workflow too. In `Window 0`, enable
`use sample task list` to make the reducer tail an internal task file instead
of directly watching one folder. Enable `start Kafka bridge with reducer` when the
same GUI session should also run `scripts/run_kafka_queue_bridge.py`. Then fill
in the Kafka bootstrap servers, topic, and group ID. The internal queue file is
derived automatically from the beamtime root and current sample queue so old
saved GUI settings cannot point the reducer at a stale task list. If the bridge
runs on another beamline machine, leave `start Kafka bridge with reducer`
unchecked and run both tools against the same beamtime root.

## Show Live 1D Curves

Run the live curve viewer in another terminal and point it at the same reducer
output directory:

```powershell
python .\ASWAXS_reduction_pipeline_v6\scripts\run_viewer.py `
  --output-dir .\ASWAXS_reduction_pipeline_v6\outputs\live_run
```

The viewer reads the batch analysis HDF5 file by default. It first looks for
`*_analysis.h5` in the output folder, then falls back to `analysis.h5` for old
test runs. It also keeps `.dat` folder support for compatibility.
The plot source menu has the three views needed during acquisition:

- `h5 single frames`: every individual frame after 1D reduction.
- `h5 group averages`: one averaged curve per `(energy, group)`.
- `h5 final`: final ASAXS-reduced curves.

For `h5 single frames`, use the energy and group selectors instead of browsing a
long flat list. The raw-frame plot modes are:

- `latest`: show the newest frame in the selected energy/group.
- `single frame`: use the frame slider to inspect one frame.
- `last N`: overlay the newest N frames in the group.
- `all in group`: overlay all frames for that group with a compact status legend.
- `average + frames`: show all raw frames lightly with the group average bold.
- `heatmap`: show frame order versus q with intensity as color.

For group-average and final-curve views, click to plot one curve, Ctrl-click to
add or remove curves, and Shift-click to select a range. Large raw-frame overlays
use compact status legends instead of one legend entry per frame.
Auto-refresh updates the available curve list without disturbing a manually
selected plot. Enable `follow latest` when you want the plotted curve to move to
the newest HDF5 row as it appears. The stitched live tab enables this by default;
raw-frame `latest` mode also replots automatically.
In sample-list mode, the monitor progress bar estimates total frames as
`sample rows * energies * groups * frames`. If one sample has fewer files than
the requested group/frame count, the reducer will wait on that sample and the
remaining-time estimate will reflect the requested count.

## Main Task Queue GUI

FrameByFrame-ASWAXS combines task creation, queue control, task progress, final
curve preview, and HDF5 tools in one dashboard. After installation, start it
from any directory with:

```text
framebyframe-aswaxs
```

For an uninstalled copied checkout, run this from the project root:

```powershell
python run_gui.py
```

The GUI remembers task-builder values in `aswaxs_v6_builder_settings.json` at
the project root. That local settings file should remain outside published
application changes.

### Multicore Reduction

For dual-detector tasks, the requested CPU budget is divided between Pil300K
and Eig1M detector subprocesses. Inside each detector, the process pool schedules
one independent job per `(energy, group)`. For example, 20 energies and 6 groups
provide 120 schedulable jobs per detector instead of limiting parallelism to 20
energy batches. Workers keep their PONI and mask loaded while taking additional
group jobs.

Each worker reduces one complete group, runs applicable QC, calculates the
average, and then releases those frame curves. Large QC plot arrays are written
to worker-local temporary HDF5 shards instead of being transferred through the
multiprocessing result pipe. The detector parent merges the shards into the
analysis HDF5 and removes the temporary files.

Raw HDF5 files remain read-only. After manifest validation, the frame loop opens
each source HDF5 once to read its image, energy, and monitor value together; it
does not perform separate per-frame existence and file-size queries.

### Progress And Timing

The dashboard progress bar represents the current task, not the whole queue.
Pil300K and Eig1M frame totals are shown separately. Aggregate throughput and ETA
use a rolling 30-second rate with exponential smoothing, which reduces jumps
when group workers start or network HDF5 throughput changes.

At completion, the queue records the full task wall time in its JSON state and
shows it in the task tooltip and completion message. The same successful-run
duration and UTC completion timestamp are written to:

```text
/entry/reduction_timing
```

Resume mode validates the existing analysis HDF5 before reusing it. If HDF5
metadata is damaged, for example after a crashed writer or interrupted copy, the
reducer moves that file aside as `*_corrupt_YYYYMMDD_HHMMSS.h5` and continues
with a fresh analysis file at the normal path. The moved file is kept for later
inspection instead of being deleted.

In sample-list and online Kafka modes, choose the beamtime date folder as
`Beamtime date folder`, for example `Tianbo/2026Jun`. Do not choose an
individual sample folder. A `scan_dir_waiting` monitor message means the reducer
is waiting for a derived detector folder such as
`<root>/<sample>/Pil300K` or `<root>/<sample>/Eig1M` to appear.

For simultaneous detector acquisition, set `Detector jobs` to `Pil300K + Eig1M`.
The GUI launches two reducer processes in parallel:

- Pil300K watches the Pil300K folder and writes its live working files to
  `<root>/Extracted/<sample>/Pil300K`.
- Eig1M watches the Eig1M folder and writes its live working files to
  `<root>/Extracted/<sample>/Eig1M`.

The GUI coordinator keeps one public batch analysis HDF5 named
`<sample_name>_analysis.h5` under `<root>/Extracted/<sample>`. The legacy
`Output directory` and `Analysis HDF5` fields are hidden in sample-list and
online Kafka modes because those paths are derived from the beamtime folder.
That combined file is organized as:

```text
/entry
  /Pil300K              # copied Pil300K analysis record
  /Eig1M                # copied Eig1M analysis record
  /stitched_averages    # stitched detector group averages
```

The two reducer processes keep their own detector working HDF5 files so they do
not write to the same HDF5 at the same time. The GUI is the only writer for the
combined HDF5: it refreshes `/entry/Pil300K`, `/entry/Eig1M`, and
`/entry/stitched_averages` as new matching group averages appear.

## Current V6 Boundary

This project is the experimental beamline-server copy. The queue transport is a
simple JSONL file so it can be tested now without requiring Kafka or a specific
Bluesky deployment. The reducer-side contract is intentionally small: receive a
`measurement_done` job, scan `data_dir`, keep raw HDF5 read-only, and append all
analysis/provenance/history to the analysis HDF5 and `live_events.jsonl`.
