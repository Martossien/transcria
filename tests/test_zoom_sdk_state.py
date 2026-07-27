"""Décision du bot Zoom (SDK natif) — interprétation des codes et registre des locuteurs.

Le SDK est une dépendance opt-in de ~275 Mo, x86_64 Linux seulement : ces tests ne l'importent
donc PAS. Ils portent sur les noms de codes (chaînes), ce qui est précisément pourquoi la
décision a été isolée dans un module pur — sinon rien de tout cela ne serait vérifiable en CI.
"""
from __future__ import annotations

import pytest

from connector_service.live.zoom_sdk_state import (
    Participant,
    ParticipantRegistry,
    RecordingPermission,
    ZoomSdkPhase,
    describe_auth_result,
    describe_failed_admission,
    describe_privilege_outcome,
    exit_reason,
    interpret_meeting_status,
    interpret_raw_recording_readiness,
)


# --------------------------------------------------------------------------- #
#  Authentification
# --------------------------------------------------------------------------- #
def test_succes_authentification():
    diagnosis = describe_auth_result("AUTHRET_SUCCESS")
    assert diagnosis.ok and not diagnosis.retryable


@pytest.mark.parametrize("code", [
    "AUTHRET_KEYORSECRETEMPTY",
    "AUTHRET_KEYORSECRETWRONG",
    "AUTHRET_JWTTOKENWRONG",
    "AUTHRET_ACCOUNTNOTSUPPORT",
    "AUTHRET_ACCOUNTNOTENABLESDK",
    "AUTHRET_CLIENT_INCOMPATIBLE",
])
def test_erreurs_definitives_non_reessayables(code):
    """Distinction essentielle : réessayer sur un secret erroné fait boucler le bot
    indéfiniment sans jamais aboutir."""
    diagnosis = describe_auth_result(code)
    assert not diagnosis.ok
    assert not diagnosis.retryable
    assert diagnosis.message


@pytest.mark.parametrize("code", [
    "AUTHRET_SERVICE_BUSY",
    "AUTHRET_OVERTIME",
    "AUTHRET_NETWORKISSUE",
    "AUTHRET_LIMIT_EXCEEDED_EXCEPTION",
    "AUTHRET_NONE",
])
def test_erreurs_passageres_reessayables(code):
    """Et l'inverse : abandonner sur un incident réseau perdrait une réunion entière."""
    diagnosis = describe_auth_result(code)
    assert not diagnosis.ok
    assert diagnosis.retryable


def test_code_inconnu_traite_comme_definitif():
    """Un code d'une version future ne doit PAS faire boucler le bot : on s'arrête avec un
    message qui nomme le code, ce qui rend le diagnostic possible."""
    diagnosis = describe_auth_result("AUTHRET_QUELQUE_CHOSE_DE_NOUVEAU")
    assert not diagnosis.ok
    assert not diagnosis.retryable
    assert "AUTHRET_QUELQUE_CHOSE_DE_NOUVEAU" in diagnosis.message


def test_le_jwt_errone_est_bien_diagnostique():
    """Verrou sur le code RÉELLEMENT observé en exécution (JWT signé avec un faux secret) :
    c'est celui qu'un utilisateur rencontrera en premier s'il se trompe d'identifiants."""
    diagnosis = describe_auth_result("AUTHRET_JWTTOKENWRONG")
    assert not diagnosis.ok
    assert "signature" in diagnosis.message.lower()


# --------------------------------------------------------------------------- #
#  Statut de réunion → phase
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status, attendu", [
    ("MEETING_STATUS_CONNECTING", ZoomSdkPhase.CONNECTING),
    ("MEETING_STATUS_WAITINGFORHOST", ZoomSdkPhase.WAITING_FOR_HOST),
    ("MEETING_STATUS_IN_WAITING_ROOM", ZoomSdkPhase.IN_WAITING_ROOM),
    ("MEETING_STATUS_INMEETING", ZoomSdkPhase.ACTIVE),
    ("MEETING_STATUS_RECONNECTING", ZoomSdkPhase.RECONNECTING),
    ("MEETING_STATUS_ENDED", ZoomSdkPhase.ENDED),
    ("MEETING_STATUS_DISCONNECTING", ZoomSdkPhase.ENDED),
    ("MEETING_STATUS_IDLE", ZoomSdkPhase.ENDED),
    ("MEETING_STATUS_FAILED", ZoomSdkPhase.FAILED),
])
def test_statuts_traduits_en_phases(status, attendu):
    assert interpret_meeting_status(status) is attendu


@pytest.mark.parametrize("status", [
    "MEETING_STATUS_LOCKED",
    "MEETING_STATUS_UNLOCKED",
    "MEETING_STATUS_UNKNOWN",
])
def test_notifications_ne_changent_pas_la_phase(status):
    """Le verrouillage d'une réunion est annoncé ALORS QUE le bot est dedans. Le traiter comme
    une phase le ferait sortir d'une réunion en cours — la transcription serait tronquée."""
    assert interpret_meeting_status(status, ZoomSdkPhase.ACTIVE) is ZoomSdkPhase.ACTIVE
    assert interpret_meeting_status(status, ZoomSdkPhase.IN_WAITING_ROOM) \
        is ZoomSdkPhase.IN_WAITING_ROOM


def test_statut_inconnu_preserve_la_phase_courante():
    """Prudence face aux versions futures du SDK : on ne conclut rien d'un statut qu'on ne
    connaît pas."""
    assert interpret_meeting_status("MEETING_STATUS_DEMAIN", ZoomSdkPhase.ACTIVE) \
        is ZoomSdkPhase.ACTIVE


@pytest.mark.parametrize("statuts, attendu", [
    # Parcours nominal : connexion → salle d'attente → dans la réunion.
    (["MEETING_STATUS_CONNECTING", "MEETING_STATUS_IN_WAITING_ROOM",
      "MEETING_STATUS_INMEETING"], ZoomSdkPhase.ACTIVE),
    # Réunion verrouillée puis terminée alors qu'on était dedans.
    (["MEETING_STATUS_INMEETING", "MEETING_STATUS_LOCKED",
      "MEETING_STATUS_ENDED"], ZoomSdkPhase.ENDED),
    # Coupure réseau au milieu : on repasse ACTIVE après reconnexion.
    (["MEETING_STATUS_INMEETING", "MEETING_STATUS_RECONNECTING",
      "MEETING_STATUS_INMEETING"], ZoomSdkPhase.ACTIVE),
])
def test_sequences_realistes(statuts, attendu):
    """Les statuts arrivent en SÉRIE : ce qui compte est l'état après enchaînement, pas
    chaque traduction isolée."""
    phase = ZoomSdkPhase.CONNECTING
    for status in statuts:
        phase = interpret_meeting_status(status, phase)
    assert phase is attendu


# --------------------------------------------------------------------------- #
#  Motif de sortie
# --------------------------------------------------------------------------- #
def test_fin_apres_entree_est_une_reunion_terminee():
    assert exit_reason(ZoomSdkPhase.ENDED, was_active=True) == "conference_ended"


def test_fin_sans_jamais_entrer_est_un_echec_d_entree():
    """Confondre les deux rendrait les journaux inexploitables : « réunion terminée » alors
    que le bot n'a jamais réussi à entrer envoie chercher au mauvais endroit."""
    assert exit_reason(ZoomSdkPhase.ENDED, was_active=False) == "join_failed"
    assert exit_reason(ZoomSdkPhase.FAILED, was_active=False) == "join_failed"


def test_echec_prime_meme_apres_activite():
    assert exit_reason(ZoomSdkPhase.FAILED, was_active=True) == "join_failed"


# --------------------------------------------------------------------------- #
#  Registre des participants
# --------------------------------------------------------------------------- #
def test_nom_resolu_puis_utilise():
    registry = ParticipantRegistry()
    registry.remember(42, "Alice")
    assert registry.name_of(42) == "Alice"


def test_participant_inconnu_a_un_repli_lisible():
    """Ne JAMAIS rendre une chaîne vide : un segment sans locuteur identifiable doit rester
    rattachable à un flux, sinon il devient inexploitable en aval."""
    assert ParticipantRegistry().name_of(7) == "participant-7"


def test_renommage_en_cours_de_reunion_pris_en_compte():
    """C'est la limite que le pilote navigateur ne savait pas lever : le nom était résolu une
    fois, à l'arrivée de la piste, et un renommage restait invisible."""
    registry = ParticipantRegistry()
    registry.remember(42, "Invité")
    registry.remember(42, "Alice Martin")
    assert registry.name_of(42) == "Alice Martin"


def test_nom_vide_n_ecrase_pas_un_nom_connu():
    """Le SDK publie parfois l'identifiant avant que le nom soit disponible : accepter ce
    vide ferait REGRESSER un nom déjà résolu."""
    registry = ParticipantRegistry()
    registry.remember(42, "Alice")
    registry.remember(42, "")
    assert registry.name_of(42) == "Alice"


def test_depart_oublie_le_participant():
    registry = ParticipantRegistry()
    registry.remember(42, "Alice")
    registry.forget(42)
    assert registry.name_of(42) == "participant-42"
    assert registry.alone()


def test_le_bot_ne_se_compte_pas_lui_meme():
    """Sans cette exclusion, le bot serait « accompagné » par lui-même et ne détecterait
    jamais qu'il est resté seul dans la réunion."""
    registry = ParticipantRegistry()
    registry.remember(1, "TranscrIA", is_bot=True)
    assert registry.alone()
    assert registry.is_self(1)
    assert not registry.is_self(2)

    registry.remember(2, "Alice")
    assert not registry.alone()
    assert [p.name for p in registry.others()] == ["Alice"]


def test_depart_du_bot_oublie_son_identite():
    registry = ParticipantRegistry()
    registry.remember(1, "TranscrIA", is_bot=True)
    registry.forget(1)
    assert not registry.is_self(1)


def test_rattrapage_remplace_l_etat_complet():
    """Le bot arrive APRÈS les participants déjà présents, et leurs événements d'arrivée ne
    sont pas rejoués : sans ce rattrapage, leurs noms resteraient inconnus toute la réunion."""
    registry = ParticipantRegistry()
    registry.remember(99, "Fantôme")
    registry.replace_all([Participant(1, "TranscrIA", is_bot=True),
                          Participant(2, "Alice"), Participant(3, "Bob")])
    assert registry.name_of(99) == "participant-99"        # l'ancien état a disparu
    assert sorted(p.name for p in registry.others()) == ["Alice", "Bob"]
    assert registry.is_self(1)


def test_rattrapage_conserve_l_identite_du_bot_absente_de_l_instantane():
    """`GetParticipantsList` peut ne pas marquer le bot : perdre cette information le ferait
    se transcrire lui-même."""
    registry = ParticipantRegistry()
    registry.remember(1, "TranscrIA", is_bot=True)
    registry.replace_all([Participant(1, "TranscrIA"), Participant(2, "Alice")])
    assert registry.is_self(1)
    assert [p.name for p in registry.others()] == ["Alice"]


# --------------------------------------------------------------------------- #
#  Droit d'enregistrement — condition d'accès à l'audio brut
# --------------------------------------------------------------------------- #
def test_droit_deja_acquis():
    """Hôte, co-hôte, ou droit déjà accordé : rien à demander."""
    assert interpret_raw_recording_readiness("SDKERR_SUCCESS", can_request=False) \
        is RecordingPermission.GRANTED


def test_droit_absent_mais_demandable():
    """Cas COURANT sur un compte gratuit : le jeton d'enregistrement local n'y fonctionne
    pas, donc la seule voie est la demande à l'hôte en séance."""
    assert interpret_raw_recording_readiness("SDKERR_NO_PERMISSION", can_request=True) \
        is RecordingPermission.MUST_ASK


def test_droit_absent_et_non_demandable():
    """Insister n'aurait aucun sens : mieux vaut échouer avec un remède à proposer."""
    assert interpret_raw_recording_readiness("SDKERR_NO_PERMISSION", can_request=False) \
        is RecordingPermission.UNAVAILABLE


@pytest.mark.parametrize("code", ["SDKERR_WRONG_USAGE", "SDKERR_NO_IMPL", ""])
def test_autre_erreur_est_bloquante(code):
    """Un code qui n'est ni « autorisé » ni « pas la permission » ne se rattrape pas en
    demandant : sans cette prudence, le bot boucle sur une demande sans objet."""
    assert interpret_raw_recording_readiness(code, can_request=True) \
        is RecordingPermission.UNAVAILABLE


def test_hote_accorde():
    granted, message = describe_privilege_outcome("RequestLocalRecording_Granted")
    assert granted and "autoris" in message.lower()


@pytest.mark.parametrize("status", [
    "RequestLocalRecording_Denied",
    "RequestLocalRecording_Timeout",
])
def test_refus_et_absence_de_reponse_sont_distingues(status):
    """Les deux bloquent, mais le message doit dire LEQUEL : « refusé » et « pas répondu »
    n'appellent pas la même action de l'exploitant."""
    granted, message = describe_privilege_outcome(status)
    assert not granted
    assert message and "enregistrement" in message.lower()


def test_messages_de_refus_et_de_silence_different():
    _, refuse = describe_privilege_outcome("RequestLocalRecording_Denied")
    _, silence = describe_privilege_outcome("RequestLocalRecording_Timeout")
    assert refuse != silence


def test_statut_inconnu_ne_passe_pas_pour_un_accord():
    """Prudence : un statut inattendu ne doit JAMAIS être lu comme une autorisation, sinon
    le bot capterait le vide en croyant avoir le droit."""
    granted, message = describe_privilege_outcome("RequestLocalRecording_Demain")
    assert not granted
    assert "RequestLocalRecording_Demain" in message


# --------------------------------------------------------------------------- #
#  Message d'échec d'entrée — dire POURQUOI
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("phase, attendu", [
    (ZoomSdkPhase.CONNECTING, "pas ouverte"),
    (ZoomSdkPhase.WAITING_FOR_HOST, "démarré"),
    (ZoomSdkPhase.IN_WAITING_ROOM, "SALLE D'ATTENTE"),
    (ZoomSdkPhase.FAILED, "code secret"),
    (ZoomSdkPhase.ENDED, "terminée"),
])
def test_chaque_echec_d_entree_nomme_son_remede(phase, attendu):
    """La phase atteinte est une donnée INTERNE, pas un diagnostic. Les causes possibles
    appellent des gestes différents — ouvrir la réunion, admettre le bot, corriger le code —
    et c'est ce geste que le message doit nommer."""
    message = describe_failed_admission(phase, timeout_s=300, reason="join_failed")
    assert attendu in message
    assert "join_failed" in message          # le motif machine reste présent pour l'aval


def test_le_delai_apparait_dans_le_message():
    message = describe_failed_admission(ZoomSdkPhase.IN_WAITING_ROOM,
                                        timeout_s=45, reason="in_waiting_room")
    assert "45" in message


def test_phase_inattendue_reste_lisible():
    message = describe_failed_admission(ZoomSdkPhase.RECONNECTING,
                                        timeout_s=10, reason="reconnecting")
    assert "reconnecting" in message
