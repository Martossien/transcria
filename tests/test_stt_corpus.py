"""Tests du corpus difficulté↔qualité STT par segment (brique 2 de calibration)."""

from types import SimpleNamespace

from transcria.stt.corpus import (
    build_segment_corpus,
    difficulty_for_range,
    summarize_corpus,
)

_MAP = [
    {"start": 0.0, "end": 5.0, "difficulty": "ok", "signals": []},
    {"start": 2.5, "end": 7.5, "difficulty": "suspect", "signals": ["squim_pesq_faible"]},
    {"start": 5.0, "end": 10.0, "difficulty": "degrade", "signals": ["overlap", "squim_stoi_faible"]},
    {"start": 7.5, "end": 12.5, "difficulty": "degrade", "signals": ["squim_stoi_faible"]},
]


def test_difficulty_for_range_empty_map_returns_none():
    assert difficulty_for_range([], 0.0, 5.0) is None
    assert difficulty_for_range(None, 0.0, 5.0) is None


def test_difficulty_for_range_no_overlap_returns_none():
    assert difficulty_for_range(_MAP, 100.0, 110.0) is None


def test_difficulty_for_range_single_window_overlap():
    res = difficulty_for_range(_MAP, 0.0, 2.0)
    assert res["level"] == "ok"
    assert res["windows"] == 1
    assert res["signals"] == []
    assert res["degrade_ratio"] == 0.0


def test_difficulty_for_range_takes_worst_and_unions_signals():
    # [4.0, 9.0] chevauche ok, suspect, et les deux degrade.
    res = difficulty_for_range(_MAP, 4.0, 9.0)
    assert res["level"] == "degrade"
    assert res["windows"] == 4
    # union triée des signaux des fenêtres chevauchées
    assert res["signals"] == ["overlap", "squim_pesq_faible", "squim_stoi_faible"]
    # 2 fenêtres degrade sur 4 chevauchées
    assert res["degrade_ratio"] == 0.5


def _seg(start, end, **kw):
    base = {"start": start, "end": end, "text": "bonjour le monde"}
    base.update(kw)
    return base


def test_build_segment_corpus_fields_and_difficulty_join():
    segs = [
        _seg(0.0, 2.0, reliability="ok", reliability_reasons=[]),
        _seg(6.0, 9.0, reliability="degrade", reliability_reasons=["mots_faible_confiance"],
             no_speech_prob=0.7, avg_logprob=-1.2,
             words=[{"probability": 0.9}, {"probability": 0.2}, {"probability": 0.3}]),
    ]
    corpus = build_segment_corpus(segs, backend="whisper", difficulty_map=_MAP)
    assert len(corpus) == 2

    a, b = corpus
    assert a["backend"] == "whisper"
    assert a["n_words"] == 3
    assert a["difficulty"] == "ok"
    assert a["quality_measure"] is None
    # pas de confiance native sur ce segment Cohere-like
    assert a["avg_logprob"] is None
    assert a["no_speech_prob"] is None
    assert a["word_conf_mean"] is None
    assert a["low_word_conf_ratio"] is None

    assert b["difficulty"] == "degrade"
    assert b["reliability"] == "degrade"
    assert b["no_speech_prob"] == 0.7
    assert b["avg_logprob"] == -1.2
    assert round(b["word_conf_mean"], 3) == round((0.9 + 0.2 + 0.3) / 3, 3)
    # 2 mots sur 3 sous le seuil 0.4
    assert round(b["low_word_conf_ratio"], 3) == round(2 / 3, 3)
    assert "overlap" in b["difficulty_signals"]


def test_build_segment_corpus_join_matches_bruteforce_on_varied_map():
    # Map non triée, longueurs de fenêtres variées (chevauchements) → le chemin
    # optimisé (bisect) doit donner exactement le même verdict que la jointure brute.
    import random

    rnd = random.Random(42)
    raw_map = []
    for _ in range(400):
        s = round(rnd.uniform(0, 600), 2)
        length = rnd.choice([5.0, 10.0, 2.5])
        level = rnd.choice(["ok", "suspect", "degrade"])
        raw_map.append({"start": s, "end": s + length, "difficulty": level, "signals": [level]})

    segs = [_seg(round(rnd.uniform(0, 600), 2), 0.0) for _ in range(200)]
    for seg in segs:
        seg["end"] = seg["start"] + rnd.choice([0.8, 3.0, 7.0])

    corpus = build_segment_corpus(segs, "cohere", raw_map)
    for seg, row in zip(segs, corpus):
        brute = difficulty_for_range(raw_map, seg["start"], seg["end"])
        assert row["difficulty"] == (brute["level"] if brute else None)
        assert row["difficulty_signals"] == (brute["signals"] if brute else [])


def test_build_segment_corpus_no_map_leaves_difficulty_none():
    corpus = build_segment_corpus([_seg(0.0, 2.0, reliability="ok")], backend="cohere", difficulty_map=[])
    assert corpus[0]["difficulty"] is None
    assert corpus[0]["difficulty_signals"] == []


def test_summarize_corpus_contingency_and_means():
    segs = [
        _seg(0.0, 2.0, reliability="ok"),
        _seg(6.0, 9.0, reliability="degrade", no_speech_prob=0.8,
             words=[{"probability": 0.2}, {"probability": 0.2}]),
        _seg(7.6, 9.0, reliability="suspect", no_speech_prob=0.4,
             words=[{"probability": 0.9}, {"probability": 0.9}]),
    ]
    corpus = build_segment_corpus(segs, backend="whisper", difficulty_map=_MAP)
    summary = summarize_corpus(corpus)

    assert summary["segments"] == 3
    assert summary["backend"] == "whisper"
    # seg 0 = ok difficulty ; segs 1,2 = degrade difficulty
    assert summary["by_difficulty"]["ok"]["count"] == 1
    assert summary["by_difficulty"]["degrade"]["count"] == 2
    assert summary["by_difficulty"]["degrade"]["reliability"]["degrade"] == 1
    assert summary["by_difficulty"]["degrade"]["reliability"]["suspect"] == 1
    # moyennes sur les segments qui exposent la métrique
    assert round(summary["no_speech_prob_mean"], 3) == round((0.8 + 0.4) / 2, 3)


def test_summarize_corpus_empty():
    summary = summarize_corpus([])
    assert summary["segments"] == 0
    assert summary["by_difficulty"] == {}


class _FakeFs:
    def __init__(self, preflight):
        self._preflight = preflight
        self.saved: dict = {}

    def load_json(self, path):
        return self._preflight if path == "metadata/audio_preflight.json" else None

    def save_json(self, path, data):
        self.saved[path] = data


class _FakeLog:
    def info(self, *args, **kwargs):
        pass


def _transcriber(config):
    from transcria.stt.transcription import Transcriber

    t = Transcriber.__new__(Transcriber)  # bypass __init__ (pas de backend GPU)
    t.config = config
    return t


def test_write_stt_corpus_enabled_writes_file_and_returns_summary(monkeypatch):
    from transcria.jobs.store import JobStore

    promoted = {}
    monkeypatch.setattr(JobStore, "update_extra_data",
                        lambda job_id, updater: promoted.update(updater({})))

    t = _transcriber({"workflow": {"stt_corpus": {"enabled": True}}})
    fs = _FakeFs({"difficulty_map": _MAP})
    summary = t._write_stt_corpus(
        SimpleNamespace(id="job-1"),
        [_seg(0.0, 2.0, reliability="ok", reliability_reasons=[])],
        "cohere", fs, _FakeLog(),
    )
    assert "metadata/stt_corpus.json" in fs.saved
    assert len(fs.saved["metadata/stt_corpus.json"]) == 1
    assert summary["segments"] == 1
    # promotion cross-jobs effectuée
    assert promoted["stt_corpus_summary"]["segments"] == 1


def test_write_stt_corpus_disabled_is_noop():
    t = _transcriber({"workflow": {"stt_corpus": {"enabled": False}}})
    fs = _FakeFs({"difficulty_map": _MAP})
    summary = t._write_stt_corpus(SimpleNamespace(id="x"), [_seg(0.0, 2.0)], "cohere", fs, _FakeLog())
    assert summary is None
    assert fs.saved == {}
