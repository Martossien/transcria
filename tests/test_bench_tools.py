import importlib.util
import sys
from pathlib import Path


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
    assert row["delta"]["warnings"] == -1
    assert row["delta"]["segments"] == 1
    assert row["hotwords"]["hotwords_injected"] == 1


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
          {"start": 0.0, "end": 30.0, "text": "Pour plus d'informations, contactez-nous sur le site web de l'Université d'Ottawa.", "reliability": "ok"}
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
