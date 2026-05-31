from __future__ import annotations

import json

from transcria.benchmarks.stt_concurrency_estimator import collect_measurements
from transcria.benchmarks.stt_concurrency_estimator import estimate_local_concurrency
from transcria.benchmarks.stt_concurrency_estimator import write_estimates


def _write_result(run_dir, combo_id="S01", *, chunk_metrics=None):
    payload = {
        "combo_id": combo_id,
        "audio_file": "test2.mp3",
        "stt_backend": "cohere",
        "effective_stt_backend": "cohere",
        "status": "ok",
        "timings": {"pipeline_s": 16.0},
        "srt": {"raw_segments": 29},
        "transcription_metadata": {
            "backend": "cohere",
            "chunking_mode": "pyannote_turns",
            "segments": 29,
            "chunk_metrics": chunk_metrics,
        },
    }
    (run_dir / f"{combo_id}.json").write_text(json.dumps(payload), encoding="utf-8")
    (run_dir / f"{combo_id}.log").write_text(
        "\n".join(
            [
                "2026-05-27 [INFO] - transcria.services.job_service:- Audio analysé | job_id=x, duree=73.0645, codec=mp3",
                "2026-05-27 [INFO] - transcria.services.pipeline_service:- Transcription terminée | step=transcribe, duree=13.9, segments=29",
            ]
        ),
        encoding="utf-8",
    )


def test_collect_measurements_uses_segments_proxy_for_legacy_results(tmp_path):
    run_dir = tmp_path / "test2_legacy"
    run_dir.mkdir()
    _write_result(run_dir, chunk_metrics=None)

    measurements = collect_measurements(tmp_path)

    assert len(measurements) == 1
    assert measurements[0].unit_basis == "segments_proxy"
    assert measurements[0].unit_count == 29
    assert measurements[0].transcribe_s == 13.9
    assert measurements[0].audio_duration_s == 73.0645


def test_estimate_local_concurrency_marks_legacy_confidence_low(tmp_path):
    run_dir = tmp_path / "test2_legacy"
    run_dir.mkdir()
    _write_result(run_dir, chunk_metrics=None)
    measurements = collect_measurements(tmp_path)

    estimates = estimate_local_concurrency(measurements, target_workers=[4], efficiency=0.75)

    assert len(estimates) == 1
    assert estimates[0].target_workers == 4
    assert estimates[0].confidence == "low"
    assert estimates[0].estimated_speedup == 3.25
    assert estimates[0].estimated_transcribe_s < measurements[0].transcribe_s


def test_collect_measurements_prefers_persisted_chunk_metrics(tmp_path):
    run_dir = tmp_path / "test2_current"
    run_dir.mkdir()
    _write_result(
        run_dir,
        chunk_metrics={
            "mode": "sequential",
            "workers": 1,
            "chunks": 12,
            "segments": 29,
            "elapsed_s": 13.9,
            "chunks_per_s": 0.86,
            "segments_per_s": 2.08,
        },
    )

    measurements = collect_measurements(tmp_path)
    estimates = estimate_local_concurrency(measurements, target_workers=[2], efficiency=0.75)

    assert measurements[0].unit_basis == "chunk_metrics"
    assert measurements[0].unit_count == 12
    assert estimates[0].confidence == "medium"


def test_write_estimates_marks_scope_and_source(tmp_path):
    run_dir = tmp_path / "test2_current"
    run_dir.mkdir()
    _write_result(run_dir)
    estimates = estimate_local_concurrency(collect_measurements(tmp_path), target_workers=[2])

    csv_path, md_path = write_estimates(estimates, tmp_path / "out")

    csv_text = csv_path.read_text(encoding="utf-8")
    md_text = md_path.read_text(encoding="utf-8")
    assert "machine_locale" in csv_text
    assert "estimation" in csv_text
    assert "## Synthèse" in md_text
    assert "ces chiffres ne sont pas des mesures de serveur GPU distant" in md_text
