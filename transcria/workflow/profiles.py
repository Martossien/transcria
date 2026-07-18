"""Profils de traitement — contrat produit central (Phase 1 du cadrage profils).

Ce module remplace, à terme, le binaire `fast`/`quality` par des **profils de livrable**
nommés et stables. Il est volontairement **pur et additif** : il ne décrit que des données
immuables et des fonctions sans effet de bord. Aucune route, aucun pipeline, aucun wizard ne
l'utilise encore à ce stade — l'introduction de l'abstraction précède son câblage (cf.
`docs/PROFILS_TRAITEMENT_WORKFLOW.md`, plan d'action).

Le module est la **source unique** du contrat : ce qu'un profil produit, ce qu'il exige de
l'utilisateur, et quelles phases machine il exécute. Objectif : éliminer la dispersion des
`if mode == "quality"` (42 occurrences sur 17 fichiers à la revue du 2026-06-24).

Délibérément DIFFÉRÉ (phases ultérieures, pour rester sans dépendance DB/fs/config lourde) :

- `profile_prerequisites_status(job, fs, profile)` — état des prérequis humains (Phase 2/3,
  nécessite `Job` + `JobFilesystem`) ;
- la résolution **config-aware** de la classe de concurrence (local vs distant) et sa
  réconciliation avec `concurrency_profile.build_profile()` (Phase 3/5). Ici, `profile_phase_*`
  ne décrit que la vue NOMINALE locale ; le raffinement distant viendra avec le scheduler.

Acquis du spike diarisation (2026-06-24, cf. doc § « Résultat du spike ») encodé ici :
`srt_locuteurs` exécute `run_diarization=False`. La speakerisation du SRT se fait à la
transcription depuis `speaker_turns.json`, produit par l'étape wizard de détection des
locuteurs (`requires_speaker_validation`), pas par la phase diarisation du pipeline. Cette
dernière ne sert qu'au genre/stats des livrables DOCX → activée seulement pour les profils Word.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# ── Énumérations (alias Literal + sémantique) ────────────────────────────────--

# Exigence d'une étape humaine de préparation.
#   none              : non demandée, masquée
#   optional          : proposée, non bloquante
#   minimal           : version allégée demandée
#   required          : bloquante
#   required_or_empty : bloquante mais une validation « vide » explicite suffit
Requirement = Literal["none", "optional", "minimal", "required", "required_or_empty"]

PreprocessLevel = Literal["none", "minimal", "standard", "full"]
QualityLevel = Literal["none", "light", "full"]
DocxLevel = Literal["none", "basic", "structured", "enriched", "full"]
ZipLevel = Literal["none", "minimal", "standard", "full"]

# Étape wizard du lexique de session telle que présentée à l'utilisateur.
LexiconStep = Literal["hidden", "optional", "required", "required_or_empty"]

# Usage des lexiques centralisés par le profil.
#   none           : ignorés
#   prefill_only   : peuvent pré-remplir le Word, JAMAIS injectés dans la correction LLM
#   llm_correction : transmis à la correction LLM (si lexique session validé)
CentralLexiconUsage = Literal["none", "prefill_only", "llm_correction"]

# Politique de l'endpoint qualité pour le profil.
QualityEndpointPolicy = Literal["refuse", "light", "full"]

# Notification de fin de job au propriétaire.
NotificationLevel = Literal["silent", "owner"]

# Classe de concurrence d'une phase (vue produit/ressource ; cf. doc § classes de
# concurrence). NOMINALE ici : la bascule local↔distant est appliquée plus tard par le
# scheduler. Ne PAS dupliquer la taxonomie observabilité de `concurrency_profile.py`
# (SERIAL/DELEGATED) — celle-ci est plus riche et orientée admission/backpressure.
PhaseConcurrency = Literal[
    "human_interactive",
    "local_cpu",
    "local_gpu_exclusive",
    "remote_batchable",
    "remote_serialized",
    "remote_llm_batchable",
]


@dataclass(frozen=True)
class ResourceRequirements:
    """Ressources machine consommées par les phases du profil (vue statique).

    Sert de base à l'estimation VRAM profile-aware du scheduler (Phase 3). `needs_diarization`
    désigne la PHASE diarisation du pipeline (genre/DOCX), pas la détection wizard des locuteurs
    qui, elle, est comptée comme une étape synchrone séparée (`mode=speakers`).
    """

    needs_stt: bool
    needs_diarization: bool
    needs_llm: bool


@dataclass(frozen=True)
class ProcessingProfile:
    """Contrat produit immuable d'un profil de traitement.

    Les champs `requires_*` décrivent les étapes humaines (wizard) ; les champs `run_*`
    décrivent les phases machine du pipeline ; `docx_level`/`zip_level` décrivent les livrables.
    Aucune logique ici : un profil est une donnée. Les algorithmes vivent dans les fonctions
    pures du module.
    """

    id: str
    label: str
    description: str
    level: int

    # Étapes humaines (wizard).
    requires_summary: bool
    requires_context: Requirement
    requires_participants: Requirement
    requires_speaker_validation: Requirement
    requires_lexicon: Requirement

    # Phases machine (pipeline).
    run_preprocess: PreprocessLevel
    run_transcription: bool
    run_diarization: bool
    run_llm_correction: bool
    run_final_review: bool
    run_quality: QualityLevel

    # Livrables.
    docx_level: DocxLevel
    zip_level: ZipLevel

    # Ressources & flags produit sensibles.
    resource_requirements: ResourceRequirements
    voice_matching_eligible: bool
    lexicon_step: LexiconStep
    central_lexicon_usage: CentralLexiconUsage
    quality_endpoint_policy: QualityEndpointPolicy
    notification_level: NotificationLevel = "owner"

    # `legacy_fast` est transitoire (compatibilité `fast`) et exclu des listes produit.
    legacy: bool = False

    # Backend STT imposé par le profil (piste §4.1 : MOSS single-pass). None =
    # backend de la config (`models.stt_backend`) — comportement historique de
    # TOUS les profils existants. Un profil qui le fixe devient indisponible si
    # le backend ne l'est pas (cf. profile_availability).
    stt_backend: str | None = None


# ── Registre des profils (codés en dur = contrat stable, cf. doc § Risques) ──--

_PROFILES: dict[str, ProcessingProfile] = {
    "srt_express": ProcessingProfile(
        id="srt_express",
        label="SRT express",
        description="Transcription brute, le plus vite possible. Aucune validation.",
        level=1,
        requires_summary=False,
        requires_context="none",
        requires_participants="none",
        requires_speaker_validation="none",
        requires_lexicon="none",
        run_preprocess="minimal",
        run_transcription=True,
        run_diarization=False,
        run_llm_correction=False,
        run_final_review=False,
        run_quality="light",
        docx_level="none",
        zip_level="minimal",
        resource_requirements=ResourceRequirements(needs_stt=True, needs_diarization=False, needs_llm=False),
        voice_matching_eligible=False,
        lexicon_step="hidden",
        central_lexicon_usage="none",
        quality_endpoint_policy="light",
    ),
    "srt_locuteurs": ProcessingProfile(
        id="srt_locuteurs",
        label="SRT avec locuteurs",
        description="Transcription attribuée aux locuteurs. Validation des locuteurs.",
        level=2,
        requires_summary=False,
        requires_context="none",
        requires_participants="optional",
        requires_speaker_validation="required",
        requires_lexicon="none",
        run_preprocess="minimal",
        run_transcription=True,
        # Spike 2026-06-24 : la phase diarisation pipeline ne sert qu'au genre/DOCX. Les
        # locuteurs du SRT viennent de la détection wizard (requires_speaker_validation),
        # appliquée à la transcription. Donc PAS de phase diarisation pour un SRT seul.
        run_diarization=False,
        run_llm_correction=False,
        run_final_review=False,
        run_quality="light",
        docx_level="none",
        zip_level="minimal",
        resource_requirements=ResourceRequirements(needs_stt=True, needs_diarization=False, needs_llm=False),
        voice_matching_eligible=True,
        lexicon_step="hidden",
        central_lexicon_usage="none",
        quality_endpoint_policy="light",
    ),
    "srt_moss": ProcessingProfile(
        id="srt_moss",
        label="SRT locuteurs une passe (MOSS)",
        description=(
            "Transcription ET locuteurs en une seule passe GPU (MOSS), réservée aux "
            "réunions courtes (10 min par défaut). Aucune validation wizard : la voie "
            "la plus directe pour un SRT attribué. Omissions et troncatures du modèle "
            "surveillées (alertes qualité)."
        ),
        level=2,
        requires_summary=False,
        requires_context="none",
        requires_participants="none",
        # Une passe : les locuteurs viennent du STT MOSS lui-même — ni détection
        # pyannote wizard, ni phase diarisation pipeline (cf. spike srt_locuteurs).
        requires_speaker_validation="none",
        requires_lexicon="none",
        run_preprocess="minimal",
        run_transcription=True,
        run_diarization=False,
        run_llm_correction=False,
        run_final_review=False,
        run_quality="light",
        docx_level="none",
        zip_level="minimal",
        resource_requirements=ResourceRequirements(needs_stt=True, needs_diarization=False, needs_llm=False),
        voice_matching_eligible=False,
        lexicon_step="hidden",
        central_lexicon_usage="none",
        quality_endpoint_policy="light",
        stt_backend="moss",
    ),
    "word_rapide": ProcessingProfile(
        id="word_rapide",
        label="Word rapide",
        description="Compte rendu Word présentable rapidement, validation minimale.",
        level=3,
        requires_summary=True,
        requires_context="minimal",
        requires_participants="optional",
        requires_speaker_validation="none",
        requires_lexicon="none",
        run_preprocess="minimal",
        run_transcription=True,
        run_diarization=False,
        run_llm_correction=False,
        run_final_review=False,
        run_quality="light",
        docx_level="basic",
        zip_level="standard",
        resource_requirements=ResourceRequirements(needs_stt=True, needs_diarization=False, needs_llm=True),
        voice_matching_eligible=False,
        lexicon_step="hidden",
        central_lexicon_usage="prefill_only",
        quality_endpoint_policy="light",
    ),
    "word_structure": ProcessingProfile(
        id="word_structure",
        label="Word structuré",
        description="Word template avec participants et structure de réunion, sans correction SRT.",
        level=4,
        requires_summary=True,
        requires_context="required",
        requires_participants="required",
        requires_speaker_validation="required",
        requires_lexicon="none",
        run_preprocess="standard",
        run_transcription=True,
        # Profil Word : la phase diarisation alimente le genre/stats des locuteurs du DOCX.
        run_diarization=True,
        run_llm_correction=False,
        run_final_review=False,
        run_quality="light",
        docx_level="structured",
        zip_level="standard",
        resource_requirements=ResourceRequirements(needs_stt=True, needs_diarization=True, needs_llm=True),
        voice_matching_eligible=True,
        lexicon_step="hidden",
        central_lexicon_usage="prefill_only",
        quality_endpoint_policy="light",
    ),
    "word_corrige": ProcessingProfile(
        id="word_corrige",
        label="Word corrigé",
        description="Word + SRT corrigés (correction LLM), lexique optionnel.",
        level=5,
        requires_summary=True,
        requires_context="required",
        requires_participants="required",
        requires_speaker_validation="required",
        requires_lexicon="optional",
        run_preprocess="standard",
        run_transcription=True,
        run_diarization=True,
        run_llm_correction=True,
        run_final_review=True,
        run_quality="light",
        docx_level="enriched",
        zip_level="standard",
        resource_requirements=ResourceRequirements(needs_stt=True, needs_diarization=True, needs_llm=True),
        voice_matching_eligible=True,
        lexicon_step="optional",
        central_lexicon_usage="llm_correction",
        quality_endpoint_policy="light",
    ),
    "dossier_qualite": ProcessingProfile(
        id="dossier_qualite",
        label="Dossier qualité complet",
        description="Workflow complet : qualité maximale, lexique validé, ZIP complet.",
        level=6,
        requires_summary=True,
        requires_context="required",
        requires_participants="required",
        requires_speaker_validation="required",
        requires_lexicon="required_or_empty",
        run_preprocess="full",
        run_transcription=True,
        run_diarization=True,
        run_llm_correction=True,
        run_final_review=True,
        run_quality="full",
        docx_level="full",
        zip_level="full",
        resource_requirements=ResourceRequirements(needs_stt=True, needs_diarization=True, needs_llm=True),
        voice_matching_eligible=True,
        lexicon_step="required_or_empty",
        central_lexicon_usage="llm_correction",
        quality_endpoint_policy="full",
    ),
    # Transitoire (Option B du cadrage) : reproduit le comportement de l'ancien `fast`
    # (pipeline complet SANS diarisation). Exclu des listes produit ; à retirer après migration.
    "legacy_fast": ProcessingProfile(
        id="legacy_fast",
        label="Rapide (hérité)",
        description="Mode de compatibilité de l'ancien traitement « fast ». Transitoire.",
        level=0,
        requires_summary=True,
        requires_context="optional",
        requires_participants="optional",
        requires_speaker_validation="none",
        requires_lexicon="optional",
        run_preprocess="standard",
        run_transcription=True,
        run_diarization=False,
        run_llm_correction=True,
        run_final_review=True,
        run_quality="full",
        docx_level="full",
        zip_level="full",
        resource_requirements=ResourceRequirements(needs_stt=True, needs_diarization=False, needs_llm=True),
        voice_matching_eligible=False,
        lexicon_step="optional",
        central_lexicon_usage="llm_correction",
        quality_endpoint_policy="full",
        legacy=True,
    ),
}

# Mapping de compatibilité des anciens modes utilisateur (Option B du cadrage).
# `fast` -> `legacy_fast` (préserve les artefacts de l'ancien rapide) ; `quality` ->
# `dossier_qualite`. Les modes de file `summary`/`speakers` NE sont PAS des profils : ils
# restent des unités d'exécution gérées par `JobExecutorService` (cf. doc § axe file vs profil).
LEGACY_MODE_MAP: dict[str, str] = {
    "fast": "legacy_fast",
    "quality": "dossier_qualite",
}

DEFAULT_PROFILE_ID = "word_structure"

# Ordre des phases machine du pipeline (cf. pipeline_service). Sert à `profile_active_phases`.
_PIPELINE_PHASE_ORDER = (
    "preprocess",
    "transcription",
    "diarization",
    "correction",
    "final_review",
    "quality",
    "export",
)


# ── Accès au registre ────────────────────────────────────────────────────────-

def get_profile(processing_profile_id: str) -> ProcessingProfile:
    """Retourne le profil par id, ou lève `KeyError` si inconnu."""
    try:
        return _PROFILES[processing_profile_id]
    except KeyError as exc:
        raise KeyError(f"Profil de traitement inconnu : {processing_profile_id!r}") from exc


def is_profile(processing_profile_id: str) -> bool:
    return processing_profile_id in _PROFILES


def list_profiles(*, include_legacy: bool = False) -> list[ProcessingProfile]:
    """Profils triés par niveau croissant. Exclut les profils `legacy` par défaut."""
    profiles = [p for p in _PROFILES.values() if include_legacy or not p.legacy]
    return sorted(profiles, key=lambda p: p.level)


def resolve_legacy_mode(mode: str) -> str:
    """Résout un `mode` (legacy ou id de profil) vers un id de profil.

    - un id de profil connu est retourné tel quel ;
    - `fast`/`quality` sont mappés via `LEGACY_MODE_MAP` ;
    - sinon `ValueError`.
    """
    if is_profile(mode):
        return mode
    if mode in LEGACY_MODE_MAP:
        return LEGACY_MODE_MAP[mode]
    raise ValueError(f"Mode/profil non reconnu : {mode!r}")


def profile_to_legacy_mode(profile: ProcessingProfile) -> str:
    """Mode legacy d'exécution pipeline pour un profil (transition Phase 2-4).

    L'ancien `_define_pipeline_steps` ajoute la diarisation ssi `mode == "quality"`. Tant que le
    pipeline n'est pas profile-aware, on route donc via `quality` les profils qui diarisent et
    via `fast` les autres. Strictement transitoire.
    """
    return "quality" if profile.run_diarization else "fast"


def resolve_request(
    processing_profile_id: str | None,
    legacy_mode: str | None,
) -> tuple[ProcessingProfile, str]:
    """Résout une requête de lancement vers ``(profil, mode legacy de routage)``.

    Priorité au profil explicite (`processing_profile_id`) ; à défaut, le mode legacy
    (`fast`/`quality`) est mappé vers un profil. Le second membre du tuple est le **mode
    d'exécution** transmis a la file et au pipeline (encore mode-based jusqu'a la Phase 4) :
    `quality` pour les profils qui diarisent, `fast` sinon.

    Lève `KeyError` (profil inconnu) ou `ValueError` (mode inconnu) — l'appelant traduit en 400.
    """
    if processing_profile_id:
        profile = get_profile(processing_profile_id)
    else:
        profile = get_profile(resolve_legacy_mode(legacy_mode or "fast"))
    # Le mode de routage dérive TOUJOURS du profil (jamais de l'entrée brute) : un id de
    # profil passé dans le champ `mode` ne doit pas fuir comme mode d'exécution.
    return profile, profile_to_legacy_mode(profile)


# ── Fonctions pures de présentation / ressources ─────────────────────────────--

def profile_active_phases(profile: ProcessingProfile) -> list[str]:
    """Phases machine effectivement exécutées par le profil, dans l'ordre du pipeline."""
    active = {
        "preprocess": profile.run_preprocess != "none",
        "transcription": profile.run_transcription,
        "diarization": profile.run_diarization,
        "correction": profile.run_llm_correction,
        "final_review": profile.run_final_review,
        "quality": profile.run_quality != "none",
        "export": profile.docx_level != "none" or profile.zip_level != "none",
    }
    return [phase for phase in _PIPELINE_PHASE_ORDER if active[phase]]


def profile_required_remote_phases(profile: ProcessingProfile) -> set[str]:
    """Phases-ressources que le profil consomme (`stt`/`diarization`/`llm`).

    Vue PURE basée sur les flags du profil. Le scheduler (Phase 3) l'intersecte avec les
    capacités distantes configurées pour décider l'admission. Un profil ne doit jamais être
    bloqué par une ressource absente de cet ensemble (ex. `srt_express` ⊥ LLM/diarisation).
    """
    phases: set[str] = set()
    if profile.resource_requirements.needs_stt:
        phases.add("stt")
    if profile.resource_requirements.needs_diarization:
        phases.add("diarization")
    if profile.resource_requirements.needs_llm:
        phases.add("llm")
    return phases


def profile_phase_classes(profile: ProcessingProfile) -> dict[str, PhaseConcurrency]:
    """Classe de concurrence NOMINALE (déploiement local) par phase active.

    Vue produit indicative. La bascule local↔distant (`remote_batchable`/`remote_serialized`/
    `remote_llm_batchable`) est appliquée par le scheduler config-aware (Phase 3/5), où cette
    table sera réconciliée avec `concurrency_profile.build_profile()`. Ici, tout est local.
    """
    nominal: dict[str, PhaseConcurrency] = {
        "preprocess": "local_cpu",
        "transcription": "local_gpu_exclusive",
        "diarization": "local_gpu_exclusive",
        "correction": "remote_llm_batchable",
        "final_review": "remote_llm_batchable",
        "quality": "local_cpu",
        "export": "local_cpu",
    }
    return {phase: nominal[phase] for phase in profile_active_phases(profile)}


# Étapes wizard (ids de `WORKFLOW_STEPS`) dans leur ordre linéaire de validation.
_WIZARD_STEP_ORDER = ("summary", "context", "participants", "lexicon")


def profile_required_steps(profile: ProcessingProfile) -> set[str]:
    """Étapes wizard (ids) que le profil EXIGE avant le lancement du pipeline.

    Mappe les `requires_*` du profil vers les ids d'étapes du wizard. La détection des
    locuteurs et la validation des participants partagent l'étape « participants ». Le wizard
    étant linéaire, atteindre une étape implique les précédentes (cf. `compute_statuses`), donc
    ce SET suffit au gating de lancement. Un profil sans exigence (`srt_express`) est lançable
    dès l'analyse, sans aucune validation humaine.
    """
    steps: set[str] = set()
    if profile.requires_summary:
        steps.add("summary")
    if profile.requires_context in ("minimal", "required"):
        steps.add("context")
    if profile.requires_participants == "required" or profile.requires_speaker_validation == "required":
        steps.add("participants")
    if profile.requires_lexicon in ("required", "required_or_empty"):
        steps.add("lexicon")
    return steps


def profile_required_steps_ordered(profile: ProcessingProfile) -> list[str]:
    """Préfixe LINÉAIRE des étapes wizard à exécuter pour un profil (pour pilotes E2E/UI).

    Comme le wizard est séquentiel, on exécute toutes les étapes jusqu'à la plus profonde
    requise (ex. `srt_locuteurs` exige « participants », ce qui implique de jouer
    summary→context→participants). Vide pour un profil sans exigence.
    """
    required = profile_required_steps(profile)
    if not required:
        return []
    deepest = max(_WIZARD_STEP_ORDER.index(s) for s in required)
    return list(_WIZARD_STEP_ORDER[: deepest + 1])


def profile_for_job(job) -> ProcessingProfile | None:
    """Profil persisté sur le job (`extra_data.execution.processing_profile_id`, cf. Phase 2).

    Retourne None pour un job legacy/sans profil : les appelants RETOMBENT alors sur le
    comportement complet (full), garantissant la compatibilité ascendante (aucun job existant
    ne perd de livrable). `job` est un `transcria.jobs.models.Job` (ou compatible `get_extra_data`).
    """
    try:
        pid = (job.get_extra_data().get("execution", {}) or {}).get("processing_profile_id")
    except Exception:  # noqa: BLE001 — job non-DB / extra_data absent
        return None
    if pid and is_profile(pid):
        return get_profile(pid)
    return None


def profile_validations(profile: ProcessingProfile) -> list[str]:
    """Libellés FR des étapes humaines demandées par le profil (pour l'UI)."""
    items: list[str] = []
    if profile.requires_summary:
        items.append("Résumé de contrôle")
    if profile.requires_context in ("minimal", "required"):
        items.append("Contexte de réunion")
    if profile.requires_participants == "required":
        items.append("Participants")
    if profile.requires_speaker_validation == "required":
        items.append("Validation des locuteurs")
    if profile.requires_lexicon in ("required", "required_or_empty"):
        items.append("Lexique de session")
    elif profile.requires_lexicon == "optional":
        items.append("Lexique (optionnel)")
    return items


def profile_deliverables(profile: ProcessingProfile) -> list[str]:
    """Libellés FR des livrables garantis par le profil, pour l'UI."""
    items: list[str] = []
    if profile.run_diarization or profile.requires_speaker_validation in ("required", "optional"):
        items.append("SRT avec locuteurs")
    else:
        items.append("SRT")
    if profile.run_llm_correction:
        items.append("SRT corrigé")
    items.append("Segments JSON")
    docx_labels = {
        "basic": "Word (template de base)",
        "structured": "Word structuré",
        "enriched": "Word enrichi",
        "full": "Word complet",
    }
    if profile.docx_level != "none":
        items.append(docx_labels[profile.docx_level])
    if profile.run_quality == "full":
        items.append("Rapport qualité complet")
    if profile.zip_level == "full":
        items.append("Archive ZIP complète")
    return items
