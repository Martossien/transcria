import enum
import uuid
from datetime import datetime, timezone

from transcria.database import db


class AuditAction(str, enum.Enum):
    LOGIN = "login"
    LOGIN_FAILED = "login_failed"
    LOGOUT = "logout"

    JOB_VIEW = "job_view"
    JOB_DOWNLOAD = "job_download"
    JOB_DELETE = "job_delete"
    JOB_SPEAKER_MAP = "job_speaker_map"
    JOB_SRT_EDIT_SAVE = "job_srt_edit_save"
    JOB_LEXICON_SAVE = "job_lexicon_save"
    JOB_CONTEXT_SAVE = "job_context_save"
    JOB_PARTICIPANTS_SAVE = "job_participants_save"
    JOB_EXTERNAL_PUSH = "job_external_push"
    JOB_ENQUEUE = "job_enqueue"
    JOB_DEQUEUE = "job_dequeue"
    JOB_REFINE_REQUEST = "job_refine_request"
    JOB_REFINE_REVERT = "job_refine_revert"
    JOB_PRIORITIZE = "job_prioritize"
    JOB_REORDER = "job_reorder"
    JOB_TEST_PURGE = "job_test_purge"
    QUEUE_PAUSE = "queue_pause"
    QUEUE_RESUME = "queue_resume"
    QUEUE_FORCE = "queue_force"

    SCHEDULE_WINDOW_CREATE = "schedule_window_create"
    SCHEDULE_WINDOW_MODIFY = "schedule_window_modify"
    SCHEDULE_WINDOW_DELETE = "schedule_window_delete"

    CONFIG_EDIT = "config_edit"
    AUDIT_EXPORT = "audit_export"

    USER_CREATE = "user_create"
    USER_MODIFY = "user_modify"
    USER_DELETE = "user_delete"

    GROUP_CREATE = "group_create"
    GROUP_MODIFY = "group_modify"
    GROUP_DELETE = "group_delete"
    GROUP_MEMBER_ADD = "group_member_add"
    GROUP_MEMBER_REMOVE = "group_member_remove"

    LEXICON_CREATE = "lexicon_create"
    LEXICON_MODIFY = "lexicon_modify"
    LEXICON_DELETE = "lexicon_delete"
    LEXICON_TERM_ADD = "lexicon_term_add"
    LEXICON_TERM_MODIFY = "lexicon_term_modify"
    LEXICON_TERM_DELETE = "lexicon_term_delete"
    LEXICON_IMPORT = "lexicon_import"
    LEXICON_EXPORT = "lexicon_export"
    LEXICON_SCOPE_CHANGE = "lexicon_scope_change"
    LEXICON_JOB_ASSIGN = "lexicon_job_assign"

    MEETING_TYPE_CREATE = "meeting_type_create"
    MEETING_TYPE_MODIFY = "meeting_type_modify"
    MEETING_TYPE_DELETE = "meeting_type_delete"
    MEETING_TYPE_SCOPE_CHANGE = "meeting_type_scope_change"
    MEETING_TYPE_IMPORT = "meeting_type_import"
    MEETING_TYPE_EXPORT = "meeting_type_export"

    VOICE_CREATE = "voice_create"
    VOICE_MODIFY = "voice_modify"
    VOICE_DELETE = "voice_delete"
    VOICE_CONSENT_VIEW = "voice_consent_view"

    MAINTENANCE_BACKUP_CREATE = "maintenance_backup_create"
    MAINTENANCE_BACKUP_RESTORE = "maintenance_backup_restore"

_VERB_FR = {
    "login": "Connexion", "login_failed": "Échec de connexion", "logout": "Déconnexion",
    "view": "Consultation", "download": "Téléchargement", "delete": "Suppression",
    "create": "Création", "modify": "Modification", "save": "Enregistrement",
    "enqueue": "Mise en file", "dequeue": "Sortie de file", "prioritize": "Priorisation",
    "reorder": "Réordonnancement", "purge": "Purge", "pause": "Pause", "resume": "Reprise",
    "force": "Forçage", "edit": "Édition", "export": "Export", "import": "Import",
    "add": "Ajout", "remove": "Retrait", "assign": "Affectation", "revert": "Restauration",
    "request": "Demande", "map": "Mappage", "push": "Envoi",
    "scope_change": "Changement de portée", "member_add": "Ajout de membre",
    "member_remove": "Retrait de membre", "srt_edit_save": "Édition de la transcription",
    "term_add": "Ajout de terme", "term_modify": "Modification de terme",
    "term_delete": "Suppression de terme", "context_save": "Enregistrement du contexte",
    "lexicon_save": "Enregistrement du lexique", "participants_save": "Enregistrement des participants",
    "speaker_map": "Mappage des locuteurs", "test_purge": "Purge des jobs de test",
    "window_create": "Création de créneau", "window_modify": "Modification de créneau",
    "window_delete": "Suppression de créneau", "refine_request": "Demande d'affinage",
    "refine_revert": "Restauration d'affinage", "external_push": "Envoi externe",
    "backup_create": "Création de sauvegarde", "backup_restore": "Restauration de sauvegarde",
}
_DOMAIN_FR = {
    "job": "traitement", "queue": "file", "schedule": "planification", "config": "configuration",
    "audit": "audit", "user": "utilisateur", "group": "groupe", "lexicon": "lexique",
    "meeting_type": "type de réunion", "voice": "voix", "maintenance": "maintenance",
}


def audit_action_label(value: str) -> str:
    """Libellé FR d'une action d'audit (le SLUG reste la valeur technique). Constat de
    revue visuelle C3.5 : la page affichait « meeting_type_create » brut."""
    if value in _VERB_FR:
        return _VERB_FR[value]
    # domaine_verbe : « meeting_type_create » → « Type de réunion — Création »
    for domain, dom_fr in sorted(_DOMAIN_FR.items(), key=lambda kv: -len(kv[0])):
        if value.startswith(domain + "_"):
            rest = value[len(domain) + 1:]
            verb = _VERB_FR.get(rest, rest.replace("_", " "))
            return f"{dom_fr.capitalize()} — {verb.lower()}"
    return value.replace("_", " ").capitalize()



class AuditLog(db.Model):
    __tablename__ = "audit_logs"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    timestamp = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True)
    # ON DELETE SET NULL : un journal d'audit doit survivre à la suppression d'un
    # compte (l'acteur est anonymisé, l'événement conservé).
    actor_id = db.Column(db.String(36), db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    actor_username = db.Column(db.String(80), nullable=False, default="system")
    action = db.Column(db.String(40), nullable=False, index=True)
    target_type = db.Column(db.String(20), nullable=False)
    target_id = db.Column(db.String(36), nullable=True, index=True)
    target_label = db.Column(db.String(255), nullable=False, default="")
    details_json = db.Column(db.Text, nullable=True)
    ip_address = db.Column(db.String(45), nullable=True)
    user_agent = db.Column(db.String(512), nullable=True)

    actor = db.relationship("User", foreign_keys=[actor_id])
