# FrameByFrame-ASWAXS Architecture

FrameByFrame-ASWAXS has four top-level ownership areas.

## App

`aswaxs_live.app` owns the main dashboard, installed launcher, shared theme, and
Qt runtime configuration. It coordinates workflows but does not implement
scientific reduction.

`aswaxs_live.app.qt` is the sole first-party Qt binding boundary. It selects
PySide6 before GUI-adjacent imports and rejects a process that already loaded
PyQt5 or PyQt6. Qt5-only external applications are hosted by subprocess bridge
modules under `tools.linkers`.

## Reduction

`aswaxs_live.reduction` owns pyFAI integration, sequence mapping, frame
averaging, frame-stability QC, SAXS/WAXS stitching, analysis-HDF5 persistence,
and XAnoS-format export.

Raw acquisition HDF5 files are always opened read-only. Derived curves,
provenance, geometry, QC, timing, and checkpoints are written only to analysis
HDF5 or export files. Each ASAXS energy uses its matching q row.

## Workflows

`aswaxs_live.workflows` owns task records, queue execution, progress and process
control, and Bluesky/Kafka message handling. Workflows decide when scientific
operations run; the reduction package performs those operations.

## Tools

`aswaxs_live.tools` owns independently usable GUIs: the combined I-q/metadata
viewer, online reducer, pyFAI setup, and rack builder. `tools.linkers` contains
only adapters for separately installed XAnoS, XModFit, and sample-position
applications. Their upstream source code is not part of FrameByFrame.

## Data Flow

```text
app -> workflows -> reduction
 |                      |
 +-------- tools -------+

raw HDF5 (read-only) -> reduction/QC -> analysis HDF5 -> export/linkers
```

Each feature has one authoritative module path. Obsolete aliases and wrapper-
only packages are not retained.
