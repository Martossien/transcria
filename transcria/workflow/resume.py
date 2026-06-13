"""État de reprise du pipeline (checkpoint / resume) — v2 : provenance par empreintes.

Permet à `PipelineService` de **sauter les phases déjà faites** et de **reprendre à la
première incomplète** après un re-queue (vram_wait / deferred / correction en attente),
au lieu de tout refaire depuis le STT. Voir docs/PIPELINE_REPRISE.md.

Modèle (sur le Job, persistant, survit aux re-queues) :
- ``extra_data.pipeline.completed_phases`` : liste ordonnée des phases **réussies**
  (marqueur autoritatif, écrit atomiquement après succès complet) ;
- ``extra_data.pipeline.phase_inputs`` : par phase, les **empreintes sha256 de ses
  entrées** au moment du checkpoint — la provenance de l'artefact ;
- ``extra_data.pipeline.audio_path`` : chemin audio **final** après les transforms pré-STT.

Le pipeline est une CHAÎNE de dépendances (la correction lit le SRT brut, la qualité lit
le SRT corrigé, l'export emballe tout). Un skip n'est donc légitime que si les entrées de
la phase n'ont pas bougé depuis son checkpoint : c'est ce que vérifient les empreintes.
Quand une phase amont se rejoue, les empreintes des phases aval ne correspondent plus →
elles se ré-exécutent. L'invalidation aval est une *conséquence* de la provenance, pas un
bookkeeping d'ordre.

Pourquoi des sha256 et pas des mtimes : en topologie split (storage.shared_backend: pg),
`pull_job_files` rematérialise les fichiers SANS préserver leurs mtimes — seule une
comparaison par contenu est stable d'une machine à l'autre.

Principe directeur : **doute → re-run**. Se rejouer est toujours sûr ; se sauter à tort ne
l'est jamais (rapport qualité calculé sur un SRT périmé, export incohérent…). C'est pour
cela que le rétro-remplissage sur simple présence d'artefact est restreint à
`transcription` (phase chère, sans entrée empreintée) et qu'un marqueur sans empreintes
(état legacy) ne suffit pas pour une phase à entrées déclarées.
"""

from __future__ import annotations

import hashlib

# Phases du pipeline principal, dans l'ordre. (Le préprocess regroupe les transforms audio.)
PIPELINE_PHASES = (
    "preprocess",
    "transcription",
    "diarization",
    "correction",
    "final_review",
    "quality",
    "export",
)

# Artefacts NON AMBIGUS d'une phase. Leur présence est une condition NÉCESSAIRE au skip
# (artefact déclaré absent = phase à rejouer), mais jamais suffisante à elle seule : la
# provenance (`_PHASE_INPUTS`) tranche. Seule `transcription` garde le rétro-remplissage
# « artefact présent ⇒ fait » : c'est la phase la plus chère, sans entrée empreintée.
_PHASE_ARTIFACT: dict[str, str] = {
    "transcription": "metadata/transcription.srt",
    "correction": "metadata/transcription_corrigee.srt",
    "quality": "quality/quality_report.json",
}

# Entrées EMPREINTÉES par phase (relpaths sous le répertoire du job). Uniquement des
# fichiers texte/JSON synchronisés : l'audio est VOLONTAIREMENT exclu (gros, intermédiaires
# hors synchro, débruitage non bit-exact entre machines — l'empreinter ferait rejouer le
# STT à chaque changement de worker, la boucle qu'on a éradiquée). Un changement d'entrée
# audio passe par la re-soumission utilisateur, qui reset l'état de reprise.
# RÈGLE : toute nouvelle phase déclare ici les fichiers qu'elle lit (cf. AGENTS.md).
_PHASE_INPUTS: dict[str, tuple[str, ...]] = {
    "preprocess": (),
    "transcription": (),
    "diarization": (),
    "correction": (
        "metadata/transcription.srt",
        "context/session_lexicon_filtered.json",
        "context/job_context.yaml",
    ),
    "final_review": (
        "metadata/transcription_corrigee.srt",
        "context/session_lexicon.json",
    ),
    "quality": (
        "metadata/transcription.srt",
        "metadata/transcription_corrigee.srt",
        "metadata/transcription_segments.json",
        "context/session_lexicon.json",
    ),
    "export": (
        "metadata/transcription.srt",
        "metadata/transcription_corrigee.srt",
        "quality/quality_report.json",
        "context/meeting_context.json",
        "summary/summary.md",
    ),
}

# Sentinelle d'empreinte pour un fichier d'entrée absent (absent == absent ⇒ inchangé :
# une entrée optionnelle manquante des deux côtés ne force pas de re-run).
_ABSENT = "absent"


def _pipeline_state(job) -> dict:
    try:
        return dict((job.get_extra_data() or {}).get("pipeline") or {})
    except Exception:  # noqa: BLE001
        return {}


def get_completed_phases(job) -> list[str]:
    phases = _pipeline_state(job).get("completed_phases")
    return list(phases) if isinstance(phases, list) else []


def get_phase_fingerprints(job) -> dict[str, dict[str, str]]:
    """Empreintes d'entrées enregistrées au checkpoint, par phase."""
    recorded = _pipeline_state(job).get("phase_inputs")
    if not isinstance(recorded, dict):
        return {}
    return {k: dict(v) for k, v in recorded.items() if isinstance(v, dict)}


def get_processed_audio_path(job) -> str | None:
    path = _pipeline_state(job).get("audio_path")
    return path if isinstance(path, str) and path else None


def get_skipped_phases(job) -> dict[str, str]:
    """Phases sautées pour cause TRANSITOIRE (ressource indisponible), par raison.

    Permet à l'UI / l'audit de savoir qu'un livrable est dégradé (ex. relecture finale
    non exécutée faute de LLM disponible) au lieu d'un silence. Vidé quand la phase finit
    par réussir (cf. mark_phase_done).
    """
    skipped = _pipeline_state(job).get("skipped_phases")
    if not isinstance(skipped, dict):
        return {}
    return {str(k): str(v) for k, v in skipped.items()}


def _sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def compute_input_fingerprints(phase: str, fs) -> dict[str, str]:
    """Empreintes ACTUELLES des entrées déclarées de `phase` (sha256 par fichier)."""
    fingerprints: dict[str, str] = {}
    for rel in _PHASE_INPUTS.get(phase, ()):
        try:
            path = fs.job_dir / rel
            fingerprints[rel] = _sha256_file(path) if path.is_file() else _ABSENT
        except Exception:  # noqa: BLE001 — fichier illisible = considéré changé (doute → re-run)
            fingerprints[rel] = "unreadable"
    return fingerprints


def artifact_exists(phase: str, fs) -> bool:
    """L'artefact non ambigu de cette phase existe-t-il sur disque ?"""
    rel = _PHASE_ARTIFACT.get(phase)
    if not rel or fs is None:
        return False
    try:
        return (fs.job_dir / rel).is_file()
    except Exception:  # noqa: BLE001
        return False


def phase_state_valid(phase: str, fs, recorded_fingerprints: dict | None) -> bool:
    """Une phase MARQUÉE faite peut-elle être sautée ?

    Trois conditions : artefact déclaré présent, ET (si la phase a des entrées déclarées)
    empreintes enregistrées présentes ET identiques aux empreintes actuelles. Un marqueur
    sans empreintes (job en vol lors du déploiement v2) ne suffit pas : doute → re-run.
    """
    if phase in _PHASE_ARTIFACT and not artifact_exists(phase, fs):
        return False
    declared = _PHASE_INPUTS.get(phase, ())
    if not declared:
        return True
    if not isinstance(recorded_fingerprints, dict) or not recorded_fingerprints:
        return False
    return recorded_fingerprints == compute_input_fingerprints(phase, fs)


def is_phase_done(job, phase: str, fs=None) -> bool:
    """Phase faite ET sautable : marqueur + artefact + provenance intacte.

    (Le rétro-remplissage `transcription` est géré par le pipeline, pas ici : cette
    fonction est un lecteur sans effet de bord.)
    """
    if phase not in get_completed_phases(job):
        return False
    return phase_state_valid(phase, fs, get_phase_fingerprints(job).get(phase))


def mark_phase_done(store, job_id: str, phase: str, fingerprints: dict[str, str] | None = None) -> None:
    """Inscrit `phase` comme réussie, avec la provenance de ses entrées (idempotent, atomique)."""
    def updater(extra: dict) -> dict:
        pipeline = dict(extra.get("pipeline") or {})
        done = list(pipeline.get("completed_phases") or [])
        if phase not in done:
            done.append(phase)
        pipeline["completed_phases"] = done
        inputs = dict(pipeline.get("phase_inputs") or {})
        if fingerprints is not None:
            inputs[phase] = dict(fingerprints)
        else:
            # Marquage sans empreintes : ne pas laisser traîner une provenance périmée.
            inputs.pop(phase, None)
        pipeline["phase_inputs"] = inputs
        # Une phase qui finit par réussir n'est plus « sautée » : nettoyer le flag.
        skipped = dict(pipeline.get("skipped_phases") or {})
        if skipped.pop(phase, None) is not None:
            pipeline["skipped_phases"] = skipped
        extra["pipeline"] = pipeline
        return extra

    store.update_extra_data(job_id, updater)


def mark_phase_skipped(store, job_id: str, phase: str, reason: str) -> None:
    """Note qu'une phase a été sautée pour cause TRANSITOIRE, SANS la marquer faite.

    Un skip transitoire (LLM occupée par un autre job, VRAM momentanément insuffisante)
    n'a rien produit : la phase ne doit donc PAS entrer dans ``completed_phases`` (sinon
    elle ne serait jamais rejouée — perte silencieuse). On enregistre la raison dans
    ``skipped_phases`` (auditable, surfaçable en UI) et on garantit l'absence de tout
    marqueur/empreinte périmés pour cette phase. Idempotent, atomique.
    """
    def updater(extra: dict) -> dict:
        pipeline = dict(extra.get("pipeline") or {})
        pipeline["completed_phases"] = [
            p for p in (pipeline.get("completed_phases") or []) if p != phase
        ]
        inputs = dict(pipeline.get("phase_inputs") or {})
        inputs.pop(phase, None)
        pipeline["phase_inputs"] = inputs
        skipped = dict(pipeline.get("skipped_phases") or {})
        skipped[phase] = str(reason)
        pipeline["skipped_phases"] = skipped
        extra["pipeline"] = pipeline
        return extra

    store.update_extra_data(job_id, updater)


def unmark_phase(store, job_id: str, phase: str) -> None:
    """Retire `phase` du marqueur (provenance invalidée) — PERSISTANT.

    Persister l'invalidation avant d'exécuter la phase garde les marqueurs honnêtes pour
    tous les lecteurs : l'admission du scheduler (`_done_profile_phases`) compte à nouveau
    la VRAM de cette phase si un vram_wait re-queue le job au milieu de la chaîne, et
    l'UI ne prétend pas qu'une étape périmée est faite.
    """
    def updater(extra: dict) -> dict:
        pipeline = dict(extra.get("pipeline") or {})
        done = [p for p in (pipeline.get("completed_phases") or []) if p != phase]
        pipeline["completed_phases"] = done
        inputs = dict(pipeline.get("phase_inputs") or {})
        inputs.pop(phase, None)
        pipeline["phase_inputs"] = inputs
        extra["pipeline"] = pipeline
        return extra

    store.update_extra_data(job_id, updater)


def set_processed_audio_path(store, job_id: str, audio_path: str) -> None:
    """Mémorise le chemin audio final (après transforms) pour la reprise."""
    def updater(extra: dict) -> dict:
        pipeline = dict(extra.get("pipeline") or {})
        pipeline["audio_path"] = str(audio_path)
        extra["pipeline"] = pipeline
        return extra

    store.update_extra_data(job_id, updater)


def reset_resume_state(store, job_id: str) -> None:
    """Vide l'état de reprise (re-soumission utilisateur / changement de mode → run propre).

    NE PAS appeler sur un re-queue automatique (vram_wait/deferred) : la reprise repose
    justement sur la persistance de `completed_phases`.
    """
    def updater(extra: dict) -> dict:
        extra.pop("pipeline", None)
        return extra

    store.update_extra_data(job_id, updater)
