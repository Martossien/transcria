"""Validateurs _check_* de config_schema — chaque branche d'erreur vérifiée (C3, §3.13).

Patron table-driven : une config PAR DÉFAUT (qui valide proprement), UNE mutation
invalide, et l'erreur attendue — avec son chemin — doit apparaître. Chaque validateur
non testé est un message d'erreur de config jamais vérifié, donc potentiellement faux
le jour où l'utilisateur le voit. On passe par validate_config (l'entrée publique),
pas par les _check_* directement.
"""
# ruff: noqa: E501 — la table (chemin, valeur, message attendu) est en littéraux d'une ligne, à dessein.
from copy import deepcopy

import pytest

from transcria.config.config_schema import validate_config
from transcria.config.loader import get_default_config


def _set(cfg: dict, dotted: str, value) -> None:
    keys = dotted.split(".")
    node = cfg
    for key in keys[:-1]:
        nxt = node.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            node[key] = nxt
        node = nxt
    node[keys[-1]] = value


def _validate_mutated(*mutations: tuple[str, object]):
    cfg = deepcopy(get_default_config())
    for dotted, value in mutations:
        _set(cfg, dotted, value)
    return validate_config(cfg)


def _assert_error(result, fragment: str) -> None:
    assert any(fragment in msg for msg in result.errors), (
        f"erreur contenant {fragment!r} attendue, reçu : {result.errors}"
    )


# ---------------------------------------------------------------------------
# Table : (chemin muté, valeur invalide, fragment du message d'erreur attendu)
# ---------------------------------------------------------------------------

CASES = [
    # i18n
    ("i18n", "bad", "i18n: doit être un objet YAML"),
    ("i18n.available_locales", "fr", "i18n.available_locales: doit être une liste"),
    ("i18n.default_locale", 42, "i18n.default_locale: doit être une chaîne"),
    ("i18n.default_locale", "de", "absent de i18n.available_locales"),
    # maintenance
    ("maintenance", "bad", "maintenance: doit être un objet YAML"),
    ("maintenance.schedule", "bad", "maintenance.schedule: doit être un objet YAML"),
    ("maintenance.schedule.enabled", "oui", "maintenance.schedule.enabled: doit être true/false"),
    ("maintenance.schedule.keep", -1, "maintenance.schedule.keep"),
    ("maintenance.backup_dir", 42, "maintenance.backup_dir: doit être une chaîne"),
    # server / helpers de type
    ("server.host", 42, "server.host: doit être une chaîne (reçu int)"),
    ("server.host", "  ", "server.host: chaîne vide"),
    ("server.port", True, "server.port: doit être un nombre entier, pas un booléen"),
    ("server.port", "80", "server.port: doit être un nombre (reçu str)"),
    ("server.port", 0, "server.port=0: doit être entre 1 et 65535"),
    ("server.debug", "yes", "server.debug: doit être true/false"),
    # storage
    ("storage.agent_work_dir", 42, "storage.agent_work_dir: doit être une chaîne"),
    ("storage.shared_backend", "s3", "storage.shared_backend: doit être 'fs' ou 'pg'"),
    ("storage.shared_backend", "pg", "'pg' requiert une base PostgreSQL"),
    # voice_enrollment
    ("voice_enrollment", "bad", "voice_enrollment: doit être un objet YAML"),
    ("voice_enrollment.embedding", "bad", "voice_enrollment.embedding: doit être un objet YAML"),
    ("voice_enrollment.embedding.backend", "resemblyzer", "voice_enrollment.embedding.backend: doit valoir pyannote"),
    ("voice_enrollment.embedding.model_revision", 42, "voice_enrollment.embedding.model_revision: doit être une chaîne ou null"),
    ("voice_enrollment.embedding.expected_dim", 0, "voice_enrollment.embedding.expected_dim"),
    ("voice_enrollment.embedding.normalization", "none", "voice_enrollment.embedding.normalization: doit valoir l2"),
    ("voice_enrollment.matching", "bad", "voice_enrollment.matching: doit être un objet YAML"),
    ("voice_enrollment.matching.suggestion_threshold", 0.99, "suggestion_threshold doit être <= high_confidence_threshold"),
    ("voice_enrollment.consent", "bad", "voice_enrollment.consent: doit être un objet YAML"),
    ("voice_enrollment.consent.validity_days", 0, "voice_enrollment.consent.validity_days"),
    ("voice_enrollment.consent.proof_allowed_extensions", [], "proof_allowed_extensions: doit être une liste non vide"),
    ("voice_enrollment.consent.proof_allowed_extensions", ["  "], "proof_allowed_extensions[0]: doit être une chaîne non vide"),
    ("voice_enrollment.audit", "bad", "voice_enrollment.audit: doit être un objet YAML"),
    ("voice_enrollment.audit.log_match_scores", "x", "voice_enrollment.audit.log_match_scores: doit être true/false"),
    # gpu
    ("gpu.llm_gpu_indices", [], "gpu.llm_gpu_indices: doit être une liste non vide d'index GPU"),
    ("gpu.llm_gpu_indices", [0, 0], "gpu.llm_gpu_indices: index GPU dupliqués"),
    ("gpu.llm_vram_mb_per_gpu", [], "gpu.llm_vram_mb_per_gpu: doit être une liste non vide de Mo"),
    # services
    ("services.llm_cleanup_ports", "8000", "services.llm_cleanup_ports: doit être une liste de ports"),
    ("services.llm_cleanup_ports", [True], "services.llm_cleanup_ports[0]: doit être un port numérique"),
    ("services.llm_cleanup_ports", [70000], "services.llm_cleanup_ports[0]=70000: doit être entre 1 et 65535"),
    ("services.arbitrage_script", "", "services.arbitrage_script: chemin de script non défini"),
    ("services.stop_script", None, "services.stop_script: chemin de script non défini"),
    # models
    ("models.cohere_model_revision", 42, "models.cohere_model_revision: doit être une chaîne ou null"),
    ("models.stt_backend", "fake9", "models.stt_backend='fake9' invalide"),
    # workflow.progress
    ("workflow.progress", "bad", "workflow.progress: doit être un objet YAML"),
    ("workflow.progress.update_interval_s", "x", "workflow.progress.update_interval_s: doit être un nombre ou null"),
    # whisper
    ("whisper", "bad", "whisper: doit être un objet YAML"),
    ("whisper.hotwords", 42, "whisper.hotwords: doit être une chaîne ou null"),
    ("whisper.lexicon_hotwords", "bad", "whisper.lexicon_hotwords: doit être un objet YAML"),
    ("whisper.lexicon_hotwords.priorities", [], "whisper.lexicon_hotwords.priorities: doit être une liste non vide"),
    ("whisper.lexicon_hotwords.priorities", ["haute"], "whisper.lexicon_hotwords.priorities[0]: priorité invalide 'haute'"),
    ("whisper.forced_alignment", "bad", "whisper.forced_alignment: doit être un objet YAML"),
    ("whisper.forced_alignment.backend", "mfa", "whisper.forced_alignment.backend: doit valoir torchaudio_ctc"),
    ("whisper.forced_alignment.bundle_name", 42, "whisper.forced_alignment.bundle_name: doit être une chaîne ou null"),
    # granite
    ("granite", "bad", "granite: doit être un objet YAML"),
    ("granite.torch_dtype", "int8", "granite.torch_dtype: valeurs acceptées bfloat16, float16, float32"),
    ("granite.max_new_tokens_per_second", 0, "granite.max_new_tokens_per_second: doit être strictement positif ou null"),
    ("granite.prompt_mode", "chat", "granite.prompt_mode: valeurs acceptées asr_raw, asr_punctuated, keywords"),
    ("granite.keywords", [" "], "granite.keywords[0]: doit être une chaîne non vide"),
    ("granite.keywords", 42, "granite.keywords: doit être une chaîne ou une liste de chaînes"),
    ("granite.lexicon_keywords", "bad", "granite.lexicon_keywords: doit être un objet YAML"),
    ("granite.lexicon_keywords.priorities", [], "granite.lexicon_keywords.priorities: doit être une liste non vide"),
    ("granite.lexicon_keywords.priorities", ["haute"], "granite.lexicon_keywords.priorities[0]: valeurs acceptées critique, importante, normale"),
    # cohere
    ("cohere", "bad", "cohere: doit être un objet YAML"),
    ("cohere.lexicon_biasing", "bad", "cohere.lexicon_biasing: doit être un objet YAML"),
    ("cohere.lexicon_biasing.boost", 3, "cohere.lexicon_biasing.boost: doit être entre 0 et 2"),
    ("cohere.lexicon_biasing.start_boost", 2, "cohere.lexicon_biasing.start_boost: doit être entre 0 et 1"),
    ("cohere.lexicon_biasing.priorities", [], "cohere.lexicon_biasing.priorities: doit être une liste non vide"),
    ("cohere.lexicon_biasing.priorities", ["urgent"], "cohere.lexicon_biasing.priorities[0]: priorité invalide 'urgent'"),
    # cohere_tf5
    ("cohere_tf5", "bad", "cohere_tf5: doit être un objet YAML"),
    ("cohere_tf5.model_revision", 42, "cohere_tf5.model_revision: doit être une chaîne ou null"),
    # workflow.quality_transcription
    ("workflow.quality_transcription", "bad", "workflow.quality_transcription: doit être un objet YAML"),
    ("workflow.quality_transcription.force_stt_backend", "kroko", "force_stt_backend: doit valoir cohere, cohere_tf5, whisper, granite ou parakeet"),
    ("workflow.quality_transcription.enabled_for_modes", "fast", "workflow.quality_transcription.enabled_for_modes: doit être une liste"),
    ("workflow.quality_transcription.enabled_for_modes", ["slow"], "workflow.quality_transcription.enabled_for_modes: valeurs acceptées fast, quality"),
    ("workflow.quality_transcription.degraded_summary_levels", "x", "workflow.quality_transcription.degraded_summary_levels: doit être une liste"),
    ("workflow.quality_transcription.degraded_summary_levels", [" "], "degraded_summary_levels: valeurs chaîne non vides attendues"),
    # workflow.audio_quality
    ("workflow.audio_quality", "bad", "workflow.audio_quality: doit être un objet YAML"),
    ("workflow.audio_quality.degraded_levels", "x", "workflow.audio_quality.degraded_levels: doit être une liste"),
    ("workflow.audio_quality.min_speech_ratio", "x", "workflow.audio_quality.min_speech_ratio: doit être un nombre ou null"),
    # workflow.audio_preflight
    ("workflow.audio_preflight", "bad", "workflow.audio_preflight: doit être un objet YAML"),
    # workflow.segment_reliability
    ("workflow.segment_reliability", "bad", "workflow.segment_reliability: doit être un objet YAML"),
    ("workflow.segment_reliability.non_latin_min_chars", 0, "workflow.segment_reliability.non_latin_min_chars"),
    ("workflow.segment_reliability.non_latin_char_pattern", "[", "workflow.segment_reliability.non_latin_char_pattern: regex invalide"),
    ("workflow.segment_reliability.non_latin_char_pattern", 42, "workflow.segment_reliability.non_latin_char_pattern: doit être une chaîne ou null"),
    ("workflow.segment_reliability.generic_hallucination_patterns", "x", "workflow.segment_reliability.generic_hallucination_patterns: doit être une liste"),
    ("workflow.segment_reliability.generic_hallucination_patterns", [42], "generic_hallucination_patterns[0]: doit être une chaîne non vide"),
    ("workflow.segment_reliability.generic_hallucination_patterns", ["("], "generic_hallucination_patterns[0]: regex invalide"),
    # workflow.pyannote_chunking
    ("workflow.pyannote_chunking", "bad", "workflow.pyannote_chunking: doit être un objet YAML"),
    # workflow.vad
    ("workflow.vad", "bad", "workflow.vad: doit être un objet YAML"),
    ("workflow.vad.auto_enable_final_levels", "x", "workflow.vad.auto_enable_final_levels: doit être une liste"),
    ("workflow.vad.auto_enable_final_levels", [" "], "workflow.vad.auto_enable_final_levels[0]: doit être une chaîne non vide"),
    # workflow.transcription_cleanup
    ("workflow.transcription_cleanup", "bad", "workflow.transcription_cleanup: doit être un objet YAML"),
    ("workflow.transcription_cleanup.subtitle_artifact_patterns", "x", "workflow.transcription_cleanup.subtitle_artifact_patterns: doit être une liste"),
    ("workflow.transcription_cleanup.subtitle_artifact_words", [42], "workflow.transcription_cleanup.subtitle_artifact_words[0]: doit être une chaîne"),
    ("workflow.transcription_cleanup.non_latin_min_chars", 0, "workflow.transcription_cleanup.non_latin_min_chars"),
    # voxtral
    ("voxtral", "bad", "voxtral: doit être un objet YAML"),
    ("voxtral.torch_dtype", "int8", "voxtral.torch_dtype: valeurs acceptées bfloat16, float16, float32"),
    ("voxtral.max_new_tokens_per_second", 0, "voxtral.max_new_tokens_per_second: doit être strictement positif ou null"),
    # moss
    ("moss", "bad", "moss: doit être un objet YAML"),
    # kroko
    ("kroko", "bad", "kroko: doit être un objet YAML"),
    ("kroko.variant", "32", "kroko.variant: valeurs acceptées 64, 128"),
    ("kroko.decoding_method", "beam", "kroko.decoding_method: valeurs acceptées greedy_search, modified_beam_search"),
    # workflow.multi_stt
    ("workflow.multi_stt", "bad", "workflow.multi_stt: doit être un objet YAML"),
    ("workflow.multi_stt.secondary_backend", "moss", "workflow.multi_stt.secondary_backend: valeurs acceptées"),
    ("workflow.multi_stt.levels", [], "workflow.multi_stt.levels: doit être une liste non vide"),
    ("workflow.multi_stt.levels", ["bon"], "workflow.multi_stt.levels[0]: doit valoir suspect ou degrade"),
    # workflow.stt_hybrid
    ("workflow.stt_hybrid", "bad", "workflow.stt_hybrid: doit être un objet YAML"),
    ("workflow.stt_hybrid.enabled", True, "workflow.stt_hybrid.enabled: mode non encore intégré au pipeline"),
    ("workflow.stt_hybrid.fallback_on_reliability", "x", "workflow.stt_hybrid.fallback_on_reliability: doit être une liste"),
    ("workflow.stt_hybrid.fallback_on_reliability", ["moyen"], "workflow.stt_hybrid.fallback_on_reliability[0]: doit valoir ok, suspect ou degrade"),
    # workflow.audio_scene
    ("workflow.audio_scene", "bad", "workflow.audio_scene: doit être un objet YAML"),
    ("workflow.audio_scene.thresholds", "bad", "workflow.audio_scene.thresholds: doit être un objet YAML"),
    # workflow.audio_scene_filter
    ("workflow.audio_scene_filter", "bad", "workflow.audio_scene_filter: doit être un objet YAML"),
    ("workflow.audio_scene_filter.enabled_for_modes", "x", "workflow.audio_scene_filter.enabled_for_modes: doit être une liste"),
    ("workflow.audio_scene_filter.enabled_for_modes", ["slow"], "workflow.audio_scene_filter.enabled_for_modes: valeurs acceptées fast, quality"),
    ("workflow.audio_scene_filter.target_labels", "x", "workflow.audio_scene_filter.target_labels: doit être une liste"),
    ("workflow.audio_scene_filter.target_labels", ["speech"], "workflow.audio_scene_filter.target_labels: valeurs acceptées music, noise, noEnergy"),
    # workflow.audio_normalization
    ("workflow.audio_normalization", "bad", "workflow.audio_normalization: doit être un objet YAML"),
    ("workflow.audio_normalization.enabled_for_modes", "x", "workflow.audio_normalization.enabled_for_modes: doit être une liste"),
    ("workflow.audio_normalization.enabled_for_modes", ["slow"], "workflow.audio_normalization.enabled_for_modes: valeurs acceptées fast, quality"),
    ("workflow.audio_normalization.weak_voice", "bad", "workflow.audio_normalization.weak_voice: doit être un objet YAML"),
    # workflow.audio_denoise
    ("workflow.audio_denoise", "bad", "workflow.audio_denoise: doit être un objet YAML"),
    ("workflow.audio_denoise.enabled_for_modes", "x", "workflow.audio_denoise.enabled_for_modes: doit être une liste"),
    ("workflow.audio_denoise.enabled_for_modes", ["slow"], "workflow.audio_denoise.enabled_for_modes: valeurs acceptées fast, quality"),
    ("workflow.audio_denoise.trigger_flags", "x", "workflow.audio_denoise.trigger_flags: doit être une liste"),
    # workflow.source_separation
    ("workflow.source_separation", "bad", "workflow.source_separation: doit être un objet YAML"),
    ("workflow.source_separation.backend", "spleeter", "workflow.source_separation.backend: doit valoir demucs"),
    ("workflow.source_separation.stem", "voice", "workflow.source_separation.stem: valeurs acceptées vocals, drums, bass, other"),
    ("workflow.source_separation.decision", "bad", "workflow.source_separation.decision: doit être un objet YAML"),
    # workflow.speaker_realignment
    ("workflow.speaker_realignment", "bad", "workflow.speaker_realignment: doit être un objet YAML"),
    ("workflow.speaker_realignment.punctuation_chars", 42, "workflow.speaker_realignment.punctuation_chars: doit être une chaîne"),
    # diarization
    ("diarization", "bad", "diarization: doit être un objet YAML"),
    ("diarization.prepare_pcm_timeout_s", "x", "diarization.prepare_pcm_timeout_s: doit être un entier positif ou null"),
    ("diarization.prepare_pcm_timeout_s", 0, "diarization.prepare_pcm_timeout_s: doit être >= 1 ou null"),
    ("diarization.pipeline_params", "bad", "diarization.pipeline_params: doit être un objet YAML"),
    ("diarization.pipeline_params.embedding", {}, "diarization.pipeline_params.embedding: section non supportée"),
    ("diarization.pipeline_params.clustering", "bad", "diarization.pipeline_params.clustering: doit être un objet YAML"),
    ("diarization.pipeline_params.clustering", {"k": 1}, "diarization.pipeline_params.clustering.k: paramètre non supporté"),
    # workflow.execution / queue / scheduling
    ("workflow.execution", "bad", "workflow.execution: doit être un objet YAML"),
    ("workflow.execution.max_concurrent_jobs", 99, "workflow.execution.max_concurrent_jobs=99: doit être entre 1 et 8"),
    ("workflow.queue", "bad", "workflow.queue: doit être un objet YAML"),
    ("workflow.queue.default_priority", 0, "workflow.queue.default_priority=0: doit être entre 1 et 100"),
    ("workflow.scheduling", "bad", "workflow.scheduling: doit être un objet YAML"),
    ("workflow.scheduling.timezone", "  ", "workflow.scheduling.timezone: doit être une chaîne non vide"),
    ("workflow.scheduling.timezone", "Mars/Olympus", "fuseau horaire invalide 'Mars/Olympus'"),
    ("workflow.scheduling.kill_patterns", "x", "workflow.scheduling.kill_patterns: doit être une liste"),
    ("workflow.scheduling.kill_patterns", [" "], "workflow.scheduling.kill_patterns[0]: chaîne vide"),
    ("workflow.scheduling.windows", "x", "workflow.scheduling.windows: doit être une liste"),
    ("workflow.scheduling.windows", ["x"], "workflow.scheduling.windows[0]: doit être un objet YAML"),
    # quality
    ("quality", "bad", "quality: doit être un objet YAML"),
    ("quality.asr_noise_markers", "x", "quality.asr_noise_markers: doit être une liste"),
    ("quality.asr_noise_markers", [" "], "quality.asr_noise_markers[0]: doit être une chaîne non vide"),
    ("quality.thresholds", "bad", "quality.thresholds: doit être un objet YAML"),
    # security
    ("security.allowed_upload_extensions", [], "security.allowed_upload_extensions doit être une liste non vide"),
    ("security.allowed_upload_extensions", ["mp3"], "security.allowed_upload_extensions[0]='mp3' invalide"),
    ("security.allowed_document_extensions", [], "security.allowed_document_extensions doit être une liste non vide"),
    ("security.allowed_document_extensions", ["pdf"], "security.allowed_document_extensions[0]='pdf' invalide"),
]


@pytest.mark.parametrize(
    ("dotted", "value", "fragment"),
    CASES,
    ids=[f"{dotted}={value!r}" for dotted, value, _ in CASES],
)
def test_mutation_invalide_produit_l_erreur_attendue(dotted, value, fragment):
    _assert_error(_validate_mutated((dotted, value)), fragment)


# ---------------------------------------------------------------------------
# Cas croisés / à plusieurs clés
# ---------------------------------------------------------------------------


class TestCrossFieldValidators:
    def test_section_requise_manquante(self):
        cfg = deepcopy(get_default_config())
        del cfg["workflow"]
        _assert_error(validate_config(cfg), "Section 'workflow' manquante ou null")

    def test_llm_vram_mb_per_gpu_doit_suivre_les_indices(self):
        result = _validate_mutated(
            ("gpu.llm_gpu_indices", [0, 1]),
            ("gpu.llm_vram_mb_per_gpu", [26000]),
        )
        _assert_error(result, "gpu.llm_vram_mb_per_gpu: doit avoir autant d'éléments que gpu.llm_gpu_indices")

    def test_qwen_port_requis_sans_arbitrage_llm_port(self):
        cfg = deepcopy(get_default_config())
        del cfg["services"]["arbitrage_llm_port"]
        _assert_error(validate_config(cfg), "services.qwen_port: valeur manquante")

    def test_vllm_port_legacy_valide_sans_llm_cleanup_ports(self):
        cfg = deepcopy(get_default_config())
        del cfg["services"]["llm_cleanup_ports"]
        cfg["services"]["vllm_port"] = 0
        _assert_error(validate_config(cfg), "services.vllm_port=0: doit être entre 1 et 65535")

    def test_cohere_model_path_requis_pour_backend_cohere(self):
        result = _validate_mutated(("models.cohere_model_path", ""))
        _assert_error(result, "models.cohere_model_path doit être renseigné quand le backend STT est 'cohere'")

    def test_backend_servi_route_par_url_accepte(self):
        result = _validate_mutated(
            ("models.stt_backend", "qwen3asr"),
            ("inference.stt.backends.qwen3asr.url", "http://127.0.0.1:8021/v1"),
        )
        assert not any("models.stt_backend" in msg for msg in result.errors)

    def test_granite_min_new_tokens_borne_par_max(self):
        result = _validate_mutated(
            ("granite.min_new_tokens", 100),
            ("granite.max_new_tokens", 50),
        )
        _assert_error(result, "granite.min_new_tokens: doit être inférieur ou égal à granite.max_new_tokens")

    def test_granite_keywords_chaine_libre_acceptee(self):
        result = _validate_mutated(("granite.keywords", "DRITE, quorum"))
        assert not any("granite.keywords" in msg for msg in result.errors)

    def test_stt_hybrid_backends_identiques_refuses(self):
        result = _validate_mutated(
            ("workflow.stt_hybrid.primary_backend", "whisper"),
            ("workflow.stt_hybrid.fallback_backend", "whisper"),
        )
        _assert_error(result, "primary_backend et fallback_backend doivent être différents")

    def test_arbitration_llm_active_valide_api_base_et_opencode_bin(self):
        # Désactivée par défaut : _check_llm_section ne valide le reste que si enabled.
        result = _validate_mutated(
            ("workflow.arbitration_llm.enabled", True),
            ("workflow.arbitration_llm.model_id", "arbitrage"),
            ("workflow.arbitration_llm.api_base", "localhost:8080"),
            ("workflow.arbitration_llm.opencode_bin", "  "),
        )
        _assert_error(result, "workflow.arbitration_llm.api_base doit commencer par http")
        _assert_error(result, "workflow.arbitration_llm.opencode_bin: chemin manquant")

    def test_arbitration_llm_api_base_non_chaine(self):
        result = _validate_mutated(
            ("workflow.arbitration_llm.enabled", True),
            ("workflow.arbitration_llm.model_id", "arbitrage"),
            ("workflow.arbitration_llm.api_base", 42),
        )
        _assert_error(result, "workflow.arbitration_llm.api_base doit être une chaîne")

    def test_summary_llm_active_exige_api_base_http(self):
        result = _validate_mutated(
            ("workflow.summary_llm.enabled", True),
            ("workflow.summary_llm.model_id", "qwen"),
            ("workflow.summary_llm.timeout_seconds", 600),
            ("workflow.summary_llm.api_base", "localhost:8080"),
        )
        _assert_error(result, "workflow.summary_llm.api_base doit commencer par http")

    def test_llm_desactivee_ne_valide_pas_le_reste(self):
        result = _validate_mutated(
            ("workflow.arbitration_llm.enabled", False),
            ("workflow.arbitration_llm.api_base", "localhost:8080"),
        )
        assert not any("workflow.arbitration_llm.api_base" in msg for msg in result.errors)

    def test_fenetre_de_planification_invalide_cumule_les_erreurs(self):
        window = {
            "name": "  ",
            "start": "26:00",
            "end": "9h",
            "action": "stop",
            "days": ["monday"],
            "enabled": "oui",
        }
        result = _validate_mutated(("workflow.scheduling.windows", [window]))
        _assert_error(result, "workflow.scheduling.windows[0].name: chaîne vide")
        _assert_error(result, "workflow.scheduling.windows[0].start: heure invalide")
        _assert_error(result, "workflow.scheduling.windows[0].end: doit être au format HH:MM")
        _assert_error(result, "workflow.scheduling.windows[0].action: valeur invalide 'stop'")
        _assert_error(result, "workflow.scheduling.windows[0].days: jour invalide 'monday'")
        _assert_error(result, "workflow.scheduling.windows[0].enabled: doit être true/false")

    def test_fenetre_start_non_chaine(self):
        window = {"name": "n", "start": 9, "end": "10:00", "action": "none", "days": ["lundi"], "enabled": True}
        result = _validate_mutated(("workflow.scheduling.windows", [window]))
        _assert_error(result, "workflow.scheduling.windows[0].start: doit être une chaîne HH:MM")

    def test_fenetre_days_vide(self):
        window = {"name": "n", "start": "09:00", "end": "10:00", "action": "none", "days": [], "enabled": True}
        result = _validate_mutated(("workflow.scheduling.windows", [window]))
        _assert_error(result, "workflow.scheduling.windows[0].days: doit être une liste non vide")


# ---------------------------------------------------------------------------
# Avertissements (non bloquants) et branches silencieuses
# ---------------------------------------------------------------------------


class TestWarningsEtBranchesSilencieuses:
    def test_locale_inconnue_avertit_sans_bloquer(self):
        result = _validate_mutated(("i18n.available_locales", ["fr", "xx"]))
        assert any("langue 'xx' inconnue" in msg for msg in result.warnings)
        assert not any("available_locales" in msg for msg in result.errors)

    def test_mot_de_passe_admin_par_defaut_avertit(self):
        result = _validate_mutated(("auth.first_admin_password", "CHANGE-ME"))
        assert any("auth.first_admin_password" in msg for msg in result.warnings)

    def test_regex_vide_toleree(self):
        result = _validate_mutated(("workflow.transcription_cleanup.non_latin_char_pattern", "  "))
        assert not any("non_latin_char_pattern" in msg for msg in result.errors)

    def test_pipeline_params_section_nulle_toleree(self):
        result = _validate_mutated(("diarization.pipeline_params.segmentation", None))
        assert result.is_valid

    def test_regex_liste_nulle_toleree(self):
        result = _validate_mutated(("workflow.segment_reliability.generic_hallucination_patterns", None))
        assert result.is_valid


class TestSttServedPools:
    """Pools multi-instance §2.9 : extra_urls + resource_node.engines[].backend."""

    def test_extra_urls_valides_acceptees(self):
        result = _validate_mutated(
            ("inference.stt.backends", {"qwen3asr": {
                "url": "http://127.0.0.1:8021/v1",
                "extra_urls": ["http://127.0.0.1:8022/v1"],
            }}),
        )
        assert not [e for e in result.errors if "extra_urls" in e]

    def test_extra_urls_non_liste_erreur(self):
        result = _validate_mutated(
            ("inference.stt.backends", {"qwen3asr": {
                "url": "http://127.0.0.1:8021/v1", "extra_urls": "http://x",
            }}),
        )
        _assert_error(result, "extra_urls doit être une liste d'URLs")

    def test_extra_urls_entree_non_http_erreur(self):
        result = _validate_mutated(
            ("inference.stt.backends", {"qwen3asr": {
                "url": "http://127.0.0.1:8021/v1", "extra_urls": ["127.0.0.1:8022"],
            }}),
        )
        _assert_error(result, "extra_urls doit être une liste d'URLs")

    def test_engine_backend_inconnu_avertit(self):
        result = _validate_mutated(
            ("resource_node.engines", [{
                "name": "qwen3asr-gpu0", "backend": "qwen3asr",
                "script": "s.sh", "gpu": 0, "port": 8022,
            }]),
        )
        assert any("ne correspond à aucun backend" in w for w in result.warnings)

    def test_engine_backend_declare_sans_avertissement(self):
        result = _validate_mutated(
            ("inference.stt.backends", {"qwen3asr": {"url": "http://127.0.0.1:8021/v1"}}),
            ("resource_node.engines", [{
                "name": "qwen3asr-gpu0", "backend": "qwen3asr",
                "script": "s.sh", "gpu": 0, "port": 8022,
            }]),
        )
        assert not any("ne correspond à aucun backend" in w for w in result.warnings)


class TestMossSitePersistence:
    """0.3.8 : moss.moss_site sous /tmp (purgé au reboot) = avertissement explicite."""

    def test_site_sous_tmp_avec_moss_active_avertit(self):
        result = _validate_mutated(("moss.enabled", True), ("moss.moss_site", "/tmp/transcria_moss_site"))
        assert any("purgé au reboot" in w for w in result.warnings)

    def test_site_persistant_sans_avertissement(self):
        result = _validate_mutated(("moss.enabled", True), ("moss.moss_site", "./runtimes/moss_site"))
        assert not any("purgé au reboot" in w for w in result.warnings)

    def test_moss_desactive_pas_d_avertissement_meme_sous_tmp(self):
        result = _validate_mutated(("moss.moss_site", "/tmp/transcria_moss_site"))
        assert not any("purgé au reboot" in w for w in result.warnings)
