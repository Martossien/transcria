import copy
import os

import yaml

_DEFAULT_CONFIG = {
    "server": {"host": "0.0.0.0", "port": 7870, "debug": True},
    # Rôle du process (Phase B / C1) : all (tout-en-un, défaut) | web | scheduler.
    # Surchargé par la variable d'environnement TRANSCRIA_ROLE.
    "runtime": {"role": "all"},
    "storage": {"jobs_dir": "./jobs", "database_url": "sqlite:///transcrIA.db"},
    "voice_enrollment": {
        "enabled": False,
        "storage_dir": "./voices",
        "require_active_consent": True,
        "delete_source_audio_after_embedding": True,
        "allow_global_profiles": False,
        "require_explicit_job_group_for_multi_group_users": True,
        "embedding": {
            "backend": "pyannote",
            "model_id": "pyannote/speaker-diarization-community-1",
            "model_revision": "",
            "expected_dim": None,
            "normalization": "l2",
            "min_speech_duration_s": 8.0,
            "min_segment_duration_s": 1.5,
            "max_segments_per_speaker": 5,
            "exclude_overlap": True,
        },
        "matching": {
            "enabled_after_summary": True,
            "suggestion_threshold": 0.75,
            "high_confidence_threshold": 0.85,
            "min_top2_margin": 0.08,
            "max_candidates_per_speaker": 2,
            "stale_profiles_are_matchable": False,
        },
        "consent": {
            "current_form_version": "voice-consent-v1",
            "allow_expiration": False,
            "validity_days": None,
            "proof_allowed_extensions": ["pdf", "png", "jpg", "jpeg"],
            "max_proof_size_mb": 25,
        },
        "audit": {
            "log_match_suggestions": True,
            "log_match_scores": True,
        },
    },
    "auth": {"enabled": True, "first_admin_username": "admin", "first_admin_password": "CHANGE-ME"},
    "gpu": {
        "cohere_vram_mb": 6000,
        "pyannote_vram_mb": 2000,
        "llm_vram_mb": 60000,
        "granite_vram_mb": 6000,
        "parakeet_vram_mb": 8000,
        "sortformer_vram_mb": 3500,
        "min_free_vram_mb": 4000,
    },
    "services": {
        "dashboard_llm_url": "http://127.0.0.1:5001",
        "srt_editor_easy_url": "http://127.0.0.1:7861",
        "arbitrage_script": "./scripts/launch_arbitrage.sh",
        "stop_script": "./scripts/stop_arbitrage_llm.sh",
        "arbitrage_llm_port": 8080,
        "llm_cleanup_ports": [8000],
    },
    "models": {
        "stt_backend": "cohere",
        "diarization_backend": "pyannote",
        "default_stt_model": "cohere-transcribe-03-2026",
        "fallback_stt_model": "large-v3",
        # Repo HF (auto-résolu/téléchargé) ou chemin local d'un modèle pré-téléchargé.
        "cohere_model_path": "CohereLabs/cohere-transcribe-03-2026",
        "cohere_model_revision": "",
        "pyannote_model": "pyannote/speaker-diarization-community-1",
    },
    "cohere": {
        "chunk_length_s": 30,
        "max_new_tokens": 448,
        "punctuation": True,
        "repetition_penalty": 1.2,
        "no_repeat_ngram_size": 4,
        "collapse_repetition_loops": True,
        "repetition_loop_min_repeats": 4,
        "repetition_loop_max_phrase_words": 10,
        "repetition_loop_keep_repeats": 2,
        "lexicon_biasing": {
            "enabled": False,
            "priorities": ["critique", "importante", "normale"],
            "max_terms": 300,
            "boost": 0.2,
            "start_boost": 0.05,
            "max_prefix_tokens": 20,
        },
    },
    "cohere_tf5": {
        "enabled": False,
        "tf5_site": "/tmp/transcria_tf54_site",
        "model_path": "CohereLabs/cohere-transcribe-03-2026",
        "model_revision": "",
        "timeout_s": 7200,
        "chunk_length_s": 30,
        "max_new_tokens": 448,
        "punctuation": True,
        "batch_size": 96,
        "repetition_penalty": 1.2,
        "no_repeat_ngram_size": 4,
        "collapse_repetition_loops": True,
        "repetition_loop_min_repeats": 4,
        "repetition_loop_max_phrase_words": 10,
        "repetition_loop_keep_repeats": 2,
    },
    "whisper": {
        "model_size": "large-v3",
        "compute_type": "float16",
        "cpu_threads": 4,
        "chunk_length_s": 30,
        "beam_size": 5,
        "best_of": 5,
        "vad_filter": False,
        "word_timestamps": True,
        "condition_on_previous_text": False,
        "no_speech_threshold": 0.2,
        "compression_ratio_threshold": 2.0,
        "log_prob_threshold": -1.0,
        "hallucination_silence_threshold": 3.0,
        "repetition_penalty": 1.0,
        "no_repeat_ngram_size": 0,
        "suppress_numerals": False,
        "hotwords": None,
        "initial_prompt": None,
        "lexicon_hotwords": {
            "enabled": False,
            "priorities": ["critique", "importante"],
            "max_terms": 50,
            "max_chars": 900,
            "max_tokens": 200,
            "tokenizer_model": "openai/whisper-large-v3",
            "prefix": "Termes importants :",
        },
        "collapse_repetition_loops": True,
        "repetition_loop_min_repeats": 4,
        "repetition_loop_max_phrase_words": 10,
        "repetition_loop_keep_repeats": 2,
        "forced_alignment": {
            "enabled": False,
            "backend": "torchaudio_ctc",
            "bundle_name": "VOXPOPULI_ASR_BASE_10K_FR",
            "max_segment_s": 30.0,
        },
    },
    "granite": {
        "enabled": False,
        "model_id": "./models/granite-speech-4.1-2b",
            "torch_dtype": "bfloat16",
            "chunk_length_s": 300,
            "max_new_tokens": 2000,
            "max_new_tokens_per_second": 8.0,
            "min_new_tokens": 64,
            "prompt_mode": "asr_punctuated",
        "prompt_asr_raw": "<|audio|>can you transcribe the speech into a written format?",
        "prompt_asr_punctuated": "<|audio|>transcribe the speech with proper punctuation and capitalization.",
        "prompt_keywords": "<|audio|>transcribe the speech to text. Keywords: {keywords}",
        "keywords": [],
        "fix_mistral_regex": True,
        "collapse_repetition_loops": True,
        "repetition_loop_min_repeats": 4,
        "repetition_loop_max_phrase_words": 10,
        "repetition_loop_keep_repeats": 2,
    },
    "sortformer": {
        "model_id": "nvidia/diar_streaming_sortformer_4spk-v2.1",
        "vram_mb": 3500,
    },
    "parakeet": {
        "enabled": False,
        "model_id": "nvidia/parakeet-tdt-0.6b-v3",
        "use_local_attention": True,
        "att_context_size": [256, 256],
        "decoding_strategy": "greedy_batch",
        "decoding_beam_size": 2,
        "max_chunk_duration_s": 1200,
        "collapse_repetition_loops": True,
        "repetition_loop_min_repeats": 4,
        "repetition_loop_max_phrase_words": 10,
        "repetition_loop_keep_repeats": 2,
    },
    "workflow": {
        "enable_quick_summary": True,
        "enable_speaker_detection": True,
        "enable_quality_mode": True,
        "enable_external_srt_editor_link": True,
        "enable_vad": True,
        "progress": {
            "enabled": True,
            "update_interval_s": 10.0,
        },
        "audio_quality": {
            "force_quality_backend": True,
            "degraded_levels": ["degrade"],
            "suspect_levels": ["suspect"],
            "min_bit_rate": 64000,
            "min_sample_rate_hz": 16000,
            "max_non_latin_segments": 2,
            "max_short_segment_ratio": 0.2,
            "min_speech_ratio": 0.35,
            "max_speech_ratio": 0.95,
            "scene_affects_quality_score": False,
            "max_scene_music_ratio": 0.15,
            "max_scene_noise_ratio": 0.20,
            "max_scene_no_energy_ratio": 0.30,
            "min_scene_speech_ratio": 0.55,
            "max_scene_problem_segments": 3,
        },
        "quality_transcription": {
            "force_stt_backend": None,
            "enabled_for_modes": [],
            "force_on_degraded_summary": False,
            "degraded_summary_levels": ["degrade"],
        },
        "audio_preflight": {
            "enabled": True,
            "frame_ms": 30,
            "low_rms_threshold": 0.02,
            "very_low_rms_threshold": 0.008,
            "silence_rms_threshold": 0.003,
            "low_snr_db_threshold": 6.0,
            "narrowband_hz_threshold": 3800.0,
            "clipping_threshold": 0.98,
            "clipping_ratio_threshold": 0.001,
            # Qualification du son par fenêtre (SQUIM : STOI/PESQ/SI-SDR → difficulty_map).
            "squim": {
                "enabled": True,          # SQUIM global toujours (cheap) quand le preflight tourne
                "segment_s": 5.0,
                "hop_s": 2.5,             # pas de la frise SQUIM sur GPU (pleine résolution)
                "hop_s_cpu": 5.0,         # pas élargi en repli CPU (≈ ÷2 fenêtres → vitesse, cf. hybride)
                "device": "auto",         # auto = GPU le PLUS libre (≥ vram_mb), sinon CPU. Contourne le GPU du LLM sans le tuer.
                "vram_mb": 5000,          # VRAM requise pour placer SQUIM sur un GPU (≈4,8 Go observés + marge)
                "stoi_threshold": 0.70,
                "pesq_threshold": 2.5,
                "sisdr_threshold": 5.0,
                # difficulty_map par fenêtre : lazy (seulement si l'audio n'est pas « ok »).
                # true = toujours la calculer (utile pour constituer le corpus de bench).
                "difficulty_map_always": False,
            },
            # DNSMOS P.835 (SIG/BAK/OVRL, MOS 1-5) : perceptif, distingue bruit vs
            # parole dégradée. Modèle ONNX embarqué (CC-BY-4.0, cf. THIRD_PARTY_NOTICES.md).
            "dnsmos": {
                "enabled": True,
                "ovrl_threshold": 2.5,    # OVRL < 2.5 → qualité globale dégradée
                "sig_bak_margin": 0.0,    # SIG < BAK - marge → parole intrinsèquement dégradée
            },
            # Métriques acoustiques par fenêtre (numpy/scipy, sans dépendance lourde).
            "acoustic": {
                "enabled": True,
                "rt60_threshold": 0.6,    # réverbération longue (s)
                "snr_threshold": 6.0,     # SNR par fenêtre (dB)
                "c50_threshold": -5.0,    # clarté faible (dB)
            },
        },
        "segment_reliability": {
            "enabled": True,
            "no_speech_prob_threshold": 0.5,
            "low_word_confidence_ratio": 0.5,
            "low_word_confidence_min": 0.4,
            "micro_segment_s": 0.35,
            "short_segment_s": 0.8,
            "detect_non_latin": True,
            "non_latin_char_pattern": (
                r"[\u0400-\u04FF\u0600-\u06FF\u0750-\u077F"
                r"\u4E00-\u9FFF\u3040-\u30FF\uAC00-\uD7AF]"
            ),
            "non_latin_min_chars": 2,
            "detect_generic_hallucinations": True,
            "degrade_on_text_flags": True,
            "generic_hallucination_patterns": [
                r"\bpour plus d['’]informations\b",
                r"\babonnez[- ]?vous\b",
                r"\bcontactez[- ]?nous\b",
                r"\bsite web\b",
                r"\bregard[ée] cette vid[ée]o\b",
                r"\bsous[- ]?titrage\b",
                r"\bradio[- ]?canada\b",
                r"\buniversit[eé] d['’]ottawa\b",
                r"^\s*thank\s+you\s*[.!?…]*\s*$",
                r"^\s*thank\s+you\s+very\s+much\s*[.!?…]*\s*$",
                r"^\s*thanks\s*[.!?…]*\s*$",
            ],
        },
        "pyannote_chunking": {
            "merge_micro_chunks": True,
            "micro_chunk_s": 0.35,
            "micro_chunk_neighbor_gap_s": 0.4,
            "isolated_min_chunk_s": 0.3,
            "padding_s": 0.15,
            "max_chunk_s": 45,
            "min_chunk_s": 1.5,
        },
        "vad": {
            "enabled_summary": True,
        "enabled_final": False,
        "auto_enable_final_on_degraded": False,
            "hysteresis_enabled": False,
            "onset": 0.5,
            "offset": 0.35,
            "auto_enable_final_levels": ["degrade"],
            "adaptive": True,
            "threshold": 0.5,
            "threshold_low_quality": 0.35,
            "threshold_high_noise": 0.6,
            "threshold_final_degraded": 0.6,
            "min_speech_duration_ms": 250,
            "min_silence_duration_ms": 400,
            "min_silence_duration_ms_low_quality": 250,
            "speech_pad_ms": 200,
            "speech_pad_ms_low_quality": 350,
        },
        "transcription_cleanup": {
            "enabled": True,
            "remove_subtitle_artifacts": True,
            "remove_obvious_hallucinations": True,
            "remove_non_latin_hallucinations": True,
            "remove_generic_hallucinations": True,
            "non_latin_char_pattern": (
                r"[\u0400-\u04FF\u0600-\u06FF\u0750-\u077F"
                r"\u4E00-\u9FFF\u3040-\u30FF\uAC00-\uD7AF]"
            ),
            "non_latin_min_chars": 2,
            "non_latin_min_ratio": 0.25,
            "generic_hallucination_languages": ["fr"],
            "generic_hallucination_patterns": [],
            "isolated_noise_artifact_words": ["501"],
            "isolated_noise_artifact_max_s": 0.8,
            "merge_short_segments": True,
            "short_segment_max_s": 0.45,
            "short_segment_max_words": 2,
            "merge_gap_s": 0.5,
            "merge_max_chars": 220,
            "subtitle_artifact_patterns": [],
            "subtitle_artifact_words": [],
        },
        "stt_hybrid": {
            "enabled": False,
            "primary_backend": "cohere",
            "fallback_backend": "whisper",
            "fallback_on_reliability": ["degrade"],
            "review_on_reliability": ["suspect"],
            "decision_margin": 3,
            "window_s": 30.0,
            "llm_arbitration_enabled": False,
            "write_audit_artifacts": True,
        },
        "audio_scene": {
            "enabled": False,
            "timeout_s": 120,
            "detect_gender": True,
            "thresholds": {
                "energy_ratio": 0.03,
                "min_segment_s": 0.3,
                "noise_flatness_min": 0.40,
                "music_flatness_max": 0.12,
                "music_zcr_max": 0.10,
                "music_suppress_bandwidth_hz": 3000.0,
                "female_pitch_hz": 165.0,
                "problem_segment_min_s": 2.0,
            },
        },
        "audio_scene_filter": {
            "enabled": False,
            "enabled_for_modes": ["quality"],
            "target_labels": ["music", "noise"],
            "min_segment_s": 2.0,
            "min_total_muted_s": 2.0,
            "edge_keep_s": 0.15,
            "max_intervals": 100,
            "timeout_s": 300,
        },
        "audio_normalization": {
            "enabled": False,
            "enabled_for_modes": ["quality"],
            "loudnorm_enabled": True,
            "target_i": -23.0,
            "true_peak": -2.0,
            "lra": 11.0,
            "highpass_hz": None,
            "timeout_s": 300,
            "auto_loudnorm_rms_threshold": 0.02,
            "weak_voice": {
                "enabled": True,
                "target_rms": 0.05,
                "max_gain": 8.0,
                "loudnorm_after_gain": True,
                "target_i": -23.0,
                "true_peak": -2.0,
                "lra": 11.0,
            },
        },
        "audio_denoise": {
            "enabled": False,
            "enabled_for_modes": ["quality"],
            "backend": "ffmpeg_afftdn",
            "force": False,
            "trigger_flags": ["snr_faible"],
            "noise_reduction_db": 12.0,
            "noise_floor_db": -25.0,
            "timeout_s": 300,
        },
        "source_separation": {
            "enabled": False,
            "backend": "demucs",
            "model": "htdemucs",
            "device": "auto",
            "segment_s": 10,
            "stem": "vocals",
            "decision": {
                "min_score": 3,
                "min_duration_s": 60,
                "scene_music_min_ratio": 0.80,
                "scene_music_min_duration_s": 60,
                "scene_music_min_speech_ratio_for_force": 0.08,
                "scene_noise_score_ratio": 0.35,
                "scene_noise_score": 1,
                "scene_problem_segments_score_threshold": 3,
                "scene_problem_segments_score": 1,
            },
        },
        "speaker_realignment": {
            "enabled": True,
            "min_word_overlap_s": 0.01,
            "punctuation_chars": ".,;:!?)]}»",
        },
        "execution": {"max_concurrent_jobs": 1},
        # Profil de concurrence (C7/B8) : surcharges déclaratives de la classe d'une étape.
        # Ex. {"transcribe": {"class": "delegated", "resource": "stt_backend"}}. Vide = la
        # classe est dérivée automatiquement (STT distant = délégué, sinon sériel).
        "concurrency_profile": {},
        "queue": {
            "enabled": True,
            "default_priority": 50,
            "aging_enabled": True,
            "aging_interval_minutes": 30,
            "aging_max_bonus": 49,
            "poll_interval_s": 5,
            "use_listen_notify": False,
            "starvation_timeout_hours": 24,
        },
        "scheduling": {
            "enabled": False,
            "timezone": "Europe/Paris",
            "poll_interval_s": 300,
            "kill_patterns": [
                "vllm",
                "llama-server",
                "text-generation-server",
                "aphrodite",
                "sglang",
                "lmdeploy",
                "exllamav2",
            ],
            "windows": [],
        },
        "summary_llm": {
            "enabled": False,
            "model_id": "",
            "api_base": "http://127.0.0.1:8080/v1",
            "timeout_seconds": 120,
        },
        "arbitration_llm": {
            "enabled": False,
            "model_id": "",
            "api_base": "http://127.0.0.1:8080/v1",
            "timeout_seconds": 600,
            "opencode_bin": "opencode",
        },
    },
    "quality": {
        "asr_noise_markers": [
            # --- Formules courtes ambiguës (suspicieuses dans un segment < 1 s) ---
            "thank you",
            "thanks",
            "okay",
            "all right",
            "bye",
            "absolutely",
            # --- Fillers français sur silence (Whisper génère sur bruit/silence court) ---
            "voilà",
            "et voilà",
            "voilà voilà",
            "c'est bon",
            "c'est ça",
            "c'est juste",
            "d'accord",
            "tout à fait",
            "très bien",
            "bien sûr",
            "effectivement",
            "exactement",
            "parfait",
            # --- Outros YouTube / vidéo — anglais ---
            "thanks for watching",
            "thank you for watching",
            "thank you for watching please subscribe",
            "please subscribe to my channel",
            "like and subscribe",
            "don't forget to subscribe",
            "please like and subscribe",
            # --- Outros YouTube / vidéo — français ---
            "merci d'avoir regardé cette vidéo",
            "merci d'avoir regardé",
            "n'oubliez pas de vous abonner",
            # --- Outros YouTube / vidéo — autres langues (Whisper hallucine même sur audio FR) ---
            "gracias",
            "gracias por ver el video",
            "gracias por ver",
            "obrigado",
            "obrigado por assistir",
            "e aí",
            # --- Crédits sous-titrage et services de transcription tiers ---
            "subtitles by the amara org community",
            "transcription by castingwords",
            "rev.com",
            "otter.ai",
            # --- Descriptions acoustiques (Whisper remplace parfois les hallucinations par des labels) ---
            "music",
            "applause",
            "clapping",
            "typing",
            "buzzing",
            # --- Artefacts de corpus divers documentés ---
            "come on",
            "hollywood",
        ],
        "thresholds": {
            # Détection de confiance STT (Whisper word-level probability)
            # no_speech_prob > seuil → segment probablement halluciné sur silence/bruit
            "no_speech_prob_threshold": 0.5,
            # Fraction de mots avec prob < low_word_confidence_min pour déclencher l'alerte
            "low_word_confidence_ratio": 0.5,
            # Seuil de confiance individuel par mot (prob < ce seuil = mot peu fiable)
            "low_word_confidence_min": 0.4,
        },
    },
    "diarization": {
        "cache_enabled": True,
        "cache_audio_fingerprint": True,
        "embedding_cache_enabled": True,
        "embedding_clip_seconds": 12.0,
        "progress_log_enabled": True,
        "progress_log_interval_s": 30.0,
        "min_speakers": 2,
        "max_speakers": 20,
        "num_speakers": None,
        "pipeline_params": {
            "segmentation": {
                "min_duration_off": None,
            },
            "clustering": {
                "threshold": None,
                "Fa": None,
                "Fb": None,
            },
        },
    },
    "notifications": {
        "email": {
            "enabled": False,
            "smtp_host": "",
            "smtp_port": 587,
            "smtp_username": "",
            "smtp_password": "",
            "use_starttls": True,
            "use_ssl": False,
            "from_address": "",
            "from_name": "TranscrIA",
            "base_url": "http://localhost:7870",
        }
    },
    "security": {
        "retention_days": 365,
        "allow_job_delete": True,
        "max_upload_size_mb": 1024,
        "allowed_upload_extensions": [".mp3", ".wav", ".m4a", ".mp4", ".flac", ".ogg"],
        "audit_retention_days": 1095,
        "lexicon_export_admin_only": False,
        "audit_retention_by_family": {
            "auth": 1095,
            "job": 1095,
            "lexicon": 1095,
            "voice": 1095,
            "config": 1095,
            "other": 1095,
        },
    },
    # Inférence distante : permet à TranscrIA d'être une frontale dont les
    # ressources GPU (diarisation, empreinte vocale, STT) tournent ailleurs —
    # ou sur la même machine via 127.0.0.1. mode="local" (défaut) = tout local,
    # aucun appel réseau : le comportement historique est strictement préservé.
    "inference": {
        "mode": "local",                       # local | remote | hybrid
        # Service Flask maison (diarisation + empreinte vocale), ex http://HOST:8002
        "url": "",
        # Failover actif/passif (C6 / B7) : liste ordonnée de nœuds (priorité = ordre).
        # La frontale vise le premier joignable et bascule automatiquement. Vide → on
        # retombe sur `url` ci-dessus (un seul nœud). Ex :
        #   [{"url": "http://gpu-1:8002", "priority": 1}, {"url": "http://gpu-2:8002", "priority": 2}]
        "nodes": [],
        "fallback_local": True,                # bascule locale si le service tombe
        "auth": {"api_key_env": "TRANSCRIA_INFERENCE_API_KEY", "api_key": ""},
        "transport": {"audio": "file_ref"},    # file_ref (mono-machine) | upload (distant)
        "resilience": {"timeout_s": 1800, "retries": 2},
        # STT via un serveur compatible OpenAI (vLLM, SGLang, … — non hardcodé),
        # endpoint /v1/audio/transcriptions, un port par moteur. Voir
        # scripts/launch_stt_*.sh. WAV/OGG acceptés, pas le MP3 (RemoteTranscriber
        # convertit automatiquement avant l'envoi).
        "stt": {
            "fallback_local": True,
            # Défaut global ; surchargeable par backend (voir ci-dessous).
            "response_format": "verbose_json",  # verbose_json (segments) | json (texte)
            "collapse_repetition_loops": True,
            # Transcription par tour en parallèle (distant uniquement). 1 = séquentiel
            # (défaut, comportement historique). >1 exploite le batching continu de vLLM.
            "concurrency": 1,
            "timeout_s": 600,
            "retries": 2,
            "auth": {"api_key_env": "TRANSCRIA_STT_API_KEY", "api_key": ""},
            "backends": {
                # url vide = ce moteur reste local même en mode remote/hybrid.
                # Exemple distant : "url": "http://127.0.0.1:8003/v1" (cohere).
                # response_format par moteur : Cohere Transcribe (vLLM) ne supporte
                # PAS verbose_json (400) → "json" (texte) ; Whisper gère les segments.
                "cohere": {"url": "", "model": "cohere-transcribe", "response_format": "json"},
                "whisper": {"url": "", "model": "whisper-large-v3", "response_format": "verbose_json"},
            },
        },
    },
}

_CONFIG_PATH_ENV = "TRANSCRIA_CONFIG"
_DEFAULT_CONFIG_PATH = "config.yaml"


def _deep_merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def get_default_config() -> dict:
    """Retourne une copie isolée de la configuration par défaut."""
    return copy.deepcopy(_DEFAULT_CONFIG)


def _normalize_config(cfg: dict) -> dict:
    normalized = copy.deepcopy(cfg)
    normalized.setdefault("auth", {})["enabled"] = True
    services = normalized.setdefault("services", {})
    if "arbitrage_llm_port" not in services and "qwen_port" in services:
        services["arbitrage_llm_port"] = services["qwen_port"]
    if "llm_cleanup_ports" not in services and "vllm_port" in services:
        services["llm_cleanup_ports"] = [services["vllm_port"]]
    workflow = normalized.setdefault("workflow", {})
    vad = workflow.setdefault("vad", {})
    if "enable_vad" in workflow:
        vad.setdefault("enabled_summary", bool(workflow["enable_vad"]))
        vad.setdefault("enabled_final", bool(workflow["enable_vad"]))
    return normalized


def _normalize_legacy_user_config(user_cfg: dict) -> dict:
    normalized = copy.deepcopy(user_cfg)
    services = normalized.get("services", {})
    if (
        isinstance(services, dict)
        and "vllm_port" in services
        and "llm_cleanup_ports" not in services
    ):
        services["llm_cleanup_ports"] = [services["vllm_port"]]
    workflow = normalized.get("workflow", {})
    if (
        isinstance(workflow, dict)
        and "enable_vad" in workflow
        and "vad" not in workflow
    ):
        enabled = bool(workflow["enable_vad"])
        workflow["vad"] = {
            "enabled_summary": enabled,
            "enabled_final": enabled,
        }
    return normalized


def load_config(config_path: str | None = None) -> dict:
    cfg = copy.deepcopy(_DEFAULT_CONFIG)

    if config_path is None:
        config_path = os.environ.get(_CONFIG_PATH_ENV, _DEFAULT_CONFIG_PATH)

    if os.path.isfile(config_path):
        with open(config_path, "r", encoding="utf-8") as fh:
            user_cfg = yaml.safe_load(fh)
        if user_cfg:
            user_cfg = _normalize_legacy_user_config(user_cfg)
            cfg = _deep_merge(cfg, user_cfg)

    return _normalize_config(cfg)


def get_config_path(config_path: str | None = None) -> str:
    return config_path or os.environ.get(_CONFIG_PATH_ENV, _DEFAULT_CONFIG_PATH) or _DEFAULT_CONFIG_PATH


def save_config(cfg: dict, config_path: str | None = None) -> str:
    path = get_config_path(config_path)
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(_normalize_config(cfg), fh, allow_unicode=True, sort_keys=False)
    return path


_config_singleton: dict | None = None


def get_config() -> dict:
    global _config_singleton
    if _config_singleton is None:
        _config_singleton = load_config()
    return _config_singleton


def set_config(cfg: dict) -> None:
    global _config_singleton
    _config_singleton = cfg
