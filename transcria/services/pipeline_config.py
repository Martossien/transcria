"""Config effective d'un traitement — mode, garde qualité, lexique (vague B2, lot 2).

Corps extraits de ``PipelineService`` : dérivation de la config effective par
mode (backend qualité forcé, exclusion Granite sur audio dégradé) et injection
du lexique de session dans les backends STT qui savent l'exploiter (hotwords
Whisper, biasing Cohere, keywords Granite). Fonctions pures de (config, job) —
aucune dépendance au service.
"""
import logging
from copy import deepcopy

from transcria.config.views import QualityTranscriptionView, SttView
from transcria.jobs.models import Job

logger = logging.getLogger(__name__)


def config_for_mode(source_config: dict, mode: str, job: Job | None = None) -> dict:
    cfg = deepcopy(source_config)
    quality_view = QualityTranscriptionView.from_config(cfg)
    if quality_view.force_stt_backend and (
        mode in quality_view.enabled_for_modes
        or should_force_quality_backend_for_degraded_summary(job, cfg)
    ):
        cfg.setdefault("models", {})["stt_backend"] = quality_view.force_stt_backend
    # Vue reconstruite APRÈS la mutation éventuelle du backend forcé.
    backend = SttView.from_config(cfg).stt_backend
    if backend == "granite" and job is not None:
        from transcria.jobs.filesystem import JobFilesystem
        fs = JobFilesystem(cfg.get("storage", {}).get("jobs_dir", "./jobs"), job.id)
        quality = fs.load_json("metadata/audio_quality_decision.json") or {}
        preflight = fs.load_json("metadata/audio_preflight.json") or {}
        if quality.get("level") == "degrade" or "audio_tres_faible" in (preflight.get("flags") or []):
            # Granite est expérimental et peu fiable sur audio dégradé ;
            # on revient au backend de production configuré dans la config source.
            fallback = SttView.from_config(source_config).stt_backend
            if fallback == "granite":
                fallback = "cohere"
            logger.info(
                "Granite exclu pour audio dégradé (job=%s), fallback → %s", job.id, fallback
            )
            cfg["models"]["stt_backend"] = fallback
    inject_whisper_lexicon_hotwords(cfg, job)
    inject_cohere_lexicon_biasing(cfg, job)
    inject_granite_lexicon_keywords(cfg, job)
    return cfg


def inject_whisper_lexicon_hotwords(cfg: dict, job: Job | None) -> None:
    backend = SttView.from_config(cfg).stt_backend
    if backend != "whisper" or job is None:
        return

    whisper_cfg = cfg.setdefault("whisper", {})
    hotwords_cfg = whisper_cfg.get("lexicon_hotwords", {})
    if not isinstance(hotwords_cfg, dict) or not hotwords_cfg.get("enabled", False):
        return

    try:
        from transcria.jobs.filesystem import JobFilesystem
        from transcria.stt.lexicon_hotwords import build_whisper_hotwords

        fs = JobFilesystem(cfg.get("storage", {}).get("jobs_dir", "./jobs"), job.id)
        lexicon = fs.load_json("context/session_lexicon.json") or []
        if not isinstance(lexicon, list):
            logger.warning("Hotwords Whisper lexique ignorés: format lexique inattendu job=%s", job.id)
            return

        hotwords, stats = build_whisper_hotwords(
            lexicon,
            enabled=True,
            priorities=hotwords_cfg.get("priorities"),
            max_terms=hotwords_cfg.get("max_terms", 50),
            max_chars=hotwords_cfg.get("max_chars", 900),
            max_tokens=hotwords_cfg.get("max_tokens", 200),
            prefix=hotwords_cfg.get("prefix", "Termes importants :"),
            existing_hotwords=whisper_cfg.get("hotwords"),
            tokenizer_model=hotwords_cfg.get("tokenizer_model") or "openai/whisper-large-v3",
        )
        whisper_cfg["hotwords"] = hotwords
        fs.save_json("metadata/whisper_hotwords.json", stats)
        logger.info(
            "Hotwords Whisper depuis lexique: job=%s candidats=%d injectés=%d exclus=%d tokens=%s/%s méthode=%s priorités=%s",
            job.id,
            stats.get("candidate_terms", 0),
            stats.get("injected_terms", 0),
            stats.get("excluded_terms", 0),
            stats.get("token_count", 0),
            stats.get("max_tokens", 0),
            stats.get("token_count_method", "none"),
            ",".join(stats.get("priorities", [])),
        )
    except Exception as exc:
        logger.warning("Hotwords Whisper depuis lexique indisponibles: job=%s error=%s", job.id, exc)


def inject_cohere_lexicon_biasing(cfg: dict, job: Job | None) -> None:
    backend = SttView.from_config(cfg).stt_backend
    if backend != "cohere" or job is None:
        return

    cohere_cfg = cfg.setdefault("cohere", {})
    biasing_cfg = cohere_cfg.get("lexicon_biasing", {})
    if not isinstance(biasing_cfg, dict) or not biasing_cfg.get("enabled", False):
        return

    try:
        from transcria.jobs.filesystem import JobFilesystem
        from transcria.stt.contextual_biasing import select_lexicon_bias_terms

        fs = JobFilesystem(cfg.get("storage", {}).get("jobs_dir", "./jobs"), job.id)
        lexicon = fs.load_json("context/session_lexicon.json") or []
        if not isinstance(lexicon, list):
            logger.warning("Biasing Cohere lexique ignoré: format lexique inattendu job=%s", job.id)
            return

        terms, stats = select_lexicon_bias_terms(
            lexicon,
            enabled=True,
            priorities=biasing_cfg.get("priorities"),
            max_terms=biasing_cfg.get("max_terms", 300),
        )
        stats["boost"] = biasing_cfg.get("boost", 0.2)
        stats["start_boost"] = biasing_cfg.get("start_boost", 0.05)
        stats["max_prefix_tokens"] = biasing_cfg.get("max_prefix_tokens", 20)
        cohere_cfg["_lexicon_bias_terms"] = terms
        fs.save_json("metadata/cohere_lexicon_biasing.json", stats)
        logger.info(
            "Biasing Cohere depuis lexique: job=%s candidats=%d injectés=%d exclus=%d priorités=%s",
            job.id,
            stats.get("candidate_terms", 0),
            stats.get("injected_terms", 0),
            stats.get("excluded_terms", 0),
            ",".join(stats.get("priorities", [])),
        )
    except Exception as exc:
        logger.warning("Biasing Cohere depuis lexique indisponible: job=%s error=%s", job.id, exc)


def inject_granite_lexicon_keywords(cfg: dict, job: Job | None) -> None:
    """Injecte le lexique de session dans le prompt Granite « Keywords: ».

    C'est le mécanisme de biasing officiel IBM (noms propres, acronymes,
    jargon). Miroir de `inject_cohere_lexicon_biasing` : seules les formes
    cibles validées sont poussées.
    """
    backend = SttView.from_config(cfg).stt_backend
    if backend != "granite" or job is None:
        return

    granite_cfg = cfg.setdefault("granite", {})
    keywords_cfg = granite_cfg.get("lexicon_keywords", {})
    if not isinstance(keywords_cfg, dict) or not keywords_cfg.get("enabled", False):
        return

    try:
        from transcria.jobs.filesystem import JobFilesystem
        from transcria.stt.contextual_biasing import select_lexicon_bias_terms

        fs = JobFilesystem(cfg.get("storage", {}).get("jobs_dir", "./jobs"), job.id)
        lexicon = fs.load_json("context/session_lexicon.json") or []
        if not isinstance(lexicon, list):
            logger.warning("Keywords Granite lexique ignorés: format lexique inattendu job=%s", job.id)
            return

        terms, stats = select_lexicon_bias_terms(
            lexicon,
            enabled=True,
            priorities=keywords_cfg.get("priorities"),
            max_terms=keywords_cfg.get("max_terms", 50),
        )
        if not terms:
            logger.info("Keywords Granite depuis lexique: job=%s aucun terme retenu", job.id)
            return
        granite_cfg["keywords"] = terms
        granite_cfg["prompt_mode"] = "keywords"
        stats["prompt_mode"] = "keywords"
        fs.save_json("metadata/granite_keywords.json", stats)
        logger.info(
            "Keywords Granite depuis lexique: job=%s candidats=%d injectés=%d exclus=%d priorités=%s",
            job.id,
            stats.get("candidate_terms", 0),
            stats.get("injected_terms", 0),
            stats.get("excluded_terms", 0),
            ",".join(stats.get("priorities", [])),
        )
    except Exception as exc:
        logger.warning("Keywords Granite depuis lexique indisponibles: job=%s error=%s", job.id, exc)


def should_force_quality_backend_for_degraded_summary(job: Job | None, cfg: dict) -> bool:
    if job is None:
        return False

    quality_view = QualityTranscriptionView.from_config(cfg)
    if not quality_view.force_on_degraded_summary:
        return False

    # La vue normalise déjà (strip + non vide) — même sémantique qu'historiquement.
    degraded_levels = set(quality_view.degraded_summary_levels)
    if not degraded_levels:
        return False

    try:
        from transcria.jobs.filesystem import JobFilesystem
        from transcria.quality.audio_quality import AudioQualityEvaluator

        fs = JobFilesystem(cfg.get("storage", {}).get("jobs_dir", "./jobs"), job.id)
        summary = fs.load_json("summary/summary.json") or {}
        audio_analysis = fs.load_json("metadata/audio_analysis.json") or {}
        preflight = fs.load_json("metadata/audio_preflight.json") or {}
        evaluation = AudioQualityEvaluator(cfg).evaluate(audio_analysis, summary, preflight=preflight)
        fs.save_json("metadata/audio_quality_decision.json", evaluation)
        level = str((summary.get("diagnostics") or {}).get("level", "")).strip()
        if level in degraded_levels or evaluation.get("force_quality_backend"):
            logger.info(
                "[pipeline] Qualité audio '%s' (%s): backend STT forcé par configuration",
                evaluation.get("level"),
                ", ".join(evaluation.get("reasons", [])),
            )
            return True
    except Exception as exc:
        logger.warning("[pipeline] Diagnostic résumé indisponible: %s", exc)
    return False
