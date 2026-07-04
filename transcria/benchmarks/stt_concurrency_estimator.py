"""Estimateur de concurrence STT — projette le débit local depuis un bench_audio.

Lit les résultats d'une campagne `bench_audio.py` (un `bench_root` = dossier de sortie),
extrait la durée réelle de transcription par combo depuis les logs (`collect_measurements`),
puis projette combien de jobs on peut traiter en parallèle sur les GPUs disponibles à une
efficacité donnée (`estimate_local_concurrency`) et écrit un rapport md/csv (`write_estimates`).

Sert à dimensionner `workflow.scheduling` / le nombre de workers avant une mise en charge,
à partir de MESURES réelles plutôt que d'hypothèses. Voir docs/BENCHMARKING.md.
"""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import median

TRANSCRIBE_RE = re.compile(r"Transcription terminée \| step=transcribe, duree=([0-9.]+), segments=([0-9]+)")
AUDIO_DURATION_RE = re.compile(r"Audio analysé \| .*?\bduree=([0-9.]+)")


@dataclass(frozen=True)
class BenchMeasurement:
    result_path: Path
    combo_id: str
    audio_file: str
    run_name: str
    stt_backend: str
    effective_stt_backend: str
    chunking_mode: str
    source_workers: int
    unit_count: int
    unit_basis: str
    transcribe_s: float
    pipeline_s: float | None
    audio_duration_s: float | None
    segments: int


@dataclass(frozen=True)
class LocalEstimate:
    measurement: BenchMeasurement
    target_workers: int
    efficiency: float
    estimated_transcribe_s: float
    estimated_speedup: float
    estimated_pipeline_s: float | None
    confidence: str


def collect_measurements(bench_root: Path, *, include_failed: bool = False) -> list[BenchMeasurement]:
    """Collecte les mesures locales exploitables depuis les JSON/logs de bench.

    Les anciens JSON ne contiennent pas toujours les métriques B5 fines. Dans ce
    cas, l'estimateur utilise les segments comme proxy de tours et marque la
    confiance à la baisse dans `estimate_local_concurrency`.
    """
    measurements: list[BenchMeasurement] = []
    for result_path in sorted(bench_root.glob("*/*.json")):
        if result_path.name == "run_params.json":
            continue
        data = _load_json(result_path)
        if not data:
            continue
        if not include_failed and data.get("status") not in (None, "ok"):
            continue
        transcribe_s, log_segments, audio_duration_s = _read_log_measurements(result_path)
        if transcribe_s is None:
            continue

        metadata = data.get("transcription_metadata") or {}
        chunk_metrics = metadata.get("chunk_metrics") or {}
        srt = data.get("srt") or {}
        timings = data.get("timings") or {}
        segments = _as_int(metadata.get("segments")) or _as_int(srt.get("raw_segments")) or log_segments or 0
        unit_count = _as_int(chunk_metrics.get("chunks")) or segments
        if unit_count <= 0:
            continue
        source_workers = _as_int(chunk_metrics.get("workers")) or 1
        unit_basis = "chunk_metrics" if chunk_metrics.get("chunks") else "segments_proxy"
        measurements.append(
            BenchMeasurement(
                result_path=result_path,
                combo_id=str(data.get("combo_id") or result_path.stem),
                audio_file=str(data.get("audio_file") or ""),
                run_name=result_path.parent.name,
                stt_backend=str(data.get("stt_backend") or ""),
                effective_stt_backend=str(data.get("effective_stt_backend") or metadata.get("backend") or ""),
                chunking_mode=str(metadata.get("chunking_mode") or ""),
                source_workers=max(1, source_workers),
                unit_count=unit_count,
                unit_basis=unit_basis,
                transcribe_s=transcribe_s,
                pipeline_s=_as_float(timings.get("pipeline_s")),
                audio_duration_s=audio_duration_s,
                segments=segments,
            )
        )
    return measurements


def estimate_local_concurrency(
    measurements: list[BenchMeasurement],
    *,
    target_workers: list[int],
    efficiency: float = 0.75,
) -> list[LocalEstimate]:
    """Estime l'effet d'une concurrence STT distante sur cette machine.

    Hypothèse volontairement simple et explicite : le gain marginal par worker
    supplémentaire vaut `efficiency` d'un worker idéal, borné par le nombre
    d'unités indépendantes (tours mesurés ou proxy segments). Les résultats sont
    des estimations locales, pas des mesures de serveur distant.
    """
    if not 0 < efficiency <= 1:
        raise ValueError("efficiency doit être dans ]0, 1]")

    estimates: list[LocalEstimate] = []
    for measurement in measurements:
        source_speedup = _speedup(measurement.source_workers, measurement.unit_count, efficiency)
        sequential_baseline_s = measurement.transcribe_s * source_speedup
        for workers in target_workers:
            if workers < 1:
                continue
            estimated_speedup = _speedup(workers, measurement.unit_count, efficiency)
            estimated_transcribe_s = sequential_baseline_s / estimated_speedup
            estimated_pipeline_s = None
            if measurement.pipeline_s is not None:
                non_stt_s = max(0.0, measurement.pipeline_s - measurement.transcribe_s)
                estimated_pipeline_s = non_stt_s + estimated_transcribe_s
            estimates.append(
                LocalEstimate(
                    measurement=measurement,
                    target_workers=workers,
                    efficiency=efficiency,
                    estimated_transcribe_s=round(estimated_transcribe_s, 3),
                    # `estimated_speedup` est déjà calculé (l. ci-dessus, _speedup ≥ 1.0) :
                    # le réutiliser au lieu de `baseline / estimated_transcribe_s` évite un
                    # 0/0 quand transcribe_s == 0 (audio dégénéré → baseline et transcribe
                    # estimé nuls). Valeur identique pour baseline > 0.
                    estimated_speedup=round(estimated_speedup, 3),
                    estimated_pipeline_s=round(estimated_pipeline_s, 3) if estimated_pipeline_s is not None else None,
                    confidence="medium" if measurement.unit_basis == "chunk_metrics" else "low",
                )
            )
    return estimates


def write_estimates(estimates: list[LocalEstimate], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "local_b5_estimates.csv"
    md_path = output_dir / "local_b5_estimates.md"
    fields = [
        "scope",
        "source",
        "confidence",
        "run_name",
        "combo_id",
        "audio_file",
        "stt_backend",
        "effective_stt_backend",
        "chunking_mode",
        "unit_basis",
        "unit_count",
        "source_workers",
        "target_workers",
        "efficiency",
        "measured_transcribe_s",
        "estimated_transcribe_s",
        "estimated_speedup",
        "measured_pipeline_s",
        "estimated_pipeline_s",
        "audio_duration_s",
        "segments",
        "result_path",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for estimate in estimates:
            writer.writerow(_estimate_row(estimate))

    lines = [
        "# Estimations B5 locales",
        "",
        f"- Généré : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "- Portée : machine_locale",
        "- Source : estimation depuis les logs/JSON de bench locaux",
        "- Attention : ces chiffres ne sont pas des mesures de serveur GPU distant.",
        "",
        "## Synthèse",
        "",
        "| STT | workers | runs | transcribe mesuré médian | transcribe estimé médian | speedup médian | confiance dominante |",
        "|-----|---------|------|--------------------------|--------------------------|----------------|---------------------|",
    ]
    for row in _aggregate_rows(estimates):
        lines.append(
            f"| {row['backend']} | {row['workers']} | {row['runs']} "
            f"| {row['measured_median_s']:.2f}s | {row['estimated_median_s']:.2f}s "
            f"| x{row['speedup_median']:.2f} | {row['confidence']} |"
        )
    lines += [
        "",
        "## Détail",
        "",
        "| run | combo | STT | unités | base | workers | transcribe mesuré | transcribe estimé | speedup | confiance |",
        "|-----|-------|-----|--------|------|---------|-------------------|-------------------|---------|-----------|",
    ]
    for estimate in estimates:
        m = estimate.measurement
        lines.append(
            f"| {m.run_name} | {m.combo_id} | {m.effective_stt_backend or m.stt_backend} "
            f"| {m.unit_count} | {m.unit_basis} | {estimate.target_workers} "
            f"| {m.transcribe_s:.2f}s | {estimate.estimated_transcribe_s:.2f}s "
            f"| x{estimate.estimated_speedup:.2f} | {estimate.confidence} |"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return csv_path, md_path


def _aggregate_rows(estimates: list[LocalEstimate]) -> list[dict]:
    grouped: dict[tuple[str, int], list[LocalEstimate]] = {}
    for estimate in estimates:
        backend = estimate.measurement.effective_stt_backend or estimate.measurement.stt_backend or "unknown"
        grouped.setdefault((backend, estimate.target_workers), []).append(estimate)

    rows = []
    for (backend, workers), group in sorted(grouped.items()):
        confidences = [e.confidence for e in group]
        rows.append(
            {
                "backend": backend,
                "workers": workers,
                "runs": len(group),
                "measured_median_s": median(e.measurement.transcribe_s for e in group),
                "estimated_median_s": median(e.estimated_transcribe_s for e in group),
                "speedup_median": median(e.estimated_speedup for e in group),
                "confidence": "medium" if confidences.count("medium") >= confidences.count("low") else "low",
            }
        )
    return rows


def _estimate_row(estimate: LocalEstimate) -> dict:
    m = estimate.measurement
    return {
        "scope": "machine_locale",
        "source": "estimation",
        "confidence": estimate.confidence,
        "run_name": m.run_name,
        "combo_id": m.combo_id,
        "audio_file": m.audio_file,
        "stt_backend": m.stt_backend,
        "effective_stt_backend": m.effective_stt_backend,
        "chunking_mode": m.chunking_mode,
        "unit_basis": m.unit_basis,
        "unit_count": m.unit_count,
        "source_workers": m.source_workers,
        "target_workers": estimate.target_workers,
        "efficiency": estimate.efficiency,
        "measured_transcribe_s": m.transcribe_s,
        "estimated_transcribe_s": estimate.estimated_transcribe_s,
        "estimated_speedup": estimate.estimated_speedup,
        "measured_pipeline_s": m.pipeline_s,
        "estimated_pipeline_s": estimate.estimated_pipeline_s,
        "audio_duration_s": m.audio_duration_s,
        "segments": m.segments,
        "result_path": str(m.result_path),
    }


def _speedup(workers: int, unit_count: int, efficiency: float) -> float:
    effective_workers = max(1, min(workers, max(1, unit_count)))
    return 1.0 + (effective_workers - 1) * efficiency


def _read_log_measurements(result_path: Path) -> tuple[float | None, int | None, float | None]:
    log_path = result_path.with_suffix(".log")
    if not log_path.exists():
        return None, None, None
    text = log_path.read_text(encoding="utf-8", errors="replace")
    transcribe_match = TRANSCRIBE_RE.search(text)
    if not transcribe_match:
        return None, None, _first_float(AUDIO_DURATION_RE, text)
    return float(transcribe_match.group(1)), int(transcribe_match.group(2)), _first_float(AUDIO_DURATION_RE, text)


def _first_float(pattern: re.Pattern[str], text: str) -> float | None:
    match = pattern.search(text)
    return float(match.group(1)) if match else None


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
