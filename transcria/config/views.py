"""Vues typées de la config (vague C3) — le schéma dict reste la source de vérité.

Chaque vue est l'UNIQUE endroit qui fait les ``.get()`` de son sous-système :
un consommateur qui adopte une vue cesse d'embarquer ses propres défauts de
repli. Les défauts codés ici reproduisent EXACTEMENT ceux que les consommateurs
historiques embarquaient (sémantique comprise : ``is not False`` pour la LLM
d'arbitrage, truthy ailleurs) — ils sont verrouillés des deux côtés par
``tests/contracts/test_config_views_contract.py`` :
- vue sur ``{}``  == défauts consommateurs (golden) ;
- vue sur ``get_default_config()`` == valeurs du loader (golden).
Toute dérive de l'un ou l'autre casse la CI consciemment.

Règle d'adoption (plan §C3) : une vue par sous-système consommant ≥ 5 clés ;
le stock de chaînes ``.get("x", {}).get(...)`` fond par opportunité, jamais
par campagne. Les vues sont en lecture seule (frozen) — les écritures de
config (injections lexique, backend forcé) restent sur le dict.
"""
from dataclasses import dataclass, field

from transcria.config.llm_providers import (
    LOCAL_PROVIDER_ID,
    PROVIDER_SOURCE_CATALOG,
    PROVIDER_SOURCE_CUSTOM,
    PROVIDER_SOURCE_NONE,
    PROVIDER_SOURCE_TABLE,
    get_catalog_provider,
    get_llm_provider,
)


def _section(cfg: dict, *path: str) -> dict:
    node: object = cfg
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
        if node is None:
            return {}
    return node if isinstance(node, dict) else {}


@dataclass(frozen=True)
class GpuView:
    """Section ``gpu.*`` — budgets VRAM et placement de la LLM d'arbitrage."""

    cohere_vram_mb: int
    pyannote_vram_mb: int
    llm_vram_mb: int
    granite_vram_mb: int
    voxtral_vram_mb: int
    moss_vram_mb: int
    parakeet_vram_mb: int
    sortformer_vram_mb: int
    min_free_vram_mb: int
    llm_gpu_indices: tuple[int, ...] | None
    llm_vram_mb_per_gpu: tuple[int, ...] | None
    preemption: str

    @classmethod
    def from_config(cls, cfg: dict) -> "GpuView":
        gpu = _section(cfg, "gpu")
        indices = gpu.get("llm_gpu_indices")
        per_gpu = gpu.get("llm_vram_mb_per_gpu")
        return cls(
            cohere_vram_mb=int(gpu.get("cohere_vram_mb", 6000)),
            pyannote_vram_mb=int(gpu.get("pyannote_vram_mb", 2000)),
            llm_vram_mb=int(gpu.get("llm_vram_mb", 60000)),
            granite_vram_mb=int(gpu.get("granite_vram_mb", 6000)),
            voxtral_vram_mb=int(gpu.get("voxtral_vram_mb", 11000)),
            moss_vram_mb=int(gpu.get("moss_vram_mb", 4000)),
            parakeet_vram_mb=int(gpu.get("parakeet_vram_mb", 8000)),
            sortformer_vram_mb=int(gpu.get("sortformer_vram_mb", 3500)),
            min_free_vram_mb=int(gpu.get("min_free_vram_mb", 4000)),
            llm_gpu_indices=tuple(int(i) for i in indices) if isinstance(indices, list) else None,
            llm_vram_mb_per_gpu=tuple(int(mb) for mb in per_gpu) if isinstance(per_gpu, list) else None,
            preemption=str(gpu.get("preemption", "own-only")),
        )


@dataclass(frozen=True)
class QueueView:
    """Section ``workflow.queue.*`` — file d'attente et vieillissement des priorités."""

    enabled: bool
    default_priority: int
    aging_enabled: bool
    aging_interval_minutes: int
    aging_max_bonus: int
    poll_interval_s: int
    use_listen_notify: bool
    starvation_timeout_hours: int

    @classmethod
    def from_config(cls, cfg: dict) -> "QueueView":
        queue = _section(cfg, "workflow", "queue")
        return cls(
            enabled=bool(queue.get("enabled", True)),
            default_priority=int(queue.get("default_priority", 50)),
            aging_enabled=bool(queue.get("aging_enabled", True)),
            aging_interval_minutes=int(queue.get("aging_interval_minutes", 30)),
            aging_max_bonus=int(queue.get("aging_max_bonus", 49)),
            poll_interval_s=int(queue.get("poll_interval_s", 5)),
            use_listen_notify=bool(queue.get("use_listen_notify", False)),
            starvation_timeout_hours=int(queue.get("starvation_timeout_hours", 24)),
        )


@dataclass(frozen=True)
class QualityTranscriptionView:
    """Sous-section ``workflow.quality_transcription.*`` — backend qualité forcé."""

    force_stt_backend: str | None
    enabled_for_modes: tuple[str, ...]
    force_on_degraded_summary: bool
    degraded_summary_levels: tuple[str, ...]

    @classmethod
    def from_config(cls, cfg: dict) -> "QualityTranscriptionView":
        qt = _section(cfg, "workflow", "quality_transcription")
        backend = qt.get("force_stt_backend")
        modes = qt.get("enabled_for_modes", [])
        levels = qt.get("degraded_summary_levels", [])
        return cls(
            force_stt_backend=str(backend) if backend else None,
            enabled_for_modes=tuple(str(m) for m in modes) if isinstance(modes, list) else (),
            force_on_degraded_summary=bool(qt.get("force_on_degraded_summary", False)),
            # Normalisation = exactement ce que fait l'unique consommateur
            # (should_force_quality_backend_for_degraded_summary) : strip + non vide.
            degraded_summary_levels=tuple(
                str(level).strip() for level in (levels if isinstance(levels, list) else [])
                if str(level).strip()
            ),
        )


@dataclass(frozen=True)
class WorkflowView:
    """Drapeaux de haut niveau ``workflow.*`` — gating des phases du pipeline."""

    enable_quick_summary: bool
    enable_speaker_detection: bool
    enable_quality_mode: bool
    # Sémantique historique du pipeline : la LLM d'arbitrage n'est coupée que par
    # un ``false`` EXPLICITE (absent/None ⇒ activée) — cf. pipeline_sequence.
    arbitration_llm_enabled: bool
    # Le résumé LLM, lui, exige un ``true`` explicite (absent ⇒ désactivé).
    summary_llm_enabled: bool
    multi_stt_enabled: bool
    quality_transcription: QualityTranscriptionView

    @classmethod
    def from_config(cls, cfg: dict) -> "WorkflowView":
        wf = _section(cfg, "workflow")
        return cls(
            enable_quick_summary=bool(wf.get("enable_quick_summary", True)),
            enable_speaker_detection=bool(wf.get("enable_speaker_detection", True)),
            enable_quality_mode=bool(wf.get("enable_quality_mode", True)),
            arbitration_llm_enabled=_section(cfg, "workflow", "arbitration_llm").get("enabled") is not False,
            summary_llm_enabled=bool(_section(cfg, "workflow", "summary_llm").get("enabled", False)),
            multi_stt_enabled=bool(_section(cfg, "workflow", "multi_stt").get("enabled", False)),
            quality_transcription=QualityTranscriptionView.from_config(cfg),
        )


@dataclass(frozen=True)
class SttView:
    """Section ``models.*`` — moteurs STT/diarisation et modèles par défaut."""

    stt_backend: str
    diarization_backend: str
    default_stt_model: str
    fallback_stt_model: str
    cohere_model_path: str
    pyannote_model: str

    @classmethod
    def from_config(cls, cfg: dict) -> "SttView":
        models = _section(cfg, "models")
        return cls(
            stt_backend=str(models.get("stt_backend", "cohere")),
            diarization_backend=str(models.get("diarization_backend", "pyannote")),
            default_stt_model=str(models.get("default_stt_model", "")),
            fallback_stt_model=str(models.get("fallback_stt_model", "")),
            cohere_model_path=str(models.get("cohere_model_path", "")),
            pyannote_model=str(models.get("pyannote_model", "")),
        )


def _split_model_ref(model_id: str) -> tuple[str, str]:
    """``<fournisseur>/<modèle>`` opencode → (fournisseur, modèle) ; sans ``/`` → ("", id)."""
    raw = str(model_id or "").strip()
    if "/" not in raw:
        return "", raw
    provider, model = raw.split("/", 1)
    return provider.strip().lower(), model.strip()


# Défaut historique de `workflow.arbitration_llm.api_base` (loader) : l'endpoint de la LLM locale.
_LOCAL_LLM_DEFAULT_API_BASE = "http://127.0.0.1:8080/v1"


@dataclass(frozen=True)
class LlmCloudView:
    """LLM d'arbitrage chez un FOURNISSEUR EXTERNE (``services.backend: cloud``, docs/LLM_CLOUD.md).

    L'UNIQUE endroit qui lit ``workflow.llm_cloud.*`` et découpe les deux ``model_id``
    (``<fournisseur>/<modèle>``). Résolution de l'URL et de la variable de clé, dans l'ordre
    (``provider_source``) : table des fournisseurs qualifiés (``transcria/data/llm_providers.yaml``)
    > catalogue models.dev d'opencode (cache local : URL ``api`` + variable ``env``) > dérogations
    de config (``workflow.arbitration_llm.api_base`` + ``workflow.llm_cloud.api_key_env``).
    La vue ne dit PAS si le backend cloud est actif (c'est ``arbitrage_endpoint.is_cloud_backend``).
    """

    provider: str                 # premier segment du model_id d'arbitrage ("" si absent)
    model: str                    # modèle nu pour l'API directe (premier segment retiré)
    summary_provider: str         # premier segment du model_id de résumé
    summary_model: str
    known_provider: bool          # connu d'opencode (table OU catalogue) : aucun bloc opencode.json requis
    base_url: str                 # URL OpenAI-compatible effective ("" si indéterminable)
    api_key_env: str              # variable d'environnement qui porte la clé ("" si inconnue)
    extra_request_fields: dict = field(default_factory=dict)
    provider_source: str = PROVIDER_SOURCE_NONE   # table | catalog | custom | "" (aucun fournisseur)
    provider_label: str = ""                      # libellé affichable (table/catalogue), sinon l'identifiant

    @property
    def qualified(self) -> bool:
        """Fournisseur de la table : faits d'hébergement/conservation connus et affichés."""
        return self.provider_source == PROVIDER_SOURCE_TABLE

    @property
    def is_local_provider(self) -> bool:
        return self.provider == LOCAL_PROVIDER_ID

    @classmethod
    def from_config(cls, cfg: dict) -> "LlmCloudView":
        cloud = _section(cfg, "workflow", "llm_cloud")
        arb = _section(cfg, "workflow", "arbitration_llm")
        summ = _section(cfg, "workflow", "summary_llm")
        provider, model = _split_model_ref(str(arb.get("model_id") or ""))
        s_provider, s_model = _split_model_ref(str(summ.get("model_id") or ""))
        arb_enabled = arb.get("enabled") is not False
        if s_provider and (not provider or (not arb_enabled and summ.get("enabled"))):
            # Le fournisseur vient d'une section ACTIVÉE : correction coupée (`enabled: false`,
            # model_id résiduel `local/arbitrage`) et résumé en cloud ⇒ le résumé fait foi.
            # Sinon un résumé cloud lirait le fournisseur « local » et échouerait toujours.
            provider, model = s_provider, s_model
        listed = get_llm_provider(provider)
        catalog = get_catalog_provider(provider) if (provider and listed is None) else None
        api_base = str(arb.get("api_base") or "").strip().rstrip("/")
        key_env = str(cloud.get("api_key_env") or "").strip()
        extra = cloud.get("extra_request_fields")
        if listed is not None:
            source, base_url, env_name, label = PROVIDER_SOURCE_TABLE, listed.base_url, listed.api_key_env, listed.label
        elif catalog is not None:
            # Catalogue : URL ET variable viennent de models.dev — c'est ce qu'opencode lit lui-même
            # (sa base d'identifiants est masquée par le XDG_DATA_HOME de chaque run) ; une autre
            # variable ou un `api_base` de config ne seraient suivis que par nos appels directs.
            source, base_url, env_name, label = PROVIDER_SOURCE_CATALOG, catalog.base_url, catalog.api_key_env, catalog.label
        elif provider:
            # Hors table et hors catalogue : la dérogation de config est l'UNIQUE source. Le défaut
            # du loader (LLM LOCALE sur 8080) n'est JAMAIS une URL cloud : cache models.dev absent
            # ⇒ vue SANS URL, plutôt qu'un Bearer envoyé à ce qui écoute sur 8080.
            cloud_base = "" if api_base.rstrip("/") == _LOCAL_LLM_DEFAULT_API_BASE else api_base
            source, base_url, env_name, label = PROVIDER_SOURCE_CUSTOM, cloud_base, key_env, provider
        else:
            source, base_url, env_name, label = PROVIDER_SOURCE_NONE, "", key_env, ""
        return cls(
            provider=provider,
            model=model,
            summary_provider=s_provider,
            summary_model=s_model,
            known_provider=source in (PROVIDER_SOURCE_TABLE, PROVIDER_SOURCE_CATALOG),
            base_url=base_url,
            api_key_env=env_name,
            extra_request_fields=dict(extra) if isinstance(extra, dict) else {},
            provider_source=source,
            provider_label=label,
        )


@dataclass(frozen=True)
class AskMeetingsView:
    """Section ``workflow.ask_meetings.*`` — « Discuter avec mes réunions » (docs/DISCUTER_REUNIONS.md).

    Les plafonds sont dimensionnés sur la mesure du lot 0 : une fiche ≈ 610 tokens en médiane,
    ≈ 990 au 90e percentile — 40 fiches ≈ 24 000 tokens.
    """

    enabled: bool
    max_cards: int
    max_context_tokens: int
    level2_meetings: int
    context_turns: int
    retention_days: int
    timeout_s: int
    llm_lock_timeout_s: int
    max_question_chars: int
    max_answer_tokens: int

    @classmethod
    def from_config(cls, cfg: dict) -> "AskMeetingsView":
        ask = _section(cfg, "workflow", "ask_meetings")
        return cls(
            enabled=bool(ask.get("enabled", True)),
            max_cards=max(1, int(ask.get("max_cards", 40))),
            max_context_tokens=max(500, int(ask.get("max_context_tokens", 24000))),
            level2_meetings=max(1, int(ask.get("level2_meetings", 3))),
            context_turns=max(0, int(ask.get("context_turns", 6))),
            retention_days=max(0, int(ask.get("retention_days", 30))),
            timeout_s=max(30, int(ask.get("timeout_s", 900))),
            llm_lock_timeout_s=max(1, int(ask.get("llm_lock_timeout_s", 30))),
            max_question_chars=max(1, int(ask.get("max_question_chars", 2000))),
            max_answer_tokens=max(64, int(ask.get("max_answer_tokens", 1200))),
        )
