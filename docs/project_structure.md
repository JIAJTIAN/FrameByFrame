# Project Structure

```text
ASWAXS_reduction_pipeline_v6/
  README.md
  pyproject.toml
  run_gui.py
  docs/
  scripts/
  src/aswaxs_live/
    app/
      dashboard.py
      launcher.py
      qt_runtime.py
      theme.py
    reduction/
      analysis_h5.py
      aswaxs_sequence.py
      frame_qc.py
      live.py
      pipeline.py
      sequence.py
      stitching.py
      xanos_export.py
    workflows/
      bluesky_queue.py
      kafka_bridge.py
      queue.py
      task.py
    tools/
      iq_viewer/
      linkers/
      online_reducer/
      pyfai_setup/
      rack_builder/
```

Scripts and installed commands are launchers only. Scientific calculations
live under `reduction`; scheduling and process control live under `workflows`.

