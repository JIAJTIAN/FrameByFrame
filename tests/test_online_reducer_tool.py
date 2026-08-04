from pathlib import Path

import numpy as np
import h5py
from pyFAI.integrator.azimuthal import AzimuthalIntegrator

from aswaxs_live.tools.online_reducer.config import OnlineConfig
from aswaxs_live.tools.online_reducer.engine import ensure_v6_importable
from aswaxs_live.tools.online_reducer.checkpoints import (
    read_checkpoint_uid,
    write_experiment_metadata,
    write_stage_checkpoint,
)
from aswaxs_live.tools.online_reducer.identity import resolve_experiment_identity
from aswaxs_live.tools.online_reducer.session import OnlineCurveStore
from aswaxs_live.tools.online_reducer.zmq_receiver import parse_image_message


def test_online_reducer_uses_beamline_image_path_contract() -> None:
    path, payload = parse_image_message(b'{"image_path":"Y:\\\\run\\\\Pil300K\\\\frame_001.h5"}')

    assert path.name == "frame_001.h5"
    assert payload["image_path"].endswith("frame_001.h5")


def test_online_reducer_rejects_missing_image_path() -> None:
    try:
        parse_image_message('{"other": 1}')
    except ValueError as exc:
        assert "image_path" in str(exc)
    else:
        raise AssertionError("missing image_path was accepted")


def test_online_reducer_config_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    OnlineConfig(sample_name="demo", num_frames=12).save(path)

    actual = OnlineConfig.load(path)

    assert actual.sample_name == "demo"
    assert actual.num_frames == 12
    assert not hasattr(actual, "analysis_mode")
    assert not hasattr(actual, "output_dir")


def test_online_reducer_uses_embedded_v6_engine() -> None:
    src = ensure_v6_importable()

    assert (src / "aswaxs_live" / "reduction" / "live.py").is_file()
    from aswaxs_live.reduction.live import LivePipelineState, SequenceAssigner

    assert LivePipelineState is not None
    assert SequenceAssigner(2, 3, 1).next_position().frame_index == 1


def test_online_reducer_gui_does_not_embed_legacy_viewer() -> None:
    import inspect

    from aswaxs_live.tools.online_reducer.app import MainWindow

    build_ui_source = inspect.getsource(MainWindow._build_ui)

    assert "Open V6 curve viewer" not in build_ui_source
    assert "viewer_button" not in build_ui_source


def _curve_payload(frame: int, *, detector: str = "Pil300K", group: int = 3) -> dict[str, object]:
    q = np.linspace(0.01, 0.2, 20)
    return {
        "detector": detector,
        "sequence_index": frame,
        "energy_index": 1,
        "group_index": group,
        "frame_index": frame,
        "energy_kev": 12.0,
        "monitor_value": 100.0,
        "source_path": f"frame_{frame:03d}.h5",
        "q": q,
        "intensity": 1.0 / q + frame * 0.001,
        "sigma": np.full(q.size, 0.01),
    }


def test_online_curve_store_builds_qc_series_without_raw_file_reads() -> None:
    store = OnlineCurveStore()
    first = store.add_payload(_curve_payload(1))
    second = store.add_payload(_curve_payload(2))

    label, series = store.frame_series([0, 1])

    assert first.q is second.q
    assert "Pil300K" in label
    assert series.intensity.shape == (2, 20)
    assert series.source_path == ["frame_001.h5", "frame_002.h5"]


def test_online_curve_store_rejects_mixed_qc_series() -> None:
    store = OnlineCurveStore()
    store.add_payload(_curve_payload(1))
    store.add_payload(_curve_payload(2, detector="Eig1M"))

    try:
        store.frame_series([0, 1])
    except ValueError as exc:
        assert "same experiment, detector" in str(exc)
    else:
        raise AssertionError("mixed detector curves were accepted for frame QC")


def test_online_export_copies_only_h5_and_preserves_detector_structure(tmp_path: Path) -> None:
    from aswaxs_live.tools.online_reducer.app import export_temporary_analysis_h5

    session = tmp_path / "session"
    (session / "Pil300K").mkdir(parents=True)
    (session / "Eig1M").mkdir(parents=True)
    (session / "Pil300K" / "sample_Pil300K_analysis.h5").write_bytes(b"pil")
    (session / "Eig1M" / "sample_Eig1M_analysis.h5").write_bytes(b"eig")
    (session / "live_events.jsonl").write_text("not exported", encoding="utf-8")

    exported = export_temporary_analysis_h5(session, tmp_path / "export", "sample name")

    assert (exported / "Pil300K" / "sample_Pil300K_analysis.h5").read_bytes() == b"pil"
    assert (exported / "Eig1M" / "sample_Eig1M_analysis.h5").read_bytes() == b"eig"
    assert not (exported / "live_events.jsonl").exists()


def test_online_export_uses_each_experiments_canonical_extracted_root(tmp_path: Path) -> None:
    from aswaxs_live.tools.online_reducer.app import export_experiments_to_canonical

    session = tmp_path / "session"
    source = session / "Run_A_uid-a" / "Pil300K" / "Run_A_Pil300K_analysis.h5"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"analysis")
    canonical = tmp_path / "beamtime" / "Extracted" / "Raw_Run_A"

    written = export_experiments_to_canonical(
        session,
        [{"storage_name": "Run_A_uid-a", "canonical_output_root": str(canonical)}],
    )

    target = canonical / "Pil300K" / source.name
    assert written == [target.resolve()]
    assert target.read_bytes() == b"analysis"


def test_experiment_identity_prefers_zmq_and_uses_local_extracted_path(tmp_path: Path) -> None:
    raw_root = tmp_path / "2026Jun" / "Raw_Experiment_Folder"
    image = raw_root / "Pil300K" / "frame_0001.h5"
    image.parent.mkdir(parents=True)
    with h5py.File(image, "w") as handle:
        handle.create_dataset("entry/data/data", data=np.ones((2, 2)))

    identity = resolve_experiment_identity(
        image,
        {"experiment_title": "Protein Run A", "experiment_uid": "exp-123456789", "run_uid": "run-10"},
        detector="Pil300K",
    )

    assert identity.title == "Protein Run A"
    assert identity.experiment_uid == "exp-123456789"
    assert identity.raw_experiment_root == raw_root.resolve()
    assert identity.canonical_output_root == (raw_root.parent / "Extracted" / raw_root.name).resolve()
    assert identity.storage_name.startswith("Protein_Run_A_exp-1234")


def test_experiment_identity_reads_hdf5_metadata_when_payload_has_only_path(tmp_path: Path) -> None:
    image = tmp_path / "Experiment_B" / "Eig1M" / "frame_0001.h5"
    image.parent.mkdir(parents=True)
    with h5py.File(image, "w") as handle:
        ndattrs = handle.create_group("entry/instrument/NDAttributes")
        ndattrs.create_dataset("Experiment_Title", data="HDF5 Experiment", dtype=h5py.string_dtype("utf-8"))
        ndattrs.create_dataset("Experiment_UID", data="h5-exp-1", dtype=h5py.string_dtype("utf-8"))

    identity = resolve_experiment_identity(image, {"image_path": str(image)}, detector="Eig1M")

    assert identity.title == "HDF5 Experiment"
    assert identity.experiment_uid == "h5-exp-1"
    assert identity.identity_source == "raw HDF5 metadata"


def test_checkpoint_registry_records_experiment_and_stage_dependencies(tmp_path: Path) -> None:
    image = tmp_path / "Experiment_C" / "Pil300K" / "frame.h5"
    image.parent.mkdir(parents=True)
    with h5py.File(image, "w") as handle:
        handle.create_group("entry")
    identity = resolve_experiment_identity(
        image,
        {"experiment_title": "Experiment C", "experiment_uid": "experiment-c"},
        detector="Pil300K",
    )
    analysis = tmp_path / "analysis.h5"
    with h5py.File(analysis, "w") as handle:
        handle.create_group("entry")

    write_experiment_metadata(analysis, identity)
    integration_uid = write_stage_checkpoint(
        analysis,
        "detector_integration",
        identity=identity,
        status="complete",
        output_group_path="/entry/realtime/process_01_reduction/frames",
        expected_items=100,
        written_items=100,
        parameters={"detector": "Pil300K", "npt": 1000},
    )
    write_stage_checkpoint(
        analysis,
        "group_averaging",
        identity=identity,
        status="partial",
        output_group_path="/entry/process_01_reduction/data",
        expected_items=5,
        written_items=2,
        parameters={"frames_per_group": 20},
        input_checkpoint_ids=[integration_uid],
    )

    assert read_checkpoint_uid(analysis, "detector_integration") == integration_uid
    with h5py.File(analysis, "r") as handle:
        assert handle["/entry/title"][()].decode() == "Experiment C"
        assert handle["/entry/experiment/analysis_uid"][()].decode() == identity.analysis_uid
        assert handle["/entry/checkpoints/detector_integration/status"][()].decode() == "complete"
        assert handle["/entry/checkpoints/group_averaging/written_items"][()] == 2
        dependencies = handle["/entry/checkpoints/group_averaging/input_checkpoint_ids_json"][()].decode()
        assert integration_uid in dependencies


def test_online_engine_routes_experiments_to_separate_h5_checkpoints(tmp_path: Path) -> None:
    from aswaxs_live.tools.online_reducer.engine import OnlineReductionEngine

    poni = tmp_path / "test.poni"
    mask = tmp_path / "mask.npy"
    AzimuthalIntegrator(
        dist=0.2,
        poni1=0.0016,
        poni2=0.0016,
        pixel1=0.0001,
        pixel2=0.0001,
    ).save(str(poni))
    np.save(mask, np.zeros((32, 32), dtype=np.uint8))
    config = OnlineConfig(
        pil300k_poni=str(poni),
        pil300k_mask=str(mask),
        eig1m_poni=str(poni),
        eig1m_mask=str(mask),
        num_frames=1,
        npt=50,
        settle_seconds=0,
    )
    engine = OnlineReductionEngine(config, tmp_path / "session")
    errors: list[str] = []
    engine.error.connect(errors.append)
    engine.initialize()

    for title, uid in (("Run A", "uid-a"), ("Run B", "uid-b")):
        image = tmp_path / title / "Pil300K" / "frame.h5"
        image.parent.mkdir(parents=True)
        with h5py.File(image, "w") as handle:
            handle.create_dataset(
                "entry/data/data",
                data=np.random.default_rng(1).poisson(10, (32, 32)).astype(np.float32),
            )
            ndattrs = handle.create_group("entry/instrument/NDAttributes")
            ndattrs.create_dataset("Mono_Energy", data=12.0)
            ndattrs.create_dataset("SPDS", data=100.0)
        engine.process_file(
            "Pil300K",
            str(image),
            {"image_path": str(image), "experiment_title": title, "experiment_uid": uid},
        )

    eig_image = tmp_path / "Run A" / "Eig1M" / "frame.h5"
    eig_image.parent.mkdir(parents=True)
    with h5py.File(eig_image, "w") as handle:
        handle.create_dataset(
            "entry/data/data",
            data=np.random.default_rng(2).poisson(10, (32, 32)).astype(np.float32),
        )
        ndattrs = handle.create_group("entry/instrument/NDAttributes")
        ndattrs.create_dataset("Mono_Energy", data=12.0)
        ndattrs.create_dataset("WPDS", data=100.0)
    engine.process_file(
        "Eig1M",
        str(eig_image),
        {"image_path": str(eig_image), "experiment_title": "Run A", "experiment_uid": "uid-a"},
    )

    runtime_paths = [runtime.state.analysis_path for runtime in engine._runtimes.values()]
    combined = next((tmp_path / "session").glob("*/Run_A_analysis.h5"))
    engine.shutdown()
    try:
        assert not errors
        assert len(runtime_paths) == 3
        for analysis_path in runtime_paths:
            with h5py.File(analysis_path, "r") as handle:
                assert handle["/entry/checkpoints/detector_integration/status"][()].decode() == "complete"
                assert handle["/entry/checkpoints/group_averaging/status"][()].decode() == "complete"
                assert handle["/entry/experiment/experiment_uid"][()].decode() in {"uid-a", "uid-b"}
        with h5py.File(combined, "r") as handle:
            assert handle["/entry/checkpoints/detector_stitching/status"][()].decode() == "complete"
            assert len(handle["/entry/stitched_averages/curves"]) == 1
        assert not list((tmp_path / "session").rglob("*.dat"))
    finally:
        if engine._runtimes:
            engine.shutdown()
