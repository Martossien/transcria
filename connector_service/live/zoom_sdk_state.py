"""Lecture de l'état du Meeting SDK Zoom — interprétation PURE, donc testable en CI.

Le SDK est une dépendance OPT-IN de ~275 Mo, x86_64 Linux seulement : rien de ce qui décide
ne doit en dépendre, sinon la logique ne serait vérifiable qu'au gate manuel. Ce module ne
manipule donc que des **noms de codes** (chaînes) et des types propres au projet ; la couche
mince de `zoom_sdk_transport` fait la traduction depuis les énumérations du SDK.

Trois responsabilités :
- traduire les codes `AUTHRET_*` en diagnostic ACTIONNABLE (que faire, et est-ce réessayable) ;
- traduire les `MEETING_STATUS_*` en phase du bot, motifs de sortie compris ;
- tenir le registre `identifiant de participant → nom affiché`, qui donne leur nom aux
  locuteurs — ce que le pilote navigateur ne pouvait pas faire.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ZoomSdkPhase(str, Enum):
    """Où en est le bot vis-à-vis de la réunion."""

    CONNECTING = "connecting"
    WAITING_FOR_HOST = "waiting_for_host"      # la réunion n'est pas ouverte
    IN_WAITING_ROOM = "in_waiting_room"        # l'hôte doit admettre
    ACTIVE = "active"                          # dans la réunion, média en cours
    RECONNECTING = "reconnecting"
    ENDED = "ended"                            # terminée, ou bot sorti
    FAILED = "failed"                          # échec d'entrée (mot de passe, verrou…)


@dataclass(frozen=True)
class AuthDiagnosis:
    """Verdict d'authentification : lisible par un humain, exploitable par le code."""

    ok: bool
    retryable: bool
    message: str


# Codes d'authentification du SDK. Les distinguer importe : un secret erroné ne se règle pas
# en réessayant, alors qu'un service occupé si. Sans cette distinction, un bot boucle sur une
# erreur définitive ou abandonne sur une erreur passagère.
_AUTH_DIAGNOSES: dict[str, AuthDiagnosis] = {
    "AUTHRET_SUCCESS": AuthDiagnosis(True, False, "authentification acceptée"),
    "AUTHRET_KEYORSECRETEMPTY": AuthDiagnosis(
        False, False, "Client ID ou Client Secret vide — vérifier la configuration du bot"),
    "AUTHRET_KEYORSECRETWRONG": AuthDiagnosis(
        False, False, "Client ID ou Client Secret refusé par Zoom — vérifier qu'ils "
                      "proviennent bien d'une app de type « Meeting SDK »"),
    "AUTHRET_JWTTOKENWRONG": AuthDiagnosis(
        False, False, "signature JWT refusée — charge utile ou horloge incorrecte "
                      "(exp doit valoir entre 30 min et 48 h après iat)"),
    "AUTHRET_ACCOUNTNOTSUPPORT": AuthDiagnosis(
        False, False, "ce compte Zoom ne permet pas l'usage du Meeting SDK"),
    "AUTHRET_ACCOUNTNOTENABLESDK": AuthDiagnosis(
        False, False, "le Meeting SDK n'est pas activé sur ce compte Zoom — à activer "
                      "sur le Marketplace"),
    "AUTHRET_CLIENT_INCOMPATIBLE": AuthDiagnosis(
        False, False, "version du SDK trop ancienne pour Zoom — mettre à jour "
                      "`zoom-meeting-sdk`"),
    "AUTHRET_LIMIT_EXCEEDED_EXCEPTION": AuthDiagnosis(
        False, True, "quota d'authentification dépassé — réessayer plus tard"),
    "AUTHRET_SERVICE_BUSY": AuthDiagnosis(
        False, True, "service Zoom occupé — réessayable"),
    "AUTHRET_OVERTIME": AuthDiagnosis(
        False, True, "délai d'authentification dépassé — réessayable"),
    "AUTHRET_NETWORKISSUE": AuthDiagnosis(
        False, True, "incident réseau pendant l'authentification — réessayable"),
    "AUTHRET_NONE": AuthDiagnosis(
        False, True, "authentification pas encore aboutie"),
}


def describe_auth_result(code_name: str) -> AuthDiagnosis:
    """Traduit un nom de code `AUTHRET_*` en diagnostic.

    Un code INCONNU est traité comme non réessayable : mieux vaut s'arrêter avec un message
    explicite que boucler indéfiniment sur une erreur qu'on ne sait pas lire.
    """
    known = _AUTH_DIAGNOSES.get(code_name)
    if known is not None:
        return known
    return AuthDiagnosis(False, False, f"code d'authentification Zoom inconnu : {code_name}")


# Statuts de réunion du SDK → phase du bot.
_STATUS_PHASES: dict[str, ZoomSdkPhase] = {
    "MEETING_STATUS_CONNECTING": ZoomSdkPhase.CONNECTING,
    "MEETING_STATUS_WAITINGFORHOST": ZoomSdkPhase.WAITING_FOR_HOST,
    "MEETING_STATUS_IN_WAITING_ROOM": ZoomSdkPhase.IN_WAITING_ROOM,
    "MEETING_STATUS_INMEETING": ZoomSdkPhase.ACTIVE,
    "MEETING_STATUS_RECONNECTING": ZoomSdkPhase.RECONNECTING,
    "MEETING_STATUS_DISCONNECTING": ZoomSdkPhase.ENDED,
    "MEETING_STATUS_ENDED": ZoomSdkPhase.ENDED,
    "MEETING_STATUS_IDLE": ZoomSdkPhase.ENDED,
    "MEETING_STATUS_FAILED": ZoomSdkPhase.FAILED,
    # Le bot reste dans la réunion : promotion/rétrogradation en webinaire et passage en
    # sous-salle ne changent pas sa capacité à capter.
    "MEETING_STATUS_WEBINAR_PROMOTE": ZoomSdkPhase.ACTIVE,
    "MEETING_STATUS_WEBINAR_DEPROMOTE": ZoomSdkPhase.ACTIVE,
    "MEETING_STATUS_JOIN_BREAKOUT_ROOM": ZoomSdkPhase.ACTIVE,
    "MEETING_STATUS_LEAVE_BREAKOUT_ROOM": ZoomSdkPhase.ACTIVE,
}

# Statuts qui sont des NOTIFICATIONS, pas des phases : le verrouillage d'une réunion est
# annoncé alors que le bot est déjà dedans. Les faire retomber sur une phase par défaut lui
# ferait croire qu'il a quitté la réunion — et déclencherait une sortie injustifiée.
_STATUS_WITHOUT_PHASE_CHANGE = frozenset({
    "MEETING_STATUS_LOCKED",
    "MEETING_STATUS_UNLOCKED",
    "MEETING_STATUS_UNKNOWN",
})


def interpret_meeting_status(status_name: str,
                            current: ZoomSdkPhase = ZoomSdkPhase.CONNECTING) -> ZoomSdkPhase:
    """Traduit un nom de statut `MEETING_STATUS_*` en phase du bot.

    `current` est la phase en cours : un statut qui ne dit rien sur la phase (verrouillage,
    statut inconnu d'une version future du SDK) la laisse INCHANGÉE. C'est le comportement
    prudent — conclure à tort qu'on a quitté la réunion coûte une transcription.
    """
    if status_name in _STATUS_WITHOUT_PHASE_CHANGE:
        return current
    return _STATUS_PHASES.get(status_name, current)


# Motifs de sortie, alignés sur ceux du bot navigateur pour que l'aval ne distingue pas les
# plateformes (`conference_ended`, `removed`, `alone`, `max_duration`, `stopped`…).
def exit_reason(phase: ZoomSdkPhase, *, was_active: bool) -> str:
    """Motif de sortie à partir de la phase terminale atteinte.

    `was_active` change le sens de ENDED : jamais entré = échec d'entrée ; entré puis sorti =
    réunion terminée. Confondre les deux rendrait les journaux inexploitables.
    """
    if phase is ZoomSdkPhase.FAILED:
        return "join_failed"
    if phase is ZoomSdkPhase.ENDED:
        return "conference_ended" if was_active else "join_failed"
    return phase.value


# --------------------------------------------------------------------------- #
#  Permission d'enregistrement local — condition d'accès à l'audio brut
# --------------------------------------------------------------------------- #
# Zoom ne délivre l'audio brut qu'à un participant qui a l'un de ces droits : hôte, co-hôte,
# « autoriser l'enregistrement local », ou un jeton d'enregistrement local. L'ancienne licence
# « raw data » n'existe plus (confirmé par Zoom) : le compte de l'hôte peut donc être GRATUIT.
# En revanche le JETON d'enregistrement local, lui, ne fonctionne PAS sur un compte gratuit —
# sur ce type de compte, l'hôte doit accorder la permission À LA MAIN, en séance.
class RecordingPermission(str, Enum):
    """Où en est le bot vis-à-vis du droit de capter l'audio brut."""

    GRANTED = "granted"            # déjà autorisé (hôte, co-hôte, ou droit accordé)
    MUST_ASK = "must_ask"          # pas autorisé, mais on peut le DEMANDER à l'hôte
    UNAVAILABLE = "unavailable"    # ni autorisé ni demandable — inutile d'insister


def interpret_raw_recording_readiness(can_start_code: str, *, can_request: bool) -> RecordingPermission:
    """Traduit `CanStartRawRecording()` (+ `IsSupportRequestLocalRecordingPrivilege()`).

    Distinction qui compte : sans elle, un bot dépourvu du droit s'abonne « avec succès » et
    ne reçoit JAMAIS de frame — panne muette, la pire à diagnostiquer en réunion.
    """
    if can_start_code == "SDKERR_SUCCESS":
        return RecordingPermission.GRANTED
    if can_start_code == "SDKERR_NO_PERMISSION" and can_request:
        return RecordingPermission.MUST_ASK
    return RecordingPermission.UNAVAILABLE


def describe_privilege_outcome(status_name: str) -> tuple[bool, str]:
    """Réponse de l'hôte à la demande de droit → (accordé ?, message exploitable)."""
    outcomes = {
        "RequestLocalRecording_Granted": (
            True, "l'hôte a autorisé l'enregistrement — capture de l'audio brut possible"),
        "RequestLocalRecording_Denied": (
            False, "l'hôte a REFUSÉ l'autorisation d'enregistrement : sans elle, Zoom ne "
                   "délivre aucun audio brut"),
        "RequestLocalRecording_Timeout": (
            False, "l'hôte n'a pas répondu à la demande d'autorisation d'enregistrement "
                   "(fenêtre « Autoriser l'enregistrement » à accepter dans la réunion)"),
    }
    return outcomes.get(
        status_name,
        (False, f"réponse inconnue à la demande d'enregistrement : {status_name}"))


@dataclass(frozen=True)
class Participant:
    """Participant tel que le bot le connaît."""

    node_id: int
    name: str
    is_bot: bool = False


class ParticipantRegistry:
    """Registre `identifiant de nœud → nom affiché`, alimenté par les événements du SDK.

    POURQUOI UN REGISTRE, et pas une résolution à la volée : les frames audio arrivent toutes
    les 10-20 ms par participant et ne portent QUE l'identifiant de nœud. Interroger le SDK à
    chaque frame serait absurde ; ne résoudre qu'une fois, à l'arrivée de la piste, laissait
    en revanche les RENOMMAGES en cours de réunion invisibles (limite connue du bot
    navigateur). Ce registre garde donc la correspondance et accepte de la RAFRAÎCHIR.

    Le bot lui-même est marqué : il ne doit ni être compté comme interlocuteur, ni voir sa
    propre entrée transcrite.
    """

    def __init__(self) -> None:
        self._by_node: dict[int, Participant] = {}
        self._self_node_id: int | None = None

    def remember(self, node_id: int, name: str, *, is_bot: bool = False) -> None:
        """Enregistre ou MET À JOUR un participant. Un nom vide n'écrase pas un nom connu :
        le SDK publie parfois l'identifiant avant que le nom soit disponible."""
        previous = self._by_node.get(node_id)
        resolved = name or (previous.name if previous else "")
        self._by_node[node_id] = Participant(node_id, resolved, is_bot)
        if is_bot:
            self._self_node_id = node_id

    def forget(self, node_id: int) -> None:
        """Retire un participant parti. On garde le nom NULLE PART ailleurs : les frames en
        vol portant encore cet identifiant retomberont sur le repli d'affichage."""
        self._by_node.pop(node_id, None)
        if self._self_node_id == node_id:
            self._self_node_id = None

    def name_of(self, node_id: int) -> str:
        """Nom affiché, ou repli lisible. Ne renvoie JAMAIS de chaîne vide : un segment sans
        locuteur identifiable doit rester rattachable à un flux."""
        known = self._by_node.get(node_id)
        if known is not None and known.name:
            return known.name
        return f"participant-{node_id}"

    def is_self(self, node_id: int) -> bool:
        """L'identifiant est-il celui du bot ? Sert à ne pas se transcrire soi-même."""
        return self._self_node_id is not None and node_id == self._self_node_id

    def others(self) -> list[Participant]:
        """Participants HORS bot — c'est ce comptage qui dit si le bot est resté seul."""
        return [p for p in self._by_node.values() if not p.is_bot]

    def alone(self) -> bool:
        """Le bot est-il seul ? Signal de fin de réunion le plus fiable côté SDK, la liste
        des participants étant interrogeable (contrairement au client Web)."""
        return not self.others()

    def replace_all(self, participants: list[Participant]) -> None:
        """Remplace l'état par un instantané complet issu de `GetParticipantsList()`.

        Utile au RATTRAPAGE : le bot arrive après les participants déjà présents, et les
        événements d'arrivée ne sont pas rejoués pour eux. Sert aussi à corriger une dérive
        si un événement a été manqué.
        """
        self_id = self._self_node_id
        self._by_node = {p.node_id: p for p in participants}
        # `is_bot` peut ne pas être renseigné dans l'instantané : on ne perd pas l'information.
        if self_id is not None and self_id in self._by_node:
            known = self._by_node[self_id]
            self._by_node[self_id] = Participant(known.node_id, known.name, is_bot=True)
            self._self_node_id = self_id
        else:
            self._self_node_id = next(
                (p.node_id for p in participants if p.is_bot), None)
