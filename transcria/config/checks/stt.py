"""Schéma de config — STT : backends natifs et servis, sections par moteur, multi-STT/hybride."""
from __future__ import annotations

from transcria.config.checks.base import (  # noqa: F401
    ValidationResult,
    _check_bool,
    _check_int_range,
    _check_optional_number,
    _check_optional_positive_int,
    _check_port_value,
    _check_regex_list,
    _check_regex_string,
    _check_str,
    _check_time_string,
)


def _check_models(mod: dict, r: ValidationResult, cfg: dict | None = None) -> None:
    _check_stt_backend(mod, r, cfg)
    _check_summary_stt_backend(mod, r, cfg)
    _check_live_stt_backend(mod, r, cfg)
    _check_str(mod, "default_stt_model", "models.default_stt_model", r)
    _check_str(mod, "fallback_stt_model", "models.fallback_stt_model", r)
    _check_str(mod, "cohere_model_path", "models.cohere_model_path", r)
    cohere_revision = mod.get("cohere_model_revision")
    if cohere_revision is not None and not isinstance(cohere_revision, str):
        r.add_error("models.cohere_model_revision: doit être une chaîne ou null")
    _check_str(mod, "pyannote_model", "models.pyannote_model", r)

    stt_model = mod.get("stt_backend", "")
    cohere_path = mod.get("cohere_model_path", "")
    if isinstance(stt_model, str) and stt_model == "cohere" and not cohere_path:
        r.add_error(
            "models.cohere_model_path doit être renseigné quand le backend STT est 'cohere'"
        )


# Backends STT natifs acceptés. Liste LITTÉRALE à dessein : config/ est du noyau et
# n'importe pas le domaine stt (§8.2) — la cohérence avec stt/registry.py est verrouillée
# en CI par tests/contracts/test_stt_backend_contract.py (toute dérive casse la suite).

_VALID_STT_BACKENDS = frozenset({"cohere", "cohere_tf5", "whisper", "granite", "parakeet", "voxtral", "kroko", "moss"})

def _check_stt_backend(mod: dict, r: ValidationResult, cfg: dict | None = None) -> None:
    backend = mod.get("stt_backend", "cohere")
    if isinstance(backend, str) and backend in _VALID_STT_BACKENDS:
        return
    # Backend SERVI (runtimes C++, ex. qwen3asr/nemotron) : n'importe quel nom est
    # accepté s'il est ROUTÉ — url non vide dans inference.stt.backends.<nom>
    # (cf. docs/EXTERNAL_STT_RUNTIMES.md). Sans URL, l'erreur reste (le factory
    # retomberait silencieusement sur cohere — piège utilisateur).
    routed = (((cfg or {}).get("inference", {}) or {}).get("stt", {}) or {}).get("backends", {}) or {}
    if isinstance(backend, str) and str((routed.get(backend) or {}).get("url") or "").strip():
        return
    r.add_error(
        f"models.stt_backend='{backend}' invalide. "
        f"Valeurs acceptées: {', '.join(sorted(_VALID_STT_BACKENDS))} — ou un backend SERVI déclaré "
        f"avec une url dans inference.stt.backends.<nom> (runtimes audio.cpp/parakeet.cpp)"
    )

def _check_stt_served_pools(cfg: dict, r: ValidationResult) -> None:
    """Pools multi-instance (§2.9) : `inference.stt.backends.<nom>.extra_urls` doit
    être une liste d'URLs http(s) ; `resource_node.engines[].backend` doit référencer
    un backend servi déclaré (sinon l'instance ne serait jamais assurée)."""
    stt = ((cfg.get("inference", {}) or {}).get("stt", {}) or {})
    backends = stt.get("backends", {}) or {}
    for name, spec in backends.items():
        extra = (spec or {}).get("extra_urls")
        if extra is None:
            continue
        if not isinstance(extra, list) or not all(
            isinstance(u, str) and u.strip().startswith(("http://", "https://")) for u in extra
        ):
            r.add_error(
                f"inference.stt.backends.{name}.extra_urls doit être une liste d'URLs "
                f"http(s) (instances supplémentaires du même moteur)"
            )
    for entry in (cfg.get("resource_node", {}) or {}).get("engines", []) or []:
        declared = str((entry or {}).get("backend") or "").strip()
        if declared and declared not in backends:
            r.add_warning(
                f"resource_node.engines['{(entry or {}).get('name')}'].backend='{declared}' "
                f"ne correspond à aucun backend de inference.stt.backends — l'instance "
                f"ne sera jamais sollicitée par le pool client"
            )

def _check_summary_stt_backend(mod: dict, r: ValidationResult, cfg: dict | None = None) -> None:
    """`models.summary_stt_backend` : null (= backend principal) ou même règle que
    `stt_backend` (natif du registre, ou servi routé avec url)."""
    backend = mod.get("summary_stt_backend")
    if backend is None:
        return
    if isinstance(backend, str) and backend in _VALID_STT_BACKENDS:
        return
    routed = (((cfg or {}).get("inference", {}) or {}).get("stt", {}) or {}).get("backends", {}) or {}
    if isinstance(backend, str) and str((routed.get(backend) or {}).get("url") or "").strip():
        return
    r.add_error(
        f"models.summary_stt_backend='{backend}' invalide. "
        f"null (= backend principal), l'un de : {', '.join(sorted(_VALID_STT_BACKENDS))}, "
        f"ou un backend SERVI déclaré avec une url dans inference.stt.backends.<nom>"
    )

def _check_live_stt_backend(mod: dict, r: ValidationResult, cfg: dict | None = None) -> None:
    """`models.live_stt_backend` : null (= pas de chaîne live) ou même règle que
    `stt_backend` (natif du registre, ou servi routé avec url). Couture 3 du
    chantier temps réel (docs/TEMPS_REEL_REUNIONS.md)."""
    backend = mod.get("live_stt_backend")
    if backend is None:
        return
    if isinstance(backend, str) and backend in _VALID_STT_BACKENDS:
        return
    routed = (((cfg or {}).get("inference", {}) or {}).get("stt", {}) or {}).get("backends", {}) or {}
    if isinstance(backend, str) and str((routed.get(backend) or {}).get("url") or "").strip():
        return
    r.add_error(
        f"models.live_stt_backend='{backend}' invalide. "
        f"null (= pas de chaîne live), l'un de : {', '.join(sorted(_VALID_STT_BACKENDS))}, "
        f"ou un backend SERVI déclaré avec une url dans inference.stt.backends.<nom>"
    )

def _check_whisper(whisper: dict, r: ValidationResult) -> None:
    if not whisper:
        return
    if not isinstance(whisper, dict):
        r.add_error("whisper: doit être un objet YAML")
        return
    _check_str(whisper, "model_size", "whisper.model_size", r)
    _check_str(whisper, "compute_type", "whisper.compute_type", r)
    _check_int_range(whisper, "cpu_threads", "whisper.cpu_threads", 1, 128, r)
    _check_int_range(whisper, "chunk_length_s", "whisper.chunk_length_s", 1, 300, r)
    _check_int_range(whisper, "beam_size", "whisper.beam_size", 1, 32, r)
    _check_int_range(whisper, "best_of", "whisper.best_of", 1, 32, r)
    for key in (
        "vad_filter", "word_timestamps", "condition_on_previous_text",
        "suppress_numerals", "collapse_repetition_loops",
    ):
        _check_bool(whisper, key, f"whisper.{key}", r)
    _check_optional_number(whisper, "no_speech_threshold", "whisper.no_speech_threshold", r)
    _check_optional_number(whisper, "compression_ratio_threshold", "whisper.compression_ratio_threshold", r)
    _check_optional_number(whisper, "log_prob_threshold", "whisper.log_prob_threshold", r)
    _check_optional_number(whisper, "hallucination_silence_threshold", "whisper.hallucination_silence_threshold", r)
    _check_optional_number(whisper, "repetition_penalty", "whisper.repetition_penalty", r)
    _check_int_range(whisper, "no_repeat_ngram_size", "whisper.no_repeat_ngram_size", 0, 20, r)
    _check_int_range(whisper, "repetition_loop_min_repeats", "whisper.repetition_loop_min_repeats", 2, 100, r)
    _check_int_range(whisper, "repetition_loop_max_phrase_words", "whisper.repetition_loop_max_phrase_words", 1, 100, r)
    _check_int_range(whisper, "repetition_loop_keep_repeats", "whisper.repetition_loop_keep_repeats", 1, 20, r)
    for key in ("hotwords", "initial_prompt"):
        val = whisper.get(key)
        if val is not None and not isinstance(val, str):
            r.add_error(f"whisper.{key}: doit être une chaîne ou null")
    lexicon_hotwords = whisper.get("lexicon_hotwords", {})
    if lexicon_hotwords is not None:
        if not isinstance(lexicon_hotwords, dict):
            r.add_error("whisper.lexicon_hotwords: doit être un objet YAML")
        else:
            _check_bool(lexicon_hotwords, "enabled", "whisper.lexicon_hotwords.enabled", r)
            _check_int_range(lexicon_hotwords, "max_terms", "whisper.lexicon_hotwords.max_terms", 1, 500, r)
            _check_int_range(lexicon_hotwords, "max_chars", "whisper.lexicon_hotwords.max_chars", 40, 10000, r)
            _check_int_range(lexicon_hotwords, "max_tokens", "whisper.lexicon_hotwords.max_tokens", 1, 224, r)
            _check_str(lexicon_hotwords, "prefix", "whisper.lexicon_hotwords.prefix", r)
            _check_str(lexicon_hotwords, "tokenizer_model", "whisper.lexicon_hotwords.tokenizer_model", r)
            priorities = lexicon_hotwords.get("priorities", [])
            if not isinstance(priorities, list) or not priorities:
                r.add_error("whisper.lexicon_hotwords.priorities: doit être une liste non vide")
            else:
                allowed = {"critique", "importante", "normale"}
                for index, priority in enumerate(priorities):
                    if priority not in allowed:
                        r.add_error(
                            f"whisper.lexicon_hotwords.priorities[{index}]: priorité invalide '{priority}'"
                        )
    forced = whisper.get("forced_alignment", {})
    if forced is not None:
        if not isinstance(forced, dict):
            r.add_error("whisper.forced_alignment: doit être un objet YAML")
        else:
            _check_bool(forced, "enabled", "whisper.forced_alignment.enabled", r)
            backend = forced.get("backend", "torchaudio_ctc")
            if backend != "torchaudio_ctc":
                r.add_error("whisper.forced_alignment.backend: doit valoir torchaudio_ctc")
            for key in ("bundle_name",):
                val = forced.get(key)
                if val is not None and not isinstance(val, str):
                    r.add_error(f"whisper.forced_alignment.{key}: doit être une chaîne ou null")
            _check_optional_number(forced, "max_segment_s", "whisper.forced_alignment.max_segment_s", r)

def _check_granite(granite: dict, r: ValidationResult) -> None:
    if not granite:
        return
    if not isinstance(granite, dict):
        r.add_error("granite: doit être un objet YAML")
        return
    _check_bool(granite, "enabled", "granite.enabled", r)
    _check_str(granite, "model_id", "granite.model_id", r)
    _check_str(granite, "torch_dtype", "granite.torch_dtype", r)
    dtype = granite.get("torch_dtype")
    if isinstance(dtype, str) and dtype not in {"bfloat16", "bf16", "float16", "fp16", "float32", "fp32"}:
        r.add_error("granite.torch_dtype: valeurs acceptées bfloat16, float16, float32")
    _check_int_range(granite, "chunk_length_s", "granite.chunk_length_s", 1, 600, r)
    _check_int_range(granite, "max_new_tokens", "granite.max_new_tokens", 1, 20000, r)
    _check_optional_number(granite, "max_new_tokens_per_second", "granite.max_new_tokens_per_second", r)
    _check_int_range(granite, "min_new_tokens", "granite.min_new_tokens", 1, 20000, r)
    max_new_tokens_per_second = granite.get("max_new_tokens_per_second")
    if (
        max_new_tokens_per_second is not None
        and not isinstance(max_new_tokens_per_second, bool)
        and isinstance(max_new_tokens_per_second, (int, float))
        and max_new_tokens_per_second <= 0
    ):
        r.add_error("granite.max_new_tokens_per_second: doit être strictement positif ou null")
    if (
        isinstance(granite.get("min_new_tokens"), int)
        and isinstance(granite.get("max_new_tokens"), int)
        and granite["min_new_tokens"] > granite["max_new_tokens"]
    ):
        r.add_error("granite.min_new_tokens: doit être inférieur ou égal à granite.max_new_tokens")
    _check_str(granite, "prompt_mode", "granite.prompt_mode", r)
    prompt_mode = granite.get("prompt_mode")
    if isinstance(prompt_mode, str) and prompt_mode not in {"asr_raw", "asr_punctuated", "keywords"}:
        r.add_error("granite.prompt_mode: valeurs acceptées asr_raw, asr_punctuated, keywords")
    for key in ("prompt_asr_raw", "prompt_asr_punctuated", "prompt_keywords"):
        _check_str(granite, key, f"granite.{key}", r)
    keywords = granite.get("keywords", [])
    if isinstance(keywords, str):
        pass
    elif isinstance(keywords, list):
        for index, keyword in enumerate(keywords):
            if not isinstance(keyword, str) or not keyword.strip():
                r.add_error(f"granite.keywords[{index}]: doit être une chaîne non vide")
    else:
        r.add_error("granite.keywords: doit être une chaîne ou une liste de chaînes")
    lexicon_keywords = granite.get("lexicon_keywords", {})
    if lexicon_keywords is not None:
        if not isinstance(lexicon_keywords, dict):
            r.add_error("granite.lexicon_keywords: doit être un objet YAML")
        else:
            _check_bool(lexicon_keywords, "enabled", "granite.lexicon_keywords.enabled", r)
            _check_int_range(lexicon_keywords, "max_terms", "granite.lexicon_keywords.max_terms", 1, 2000, r)
            priorities = lexicon_keywords.get("priorities", [])
            if not isinstance(priorities, list) or not priorities:
                r.add_error("granite.lexicon_keywords.priorities: doit être une liste non vide")
            else:
                allowed = {"critique", "importante", "normale"}
                for index, priority in enumerate(priorities):
                    if not isinstance(priority, str) or priority not in allowed:
                        r.add_error(
                            f"granite.lexicon_keywords.priorities[{index}]: valeurs acceptées critique, importante, normale"
                        )
    for key in ("fix_mistral_regex", "collapse_repetition_loops"):
        _check_bool(granite, key, f"granite.{key}", r)
    _check_int_range(granite, "repetition_loop_min_repeats", "granite.repetition_loop_min_repeats", 2, 100, r)
    _check_int_range(granite, "repetition_loop_max_phrase_words", "granite.repetition_loop_max_phrase_words", 1, 100, r)
    _check_int_range(granite, "repetition_loop_keep_repeats", "granite.repetition_loop_keep_repeats", 1, 20, r)

def _check_cohere(cohere: dict, r: ValidationResult) -> None:
    if not cohere:
        return
    if not isinstance(cohere, dict):
        r.add_error("cohere: doit être un objet YAML")
        return
    _check_optional_number(cohere, "chunk_length_s", "cohere.chunk_length_s", r)
    _check_int_range(cohere, "max_new_tokens", "cohere.max_new_tokens", 1, 4096, r)
    _check_bool(cohere, "punctuation", "cohere.punctuation", r)
    _check_optional_number(cohere, "repetition_penalty", "cohere.repetition_penalty", r)
    _check_int_range(cohere, "no_repeat_ngram_size", "cohere.no_repeat_ngram_size", 0, 20, r)
    _check_bool(cohere, "collapse_repetition_loops", "cohere.collapse_repetition_loops", r)
    _check_int_range(cohere, "repetition_loop_min_repeats", "cohere.repetition_loop_min_repeats", 2, 100, r)
    _check_int_range(cohere, "repetition_loop_max_phrase_words", "cohere.repetition_loop_max_phrase_words", 1, 100, r)
    _check_int_range(cohere, "repetition_loop_keep_repeats", "cohere.repetition_loop_keep_repeats", 1, 20, r)
    lexicon_biasing = cohere.get("lexicon_biasing", {})
    if lexicon_biasing is not None:
        if not isinstance(lexicon_biasing, dict):
            r.add_error("cohere.lexicon_biasing: doit être un objet YAML")
        else:
            _check_bool(lexicon_biasing, "enabled", "cohere.lexicon_biasing.enabled", r)
            _check_int_range(lexicon_biasing, "max_terms", "cohere.lexicon_biasing.max_terms", 1, 2000, r)
            _check_optional_number(lexicon_biasing, "boost", "cohere.lexicon_biasing.boost", r)
            boost = lexicon_biasing.get("boost")
            if isinstance(boost, (int, float)) and not isinstance(boost, bool) and (boost < 0 or boost > 2):
                r.add_error("cohere.lexicon_biasing.boost: doit être entre 0 et 2")
            _check_optional_number(lexicon_biasing, "start_boost", "cohere.lexicon_biasing.start_boost", r)
            start_boost = lexicon_biasing.get("start_boost")
            if (
                isinstance(start_boost, (int, float))
                and not isinstance(start_boost, bool)
                and (start_boost < 0 or start_boost > 1)
            ):
                r.add_error("cohere.lexicon_biasing.start_boost: doit être entre 0 et 1")
            _check_int_range(lexicon_biasing, "max_prefix_tokens", "cohere.lexicon_biasing.max_prefix_tokens", 1, 100, r)
            priorities = lexicon_biasing.get("priorities", [])
            if not isinstance(priorities, list) or not priorities:
                r.add_error("cohere.lexicon_biasing.priorities: doit être une liste non vide")
            else:
                allowed = {"critique", "importante", "normale"}
                for index, priority in enumerate(priorities):
                    if priority not in allowed:
                        r.add_error(
                            f"cohere.lexicon_biasing.priorities[{index}]: priorité invalide '{priority}'"
                        )

def _check_cohere_tf5(cfg: dict, r: ValidationResult) -> None:
    if not cfg:
        return
    if not isinstance(cfg, dict):
        r.add_error("cohere_tf5: doit être un objet YAML")
        return
    _check_bool(cfg, "enabled", "cohere_tf5.enabled", r)
    for key in ("tf5_site", "model_path"):
        _check_str(cfg, key, f"cohere_tf5.{key}", r)
    revision = cfg.get("model_revision")
    if revision is not None and not isinstance(revision, str):
        r.add_error("cohere_tf5.model_revision: doit être une chaîne ou null")
    _check_int_range(cfg, "timeout_s", "cohere_tf5.timeout_s", 1, 86400, r)
    _check_optional_number(cfg, "chunk_length_s", "cohere_tf5.chunk_length_s", r)
    _check_int_range(cfg, "max_new_tokens", "cohere_tf5.max_new_tokens", 1, 4096, r)
    _check_bool(cfg, "punctuation", "cohere_tf5.punctuation", r)
    _check_int_range(cfg, "batch_size", "cohere_tf5.batch_size", 1, 512, r)
    _check_optional_number(cfg, "repetition_penalty", "cohere_tf5.repetition_penalty", r)
    _check_int_range(cfg, "no_repeat_ngram_size", "cohere_tf5.no_repeat_ngram_size", 0, 20, r)
    _check_bool(cfg, "collapse_repetition_loops", "cohere_tf5.collapse_repetition_loops", r)
    _check_int_range(cfg, "repetition_loop_min_repeats", "cohere_tf5.repetition_loop_min_repeats", 2, 100, r)
    _check_int_range(cfg, "repetition_loop_max_phrase_words", "cohere_tf5.repetition_loop_max_phrase_words", 1, 100, r)
    _check_int_range(cfg, "repetition_loop_keep_repeats", "cohere_tf5.repetition_loop_keep_repeats", 1, 20, r)

def _check_voxtral(voxtral: dict, r: ValidationResult) -> None:
    if not voxtral:
        return
    if not isinstance(voxtral, dict):
        r.add_error("voxtral: doit être un objet YAML")
        return
    _check_bool(voxtral, "enabled", "voxtral.enabled", r)
    _check_str(voxtral, "model_id", "voxtral.model_id", r)
    _check_str(voxtral, "torch_dtype", "voxtral.torch_dtype", r)
    dtype = voxtral.get("torch_dtype")
    if isinstance(dtype, str) and dtype not in {"bfloat16", "bf16", "float16", "fp16", "float32", "fp32"}:
        r.add_error("voxtral.torch_dtype: valeurs acceptées bfloat16, float16, float32")
    _check_int_range(voxtral, "chunk_length_s", "voxtral.chunk_length_s", 1, 600, r)
    _check_int_range(voxtral, "max_new_tokens", "voxtral.max_new_tokens", 1, 20000, r)
    _check_optional_number(voxtral, "max_new_tokens_per_second", "voxtral.max_new_tokens_per_second", r)
    _check_int_range(voxtral, "min_new_tokens", "voxtral.min_new_tokens", 1, 20000, r)
    max_new_tokens_per_second = voxtral.get("max_new_tokens_per_second")
    if (
        max_new_tokens_per_second is not None
        and not isinstance(max_new_tokens_per_second, bool)
        and isinstance(max_new_tokens_per_second, (int, float))
        and max_new_tokens_per_second <= 0
    ):
        r.add_error("voxtral.max_new_tokens_per_second: doit être strictement positif ou null")
    _check_bool(voxtral, "collapse_repetition_loops", "voxtral.collapse_repetition_loops", r)
    _check_int_range(voxtral, "repetition_loop_min_repeats", "voxtral.repetition_loop_min_repeats", 2, 100, r)
    _check_int_range(voxtral, "repetition_loop_max_phrase_words", "voxtral.repetition_loop_max_phrase_words", 1, 100, r)
    _check_int_range(voxtral, "repetition_loop_keep_repeats", "voxtral.repetition_loop_keep_repeats", 1, 20, r)

def _check_moss(moss: dict, r: ValidationResult) -> None:
    if not moss:
        return
    if not isinstance(moss, dict):
        r.add_error("moss: doit être un objet YAML")
        return
    _check_bool(moss, "enabled", "moss.enabled", r)
    if moss.get("enabled") and str(moss.get("moss_site") or "").startswith("/tmp"):
        r.add_warning(
            "moss.moss_site pointe sous /tmp (purgé au reboot) — le backend moss "
            "disparaîtra au redémarrage ; déplacer vers ./runtimes/moss_site et "
            "relancer `installer.cli moss-site --dir ./runtimes/moss_site` au besoin"
        )
    _check_str(moss, "model_path", "moss.model_path", r)
    _check_str(moss, "moss_site", "moss.moss_site", r)
    _check_int_range(moss, "timeout_s", "moss.timeout_s", 60, 86400, r)
    _check_int_range(moss, "max_new_tokens", "moss.max_new_tokens", 256, 65536, r)
    _check_int_range(moss, "single_pass_max_s", "moss.single_pass_max_s", 60, 7200, r)
    _check_optional_number(moss, "gap_alert_s", "moss.gap_alert_s", r)
    _check_bool(moss, "collapse_repetition_loops", "moss.collapse_repetition_loops", r)
    _check_int_range(moss, "repetition_loop_min_repeats", "moss.repetition_loop_min_repeats", 2, 100, r)
    _check_int_range(moss, "repetition_loop_max_phrase_words", "moss.repetition_loop_max_phrase_words", 1, 100, r)
    _check_int_range(moss, "repetition_loop_keep_repeats", "moss.repetition_loop_keep_repeats", 1, 20, r)

def _check_kroko(kroko: dict, r: ValidationResult) -> None:
    if not kroko:
        return
    if not isinstance(kroko, dict):
        r.add_error("kroko: doit être un objet YAML")
        return
    _check_bool(kroko, "enabled", "kroko.enabled", r)
    _check_str(kroko, "model_dir", "kroko.model_dir", r)
    _check_str(kroko, "repo_id", "kroko.repo_id", r)
    variant = kroko.get("variant")
    if variant is not None and str(variant) not in {"64", "128"}:
        r.add_error("kroko.variant: valeurs acceptées 64, 128")
    _check_int_range(kroko, "num_threads", "kroko.num_threads", 1, 128, r)
    method = kroko.get("decoding_method")
    if isinstance(method, str) and method not in {"greedy_search", "modified_beam_search"}:
        r.add_error("kroko.decoding_method: valeurs acceptées greedy_search, modified_beam_search")
    for key in ("tail_padding_s", "segment_max_gap_s", "segment_max_len_s"):
        _check_optional_number(kroko, key, f"kroko.{key}", r)
    _check_bool(kroko, "collapse_repetition_loops", "kroko.collapse_repetition_loops", r)
    _check_int_range(kroko, "repetition_loop_min_repeats", "kroko.repetition_loop_min_repeats", 2, 100, r)
    _check_int_range(kroko, "repetition_loop_max_phrase_words", "kroko.repetition_loop_max_phrase_words", 1, 100, r)
    _check_int_range(kroko, "repetition_loop_keep_repeats", "kroko.repetition_loop_keep_repeats", 1, 20, r)

def _check_multi_stt(cfg: dict, r: ValidationResult) -> None:
    if not cfg:
        return
    if not isinstance(cfg, dict):
        r.add_error("workflow.multi_stt: doit être un objet YAML")
        return
    _check_bool(cfg, "enabled", "workflow.multi_stt.enabled", r)
    _check_str(cfg, "secondary_backend", "workflow.multi_stt.secondary_backend", r)
    secondary = cfg.get("secondary_backend")
    if isinstance(secondary, str) and secondary not in {
        "cohere", "cohere_tf5", "whisper", "granite", "parakeet", "voxtral", "kroko"
    }:
        r.add_error(
            "workflow.multi_stt.secondary_backend: valeurs acceptées cohere, cohere_tf5, whisper, granite, parakeet, voxtral, kroko"
        )
    _check_int_range(cfg, "max_segments", "workflow.multi_stt.max_segments", 1, 500, r)
    for key in ("min_segment_s", "padding_s"):
        _check_optional_number(cfg, key, f"workflow.multi_stt.{key}", r)
    levels = cfg.get("levels", [])
    if not isinstance(levels, list) or not levels:
        r.add_error("workflow.multi_stt.levels: doit être une liste non vide")
    else:
        for i, value in enumerate(levels):
            if value not in {"suspect", "degrade"}:
                r.add_error(f"workflow.multi_stt.levels[{i}]: doit valoir suspect ou degrade")

def _check_stt_hybrid(cfg: dict, r: ValidationResult) -> None:
    if not cfg:
        return
    if not isinstance(cfg, dict):
        r.add_error("workflow.stt_hybrid: doit être un objet YAML")
        return
    for key in ("enabled", "llm_arbitration_enabled", "write_audit_artifacts"):
        _check_bool(cfg, key, f"workflow.stt_hybrid.{key}", r)
    if cfg.get("enabled") is True:
        r.add_error("workflow.stt_hybrid.enabled: mode non encore intégré au pipeline, doit rester false")
    for key in ("primary_backend", "fallback_backend"):
        _check_str(cfg, key, f"workflow.stt_hybrid.{key}", r)
    primary = str(cfg.get("primary_backend") or "")
    fallback = str(cfg.get("fallback_backend") or "")
    if primary and fallback and primary == fallback:
        r.add_error("workflow.stt_hybrid: primary_backend et fallback_backend doivent être différents")
    for key in ("decision_margin", "window_s"):
        _check_optional_number(cfg, key, f"workflow.stt_hybrid.{key}", r)
    for key in ("fallback_on_reliability", "review_on_reliability"):
        values = cfg.get(key, [])
        if not isinstance(values, list):
            r.add_error(f"workflow.stt_hybrid.{key}: doit être une liste")
            continue
        for i, value in enumerate(values):
            if value not in {"ok", "suspect", "degrade"}:
                r.add_error(f"workflow.stt_hybrid.{key}[{i}]: doit valoir ok, suspect ou degrade")

def _check_quality_transcription(cfg: dict, r: ValidationResult) -> None:
    if cfg is None:
        return
    if not isinstance(cfg, dict):
        r.add_error("workflow.quality_transcription: doit être un objet YAML")
        return
    backend = cfg.get("force_stt_backend")
    if backend is not None and backend not in {"cohere", "cohere_tf5", "whisper", "granite", "parakeet"}:
        r.add_error("workflow.quality_transcription.force_stt_backend: doit valoir cohere, cohere_tf5, whisper, granite ou parakeet")
    _check_bool(cfg, "force_on_degraded_summary", "workflow.quality_transcription.force_on_degraded_summary", r)
    modes = cfg.get("enabled_for_modes", [])
    if not isinstance(modes, list):
        r.add_error("workflow.quality_transcription.enabled_for_modes: doit être une liste")
        return
    for mode in modes:
        if mode not in {"fast", "quality"}:
            r.add_error("workflow.quality_transcription.enabled_for_modes: valeurs acceptées fast, quality")
    degraded_levels = cfg.get("degraded_summary_levels", [])
    if not isinstance(degraded_levels, list):
        r.add_error("workflow.quality_transcription.degraded_summary_levels: doit être une liste")
        return
    for level in degraded_levels:
        if not isinstance(level, str) or not level.strip():
            r.add_error("workflow.quality_transcription.degraded_summary_levels: valeurs chaîne non vides attendues")
