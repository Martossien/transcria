import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace


def _load_script(name: str):
    path = Path(__file__).resolve().parents[1] / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_compare_stt_segments_builds_time_aligned_recommendations(tmp_path):
    module = _load_script("compare_stt_segments.py")
    left = tmp_path / "left.json"
    right = tmp_path / "right.json"
    left.write_text(
        """
        [
          {"start": 0.0, "end": 10.0, "text": "bloc long avec erreur", "reliability": "suspect", "no_speech_prob": 0.7},
          {"start": 10.0, "end": 20.0, "text": "texte stable", "reliability": "ok", "no_speech_prob": 0.1}
        ]
        """,
        encoding="utf-8",
    )
    right.write_text(
        """
        [
          {"start": 0.0, "end": 5.0, "text": "bloc propre", "reliability": "ok", "no_speech_prob": 0.01},
          {"start": 5.0, "end": 10.0, "text": "avec terme critique", "reliability": "ok", "no_speech_prob": 0.01},
          {"start": 10.0, "end": 20.0, "text": "texte stable", "reliability": "ok", "no_speech_prob": 0.1}
        ]
        """,
        encoding="utf-8",
    )

    report = module.build_comparison(
        module.Side("cohere", "left", left),
        module.Side("whisper", "right", right),
        ["terme critique"],
        0.25,
    )

    assert report["interval_count"] == 3
    assert report["recommendation_counts"]["whisper"] >= 1
    assert report["rows"][1]["whisper"]["term_hits"] == ["terme critique"]


def test_prepare_hotwords_bench_generates_baseline_and_hotwords_commands(tmp_path):
    module = _load_script("prepare_hotwords_bench.py")
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    (audio_dir / "test file.mp3").write_bytes(b"fake")

    cases = module.discover_audio_files([audio_dir])
    assert len(cases) == 1
    assert cases[0].slug == "test-file"

    class Args:
        audio_root = [audio_dir]
        output_dir = tmp_path / "out"
        mode = "fast"
        keep = True
        skip_llm = False
        skip_summary = False
        skip_diarization = True
        lexicon_json = None
        limit = None
        gpus = "3,5"
        max_parallel = None

    manifest = module.build_manifest(Args)

    assert manifest["audio_count"] == 1
    assert manifest["run_count"] == 2
    variants = {run["variant"] for run in manifest["runs"]}
    assert variants == {"baseline", "hotwords"}
    hotwords_run = next(run for run in manifest["runs"] if run["variant"] == "hotwords")
    assert "--enable-whisper-lexicon-hotwords" in hotwords_run["command"]
    assert manifest["max_parallel"] == 2

    shell = tmp_path / "run.sh"
    module.write_shell(manifest, shell, parallel=True)
    content = shell.read_text(encoding="utf-8")
    assert "max_parallel=2" in content
    assert "wait -n" in content
    assert "failures=$((failures + 1))" in content


def test_prepare_hybrid_llm_bench_generates_three_speaker_runs(tmp_path):
    module = _load_script("prepare_hybrid_llm_bench.py")
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    audio = audio_dir / "réunion test.wav"
    audio.write_bytes(b"fake")
    lexicon = tmp_path / "lexicon.json"
    lexicon.write_text('[{"term": "Terme critique", "priority": "critique"}]', encoding="utf-8")

    class Args:
        audio_root = [audio_dir]
        output_dir = tmp_path / "out"
        mode = "fast"
        whisper_model_size = "large-v3"
        gpus = "2,3"
        max_parallel = None
        limit = None
        lexicon_json = lexicon
        enable_cohere_lexicon_biasing = False
        config_override = []

    manifest = module.build_manifest(Args)

    assert manifest["run_count"] == 3
    assert manifest["with_speakers"] is True
    assert manifest["llm_in_e2e"] is False
    variants = {run["variant"] for run in manifest["runs"]}
    assert variants == {"A-cohere", "B-whisper", "C-whisper-hotwords"}
    assert all("--skip-llm" in run["command"] for run in manifest["runs"])
    assert all("--skip-diarization" not in run["command"] for run in manifest["runs"])
    hotwords = next(run for run in manifest["runs"] if run["variant"] == "C-whisper-hotwords")
    assert "--enable-whisper-lexicon-hotwords" in hotwords["command"]


def test_bench_audio_passes_physical_gpu_without_cuda_visible_mask(tmp_path, monkeypatch):
    module = _load_script("bench_audio.py")
    args = SimpleNamespace(
        whisper_model_size="large-v3",
        pipeline_mode="quality",
        with_llm=False,
        config_override=[],
        skip_diarization=False,
        remote_stt=None,
        remote_stt_api_key=None,
        remote_inference=None,
        remote_inference_api_key=None,
        lexicon_json=None,
        lexicon_term=[],
        resume=False,
        dry_run=False,
        verbose=False,
    )
    combo = {
        "id": "S01",
        "stt": "cohere",
        "scene": False,
        "filter": False,
        "norm": False,
        "sep": False,
        "overrides": [],
    }

    cmd = module.build_e2e_cmd(combo, tmp_path / "audio.wav", tmp_path / "S01.json", "3", None, args)
    assert "--gpu" in cmd
    assert cmd[cmd.index("--gpu") + 1] == "3"
    assert "--skip-llm" in cmd

    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "3")
    captured = {}

    class _FakeProc:
        returncode = 0
        stdout = []

        def wait(self):
            return None

    def fake_popen(cmd, **kwargs):
        captured["env"] = kwargs["env"]
        return _FakeProc()

    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)
    module.run_one_combo(combo, tmp_path / "audio.wav", tmp_path, "3", None, args, worker_id=0)
    assert "CUDA_VISIBLE_DEVICES" not in captured["env"]


def test_bench_audio_output_dir_is_partitioned_for_multiple_audio(tmp_path):
    module = _load_script("bench_audio.py")
    args = SimpleNamespace(output_dir=tmp_path / "calibration", audio=[Path("a.wav"), Path("dir/test file.wav")])

    assert module.resolve_output_dir(args, Path("dir/test file.wav")) == tmp_path / "calibration" / "test_file"
    args_single = SimpleNamespace(output_dir=tmp_path / "single", audio=[Path("a.wav")])
    assert module.resolve_output_dir(args_single, Path("a.wav")) == tmp_path / "single"


def test_bench_audio_vad_matrix_targets_final_and_internal_vad():
    module = _load_script("bench_audio.py")
    args = SimpleNamespace(matrix="vad", group=None, combos=None)

    combos = module.select_combos(args)

    assert len(combos) == 8
    assert {combo["id"] for combo in combos} == {f"V{i:02d}" for i in range(1, 9)}
    assert any("workflow.vad.enabled_final=true" in combo["overrides"] for combo in combos)
    assert any("whisper.vad_filter=true" in combo["overrides"] for combo in combos)
    assert any("whisper.vad_filter=false" in combo["overrides"] for combo in combos if combo["stt"] == "whisper")
    assert all(combo["skip_diarization"] is False for combo in combos)


def test_bench_audio_vad_combo_id_normalization_and_summary_columns():
    module = _load_script("bench_audio.py")
    args = SimpleNamespace(matrix="all", group=None, combos="v1,V08")

    combos = module.select_combos(args)

    assert [combo["id"] for combo in combos] == ["V01", "V08"]

    row = module._extract_row({
        "combo_id": "V08",
        "stt_backend": "whisper",
        "status": "ok",
        "skip_diarization": False,
        "config_overrides": {
            "workflow.vad.enabled_summary": False,
            "workflow.vad.enabled_final": False,
            "whisper.vad_filter": True,
        },
        "srt": {},
        "timings": {},
        "artifacts": {},
        "transcription_metadata": {},
    })
    assert row["vad_summary"] == "summary-off"
    assert row["vad_final"] == "final-off"
    assert row["whisper_vad_filter"] == "whisper-vad-on"


def test_bench_audio_cohere_tune_matrix_is_pyannote_vad_off():
    module = _load_script("bench_audio.py")
    args = SimpleNamespace(matrix="cohere_tune", group=None, combos=None)

    combos = module.select_combos(args)

    assert len(combos) == 9
    assert {combo["id"] for combo in combos} == {f"T{i:02d}" for i in range(1, 10)}
    assert all(combo["stt"] == "cohere" for combo in combos)
    assert all(combo["skip_diarization"] is False for combo in combos)
    assert all(combo["diarization_backend"] == "pyannote" for combo in combos)
    assert all("workflow.vad.enabled_summary=false" in combo["overrides"] for combo in combos)
    assert all("workflow.vad.enabled_final=false" in combo["overrides"] for combo in combos)
    assert any("workflow.pyannote_chunking.max_chunk_s=20" in combo["overrides"] for combo in combos)
    assert any("cohere.punctuation=false" in combo["overrides"] for combo in combos)
    assert any(combo.get("enable_cohere_lexicon_biasing") is True for combo in combos)


def test_bench_audio_cohere_tune_normalization_and_lexicon_forwarding(tmp_path):
    module = _load_script("bench_audio.py")
    args_select = SimpleNamespace(matrix="all", group=None, combos="t9")
    combos = module.select_combos(args_select)
    assert [combo["id"] for combo in combos] == ["T09"]

    lexicon_json = tmp_path / "lexicon.json"
    lexicon_json.write_text("[]", encoding="utf-8")
    args = SimpleNamespace(
        whisper_model_size="large-v3",
        pipeline_mode="quality",
        with_llm=False,
        config_override=[],
        skip_diarization=False,
        remote_stt=None,
        remote_stt_api_key=None,
        remote_inference=None,
        remote_inference_api_key=None,
        lexicon_json=lexicon_json,
        lexicon_term=["EBITDA|critique"],
    )

    cmd = module.build_e2e_cmd(combos[0], tmp_path / "audio.wav", tmp_path / "T09.json", "3", None, args)

    assert "--enable-cohere-lexicon-biasing" in cmd
    assert "--lexicon-json" in cmd
    assert cmd[cmd.index("--lexicon-json") + 1] == str(lexicon_json)
    assert "--lexicon-term" in cmd
    assert cmd[cmd.index("--lexicon-term") + 1] == "EBITDA|critique"


def test_bench_audio_pyannote_tune_matrix_excludes_exact_without_known_speakers():
    module = _load_script("bench_audio.py")
    args = SimpleNamespace(matrix="pyannote_tune", group=None, combos=None, known_speakers=None)

    combos = module.select_combos(args)

    assert len(combos) == 10
    assert "P02" not in {combo["id"] for combo in combos}
    assert all(combo["stt"] == "cohere" for combo in combos)
    assert all(combo["diarization_backend"] == "pyannote" for combo in combos)
    assert all("workflow.vad.enabled_final=false" in combo["overrides"] for combo in combos)
    assert any("workflow.pyannote_chunking.padding_s=0.30" in combo["overrides"] for combo in combos)


def test_bench_audio_pyannote_tune_matrix_injects_known_speakers():
    module = _load_script("bench_audio.py")
    args = SimpleNamespace(matrix="pyannote_tune", group=None, combos="p2", known_speakers=7)

    combos = module.select_combos(args)

    assert [combo["id"] for combo in combos] == ["P02"]
    assert combos[0]["known_speakers"] == 7
    assert "diarization.num_speakers=7" in combos[0]["overrides"]


def test_arbitrate_hybrid_llm_candidate_accepts_e2e_result_json(tmp_path):
    module = _load_script("arbitrate_hybrid_llm.py")
    result = tmp_path / "result.json"
    result.write_text('{"job_id": "abc-123"}', encoding="utf-8")

    candidate = module._parse_candidate(f"A:cohere={result}", tmp_path / "jobs")

    assert candidate.code == "A"
    assert candidate.label == "cohere"
    assert candidate.job_id == "abc-123"


def test_analyze_hotwords_bench_pairs_results_and_deltas(tmp_path):
    module = _load_script("analyze_hotwords_bench.py")
    results = tmp_path / "results"
    results.mkdir()
    baseline_job = tmp_path / "job_base"
    hotwords_job = tmp_path / "job_hot"
    for job_dir, score, warnings in ((baseline_job, 80, 2), (hotwords_job, 85, 1)):
        (job_dir / "metadata").mkdir(parents=True)
        (job_dir / "quality").mkdir()
        (job_dir / "metadata" / "transcription.srt").write_text("Terme critique présent", encoding="utf-8")
        (job_dir / "quality" / "quality_report.json").write_text(
            f'{{"quality_score": {score}, "warnings": {warnings}}}',
            encoding="utf-8",
        )

    common = {
        "audio_path": "/tmp/audio.wav",
        "status": "ok",
        "errors": [],
        "timings": {"pipeline_s": 10.0},
        "srt": {"raw_segments": 2, "raw_words": 3},
        "segment_reliability_counts": {"ok": 2},
    }
    (results / "sample-whisper-baseline.json").write_text(
        __import__("json").dumps({
            **common,
            "combo_id": "sample-whisper-baseline",
            "job_id": "base",
            "job_dir": str(baseline_job),
        }),
        encoding="utf-8",
    )
    (results / "sample-whisper-hotwords.json").write_text(
        __import__("json").dumps({
            **common,
            "combo_id": "sample-whisper-hotwords",
            "job_id": "hot",
            "job_dir": str(hotwords_job),
            "srt": {"raw_segments": 3, "raw_words": 4},
            "segment_reliability_counts": {"ok": 2, "suspect": 1},
            "whisper_hotwords_data": {"injected_terms": 1, "candidate_terms": 2, "token_count": 5},
        }),
        encoding="utf-8",
    )
    lexicon = tmp_path / "lexicon.json"
    lexicon.write_text('[{"term": "Terme critique"}]', encoding="utf-8")

    report = module.build_report(results, lexicon)

    assert report["complete_pair_count"] == 1
    row = report["rows"][0]
    assert row["delta"]["quality_score"] == 5


def test_bench_analyze_filters_legacy_and_exports_calibration_columns(tmp_path):
    import csv
    import json

    module = _load_script("bench_analyze.py")
    bench_dir = tmp_path / "bench"
    bench_dir.mkdir()

    compatible = {
        "schema_version": 2,
        "combo_id": "V01",
        "audio_path": "/tmp/audio.wav",
        "stt_backend": "cohere",
        "effective_stt_backend": "cohere",
        "status": "ok",
        "mode": "quality",
        "skip_diarization": False,
        "gpu": "3",
        "config_overrides": {"workflow.vad.enabled_summary": False},
        "timings": {"summary_s": 11.2, "pipeline_s": 22.5},
        "vram_peak_mb": 6144,
        "srt": {"raw_segments": 2, "raw_words": 8},
        "audio_corpus": {
            "schema_version": 1,
            "risk_level": "suspect",
            "flags": ["squim_pesq_faible"],
            "snr_db": 18.5,
            "bandwidth_95_hz": 3200.0,
            "squim_global": {"stoi": 0.82, "pesq": 1.9, "sisdr": 3.1},
            "dnsmos_global": {"sig": 3.0, "bak": 3.7, "ovrl": 2.4},
            "difficulty_summary": {
                "windows": 12,
                "ok": 7,
                "suspect": 3,
                "degrade": 2,
                "degrade_ratio": 0.1667,
                "worst": "degrade",
            },
        },
        "quality_decision": {"level": "suspect"},
        "transcription_metadata": {
            "backend": "cohere",
            "chunking_mode": "pyannote_turns",
            "segments": 2,
            "chunk_metrics": {"workers": 1},
        },
        "segment_reliability_counts": {"ok": 1, "suspect": 1, "degrade": 0},
        "transcription_segments": [
            {"start": 0.0, "end": 1.0, "text": "bonjour", "no_speech_prob": 0.1, "words": [{"word": "bonjour", "probability": 0.9}]},
            {"start": 1.0, "end": 2.0, "text": "bruit", "no_speech_prob": 0.8, "words": [{"word": "bruit", "probability": 0.2}]},
        ],
    }
    legacy = {
        "combo_id": "S02",
        "status": "ok",
        "stt_backend": "whisper",
        "srt": {"raw_segments": 1, "raw_words": 2},
    }
    (bench_dir / "V01.json").write_text(json.dumps(compatible), encoding="utf-8")
    (bench_dir / "S02.json").write_text(json.dumps(legacy), encoding="utf-8")

    loaded = module.load_bench_dir(bench_dir)
    supported, ignored = module.split_supported_results(loaded)
    assert [r["combo_id"] for r in supported] == ["V01"]
    assert ignored[0]["_schema_errors"] == [
        "schema_version<2",
        "audio_corpus_absent",
        "transcription_metadata_absent",
        "segment_reliability_counts_absent",
    ]

    row = module.analyze_combo(supported[0])
    assert row["risk_level"] == "suspect"
    assert row["difficulty_degrade_ratio"] == 0.1667
    assert row["squim_pesq"] == 1.9
    assert row["dnsmos_ovrl"] == 2.4
    assert row["chunking_mode"] == "pyannote_turns"
    assert row["rel_suspect"] == 1
    assert row["hallucination_score"] > 0

    csv_path = tmp_path / "analysis.csv"
    module.write_csv([row], csv_path)
    csv_row = next(csv.DictReader(csv_path.open(encoding="utf-8")))
    assert csv_row["risk_level"] == "suspect"
    assert csv_row["effective_stt"] == "cohere"
    assert csv_row["difficulty_degrade_ratio"] == "0.1667"


def test_bench_analyze_qualitative_review_flags_srt_relecture(tmp_path):
    module = _load_script("bench_analyze.py")
    result = {
        "schema_version": 2,
        "combo_id": "S07",
        "stt_backend": "whisper",
        "effective_stt_backend": "whisper",
        "status": "ok",
        "skip_diarization": False,
        "timings": {},
        "srt": {
            "raw_segments": 2,
            "raw_words": 8,
            "raw_content": (
                "1\n00:00:00,000 --> 00:00:02,000\n"
                "SPEAKER_00: ولقد صار السنه هذه الايه\n\n"
                "2\n00:00:02,000 --> 00:00:04,000\n"
                "SPEAKER_00: Merci d'avoir regardé cette vidéo !\n"
            ),
        },
        "audio_corpus": {"schema_version": 1, "difficulty_summary": {}},
        "transcription_metadata": {},
        "segment_reliability_counts": {"ok": 0, "suspect": 0, "degrade": 2},
        "transcription_segments": [],
    }

    review = module.qualitative_review(result)
    assert review["review_required"] is True
    assert "script_non_latin" in review["review_reasons"]
    assert "phrase_generique_suspecte" in review["review_reasons"]
    assert review["non_latin_scripts"] == "arabic"

    row = module.analyze_combo(result)
    assert row["review_required"] is True
    assert row["manual_verdict"] == ""

    output = tmp_path / "analysis.md"
    module._raw_data = [result]
    module.write_report([row], output)
    content = output.read_text(encoding="utf-8")
    assert "Ces signaux ne sont pas un verdict automatique" in content
    assert "Relecture qualitative assistée" in content
    assert "ولقد صار" in content


def test_extract_reference_docx_parses_market_transcript(tmp_path):
    from docx import Document

    module = _load_script("extract_reference_docx.py")
    docx_path = tmp_path / "reference.docx"
    doc = Document()
    for text in (
        "INTERVENANT_01",
        "00:02:00 - 00:02:28 DUREE : 00:00:28",
        "Nombre de mots : 2",
        "bonjour, bonjour",
        "INTERVENANT_12",
        "01:00:00 - 01:00:03 DUREE : 00:00:03",
        "Nombre de mots : 3",
        "un deux trois",
    ):
        doc.add_paragraph(text)
    doc.save(docx_path)

    reference = module.parse_reference_docx(docx_path)

    assert reference["segment_count"] == 2
    assert reference["speaker_count"] == 2
    assert reference["segments"][0]["speaker"] == "INTERVENANT_01"
    assert reference["segments"][0]["start"] == 120.0
    assert reference["segments"][1]["speaker"] == "INTERVENANT_12"
    assert reference["computed_word_count"] == 5
    assert reference["parse_warnings"] == []

    srt_path = tmp_path / "reference.srt"
    module.write_srt(reference, srt_path)
    srt = srt_path.read_text(encoding="utf-8")
    assert "00:02:00,000 --> 00:02:28,000" in srt
    assert "INTERVENANT_12: un deux trois" in srt


def test_prepare_reference_windows_clips_reference_and_manifest(tmp_path, monkeypatch):
    import json

    module = _load_script("prepare_reference_windows.py")
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"fake")
    reference = {
        "source_docx": "ref.docx",
        "segments": [
            {"speaker": "INTERVENANT_01", "start": 10.0, "end": 20.0, "declared_words": 2, "word_count": 2, "text": "avant dedans"},
            {"speaker": "INTERVENANT_02", "start": 25.0, "end": 40.0, "declared_words": 3, "word_count": 3, "text": "dedans apres fin"},
            {"speaker": "INTERVENANT_03", "start": 50.0, "end": 60.0, "declared_words": 1, "word_count": 1, "text": "hors"},
        ],
    }
    reference_path = tmp_path / "reference.json"
    reference_path.write_text(json.dumps(reference), encoding="utf-8")

    def fake_write_audio(audio_path, output_path, start, end):
        output_path.write_bytes(b"wav")

    monkeypatch.setattr(module, "write_audio_window", fake_write_audio)
    manifest = module.prepare_windows(audio, reference_path, tmp_path / "out", [(15.0, 35.0)])

    assert len(manifest["windows"]) == 1
    window = manifest["windows"][0]
    assert window["speaker_count"] == 2
    assert window["word_count"] == 5
    clipped = json.loads(Path(window["reference_json"]).read_text(encoding="utf-8"))
    assert clipped["segments"][0]["start"] == 0.0
    assert clipped["segments"][0]["end"] == 5.0
    assert clipped["segments"][0]["absolute_start"] == 10.0
    assert clipped["segments"][1]["start"] == 10.0
    assert clipped["segments"][1]["end"] == 20.0
    assert Path(window["audio"]).read_bytes() == b"wav"
    assert "INTERVENANT_02" in Path(window["reference_srt"]).read_text(encoding="utf-8")


def test_score_reference_bench_scores_srt_against_reference():
    module = _load_script("score_reference_bench.py")

    reference = "Bonjour tout le monde"
    hypothesis_srt = """1
00:00:00,000 --> 00:00:02,000
SPEAKER_00(SPEAKER_00): Bonjour tout le monde
"""

    scores = module.score_pair(reference, module.srt_text(hypothesis_srt))

    assert scores["ref_words"] == 4
    assert scores["hyp_words"] == 4
    assert scores["wer"] == 0.0
    assert scores["cer"] == 0.0


def test_score_reference_bench_loads_cohere_tune_results(tmp_path):
    module = _load_script("score_reference_bench.py")
    result_path = tmp_path / "T09.json"
    result_path.write_text(json.dumps({"combo_id": "T09", "status": "ok"}), encoding="utf-8")

    results = module.load_results(tmp_path)

    assert len(results) == 1
    assert results[0]["combo_id"] == "T09"
    assert results[0]["_path"] == str(result_path)


def test_bench_cohere_tf5_chunks_turns_and_formats_srt():
    import numpy as np

    module = _load_script("bench_cohere_tf5.py")
    audio = np.zeros(10 * 16000, dtype=np.float32)
    turns = [{"start": 1.0, "end": 6.5, "speaker": "SPEAKER_01"}]

    chunks = module.chunk_turns(turns, audio, 16000, max_chunk_s=2.0)

    assert [(round(chunk.start, 1), round(chunk.end, 1), chunk.speaker) for chunk in chunks] == [
        (1.0, 3.0, "SPEAKER_01"),
        (3.0, 5.0, "SPEAKER_01"),
        (5.0, 6.5, "SPEAKER_01"),
    ]
    srt = module.segments_to_srt([
        {"start": 1.0, "end": 2.5, "speaker": "SPEAKER_01", "text": "Bonjour"},
    ])
    assert "00:00:01,000 --> 00:00:02,500" in srt
    assert "SPEAKER_01: Bonjour" in srt


def test_arbitrate_hybrid_llm_builds_speaker_aware_units(tmp_path):
    module = _load_script("arbitrate_hybrid_llm.py")
    jobs_dir = tmp_path / "jobs"
    for job_id, text in (
        ("job_a", "Bonjour par A."),
        ("job_b", "Bonjour par B."),
        ("job_c", "Bonjour par C."),
    ):
        metadata = jobs_dir / job_id / "metadata"
        metadata.mkdir(parents=True)
        (metadata / "transcription_segments.json").write_text(
            __import__("json").dumps([
                {"start": 0.0, "end": 10.0, "speaker": "SPEAKER_00", "text": text, "reliability": "ok"}
            ]),
            encoding="utf-8",
        )
    speakers = jobs_dir / "job_a" / "speakers"
    speakers.mkdir()
    (speakers / "speaker_turns.json").write_text(
        '{"exclusive_turns":[{"start":0.0,"end":10.0,"speaker":"SPEAKER_00"}]}',
        encoding="utf-8",
    )
    (speakers / "speaker_stats.json").write_text(
        '{"speakers":[{"speaker_id":"SPEAKER_00","mapped_name":"Alice","gender":"female"}]}',
        encoding="utf-8",
    )

    candidates = [
        module._parse_candidate("A:cohere=job_a", jobs_dir),
        module._parse_candidate("B:whisper=job_b", jobs_dir),
        module._parse_candidate("C:whisper_hotwords=job_c", jobs_dir),
    ]
    dataset = module.build_units(candidates, jobs_dir, None, [], window_s=15.0, context_chars=200)

    unit = dataset["units"][0]
    assert unit["speakers"] == ["SPEAKER_00"]
    assert "Alice" in unit["speaker_context"][0]
    assert unit["candidates"][0]["speaker_text"].startswith("SPEAKER_00:")


def test_arbitrate_hybrid_llm_accepts_two_candidates_in_prompt(tmp_path):
    module = _load_script("arbitrate_hybrid_llm.py")
    jobs_dir = tmp_path / "jobs"
    for job_id, text in (("job_a", "Bonjour par A."), ("job_b", "Bonjour par B.")):
        metadata = jobs_dir / job_id / "metadata"
        metadata.mkdir(parents=True)
        (metadata / "transcription_segments.json").write_text(
            json.dumps([{"start": 0.0, "end": 10.0, "speaker": "SPEAKER_00", "text": text, "reliability": "ok"}]),
            encoding="utf-8",
        )

    candidates = [
        module._parse_candidate("A:cohere=job_a", jobs_dir),
        module._parse_candidate("B:whisper=job_b", jobs_dir),
    ]
    dataset = module.build_units(candidates, jobs_dir, None, [], window_s=15.0, context_chars=200)
    system_prompt, user_prompt = module._build_batch_prompt(dataset, dataset["units"])

    assert "uniquement A|B|D" in system_prompt
    assert "C =" not in user_prompt


def test_arbitrate_hybrid_llm_filters_review_windows_from_hybrid_report(tmp_path):
    module = _load_script("arbitrate_hybrid_llm.py")
    dataset = {
        "units": [
            {"segment_id": "win_00000", "start": 0.0, "end": 30.0},
            {"segment_id": "win_00001", "start": 30.0, "end": 60.0},
        ]
    }
    report = {
        "windows": [
            {"start": 0.0, "end": 30.0, "decision": "cohere"},
            {"start": 30.0, "end": 60.0, "decision": "review"},
        ]
    }
    path = tmp_path / "hybrid.json"
    path.write_text(json.dumps(report), encoding="utf-8")

    filtered = module.filter_units_from_hybrid_report(dataset, path)

    assert [unit["segment_id"] for unit in filtered["units"]] == ["win_00001"]
    assert filtered["unit_filter"]["requested_windows"] == 1
    assert filtered["unit_filter"]["kept_units"] == 1


def test_arbitrate_hybrid_llm_enriches_risky_selected_candidate():
    module = _load_script("arbitrate_hybrid_llm.py")
    unit = {
        "segment_id": "win_00001",
        "candidates": [
            {
                "code": "A",
                "label": "cohere",
                "reliability": "suspect",
                "no_speech_prob": 0.72,
                "low_word_ratio": 0.2,
                "word_count": 12,
                "generic_hallucinations": ["sous-titrage"],
            }
        ],
    }
    decision = {"segment_id": "win_00001", "choice": "A", "confidence": "high", "reason": "ok", "risks": []}

    enriched = module._enrich_decision_audit(unit, decision)

    assert enriched["selected_label"] == "cohere"
    assert enriched["selected_reliability"] == "suspect"
    assert "high_confidence_on_non_ok_candidate" in enriched["audit_warnings"]
    assert "selected_high_no_speech_prob" in enriched["audit_warnings"]
    assert "selected_generic_hallucination" in enriched["audit_warnings"]


def test_arbitrate_hybrid_llm_parses_fenced_json():
    module = _load_script("arbitrate_hybrid_llm.py")

    parsed = module.parse_llm_response(
        '```json\n{"decisions":[{"segment_id":"win_00000","choice":"b","confidence":"high","reason":"ok","risks":[]}]}\n```'
    )

    assert parsed["decisions"][0]["choice"] == "B"


def test_arbitrate_hybrid_llm_recovers_unescaped_quote_in_reason():
    module = _load_script("arbitrate_hybrid_llm.py")

    parsed = module.parse_llm_response(
        """```json
{
  "decisions": [
    {
      "segment_id": "win_00087",
      "choice": "A",
      "confidence": "high",
      "reason": "C contient des fragments incohérents ('nom d', "'un...").",
      "risks": []
    }
  ]
}
```"""
    )

    assert parsed["decisions"][0]["segment_id"] == "win_00087"
    assert parsed["decisions"][0]["choice"] == "A"
    assert parsed["decisions"][0]["confidence"] == "high"
    assert parsed["parse_warning"] == "json_lenient_recovery"


def test_build_hybrid_transcript_rejects_generic_hallucination(tmp_path):
    module = _load_script("build_hybrid_transcript.py")
    cohere = tmp_path / "cohere.json"
    whisper = tmp_path / "whisper.json"
    cohere.write_text(
        """
        [
          {
            "start": 0.0,
            "end": 30.0,
            "text": "Pour plus d'informations, contactez-nous sur le site web de l'Université d'Ottawa.",
            "reliability": "ok"
          }
        ]
        """,
        encoding="utf-8",
    )
    whisper.write_text(
        """
        [
          {"start": 0.0, "end": 30.0, "text": "La réunion reprend avec un point sur le planning.", "reliability": "ok"}
        ]
        """,
        encoding="utf-8",
    )

    report = module.build_hybrid_report(
        [
            module.CandidateSource("cohere", "", cohere),
            module.CandidateSource("whisper", "", whisper),
        ],
        [],
        window_s=30.0,
        decision_margin=3,
    )

    window = report["windows"][0]
    assert window["decision"] == "whisper"
    cohere_candidate = next(candidate for candidate in window["candidates"] if candidate["label"] == "cohere")
    assert cohere_candidate["generic_hallucinations"]


def test_build_hybrid_transcript_lexicon_hits_use_word_boundaries():
    module = _load_script("build_hybrid_transcript.py")

    assert module._term_hits("des différents tribunaux", ["DIF"]) == []
    assert module._term_hits("le DIF est mobilisé", ["DIF"]) == ["DIF"]


def test_build_hybrid_transcript_keeps_review_when_scores_are_close(tmp_path):
    module = _load_script("build_hybrid_transcript.py")
    left = tmp_path / "left.json"
    right = tmp_path / "right.json"
    left.write_text('[{"start": 0, "end": 10, "text": "texte stable", "reliability": "ok"}]', encoding="utf-8")
    right.write_text('[{"start": 0, "end": 10, "text": "texte stable", "reliability": "ok"}]', encoding="utf-8")

    report = module.build_hybrid_report(
        [
            module.CandidateSource("cohere", "", left),
            module.CandidateSource("whisper", "", right),
        ],
        [],
        window_s=10.0,
        decision_margin=3,
    )

    assert report["windows"][0]["decision"] == "review"
    assert report["windows"][0]["selected_label"] in {"cohere", "whisper"}


def test_build_hybrid_transcript_keeps_review_for_suspect_best_without_terms(tmp_path):
    module = _load_script("build_hybrid_transcript.py")
    suspect = module.CandidateWindow(
        label="cohere",
        text="long texte plausible mais marqué suspect",
        reliability="suspect",
        no_speech_prob=None,
        low_word_ratio=None,
        source_indices=[0],
        segment_count=1,
        word_count=6,
        char_count=38,
        term_hits=[],
        generic_hallucinations=[],
    )
    degraded = module.CandidateWindow(
        label="whisper",
        text="texte court",
        reliability="degrade",
        no_speech_prob=0.7,
        low_word_ratio=None,
        source_indices=[0],
        segment_count=1,
        word_count=2,
        char_count=11,
        term_hits=[],
        generic_hallucinations=[],
    )

    row = module.choose_window([suspect, degraded], 0.0, 30.0, margin=3)

    assert row["decision"] == "review"
    assert row["selected_label"] == "cohere"


def test_build_hybrid_transcript_primary_fast_path_keeps_clean_primary():
    module = _load_script("build_hybrid_transcript.py")
    cohere = module.CandidateWindow(
        label="cohere",
        text="texte principal propre et complet",
        reliability="ok",
        no_speech_prob=None,
        low_word_ratio=None,
        source_indices=[0],
        segment_count=1,
        word_count=5,
        char_count=33,
        term_hits=[],
        generic_hallucinations=[],
    )
    whisper = module.CandidateWindow(
        label="whisper",
        text="texte alternatif un peu plus long et propre",
        reliability="ok",
        no_speech_prob=0.01,
        low_word_ratio=0.0,
        source_indices=[0],
        segment_count=1,
        word_count=8,
        char_count=42,
        term_hits=[],
        generic_hallucinations=[],
    )

    row = module.choose_window([cohere, whisper], 0.0, 30.0, margin=3, primary_label="cohere")

    assert row["decision"] == "cohere"
    assert row["selected_label"] == "cohere"


def test_build_hybrid_transcript_primary_fallback_uses_safe_alternative():
    module = _load_script("build_hybrid_transcript.py")
    cohere = module.CandidateWindow(
        label="cohere",
        text="texte halluciné générique",
        reliability="degrade",
        no_speech_prob=None,
        low_word_ratio=None,
        source_indices=[0],
        segment_count=1,
        word_count=3,
        char_count=24,
        term_hits=[],
        generic_hallucinations=["site web"],
    )
    whisper = module.CandidateWindow(
        label="whisper",
        text="la réunion reprend avec un point métier stable",
        reliability="ok",
        no_speech_prob=0.01,
        low_word_ratio=0.0,
        source_indices=[0],
        segment_count=1,
        word_count=8,
        char_count=46,
        term_hits=[],
        generic_hallucinations=[],
    )

    row = module.choose_window([cohere, whisper], 0.0, 30.0, margin=3, primary_label="cohere")

    assert row["decision"] == "whisper"
    assert row["selected_label"] == "whisper"


def test_build_hybrid_transcript_writes_readable_srt_chunks(tmp_path):
    module = _load_script("build_hybrid_transcript.py")
    report = {
        "windows": [
            {
                "start": 0.0,
                "end": 12.0,
                "selected_text": "Première phrase très simple. Deuxième phrase avec plusieurs mots pour vérifier le découpage.",
            }
        ]
    }
    output = tmp_path / "hybrid.srt"

    module.write_srt(report, output, max_words=4)

    text = output.read_text(encoding="utf-8")
    assert "00:00:00,000 -->" in text
    assert "Première phrase très simple." in text
    assert "Deuxième phrase avec plusieurs" in text
    assert "mots pour vérifier le" in text
    assert "découpage." in text
