"""Transport LIVE Zoom par le Meeting SDK NATIF — dep OPT-IN `zoom-meeting-sdk`, gate manuel.

Voie RECOMMANDÉE PAR ZOOM pour un bot headless Linux, en remplacement du pilote navigateur
(`bot/platforms/zoom_web.py`) que le client Web refuse d'exécuter automatiquement (reCAPTCHA
constaté au gate). Ce n'est pas un contournement : c'est la porte d'entrée prévue.

Ce que ce transport apporte et que le navigateur ne pouvait pas donner :
- l'audio arrive PAR PARTICIPANT avec son identifiant de nœud, donc les locuteurs sont NOMMÉS ;
- l'entrée est silencieuse NATIVEMENT (`isAudioOff`/`isVideoOff`) — aucun micro n'est ouvert ;
- la liste des participants est interrogeable, donc « le bot est-il seul ? » devient un fait
  et non une heuristique de page.

RÉPARTITION DÉLIBÉRÉE, comme pour `livekit_transport` :
- les fonctions PURES (`join_fields`, `audio_frame_to_demuxed`) et toute la décision
  (`zoom_sdk_state`) sont testées en CI, sans le SDK ;
- `zoom_sdk_demux_source` est la glue : elle n'enchaîne que des appels au SDK et est confirmée
  au gate manuel (`scripts/gate_bot_zoom_sdk.py`).

⚠ TROIS PIÈGES, tous rencontrés et vérifiés en exécution réelle :

1. **Durée de vie des objets de rappels.** `SetEvent()` / `subscribe()` ne conservent qu'un
   POINTEUR BRUT. Passer l'objet en temporaire le fait libérer par Python alors que le SDK
   s'en sert encore → **segfault**, sans message. Tous les objets de rappels sont donc
   retenus dans `_Retained` pour la durée de la session. C'est la cause du premier plantage
   observé, et rien dans l'API ne l'indique.

2. **`InitParam` doit être rempli.** Les champs chaîne laissés nuls sont déréférencés plus
   tard par le SDK.

3. **Environnement natif.** Le SDK est un client Zoom complet : sans D-Bus ni PulseAudio il
   plante par segfault au lieu de renvoyer une erreur (cf. `docker/zoom_sdk_entrypoint.sh`).

À brancher : `DemuxFrameSource(zoom_sdk_demux_source(...))` → `LiveAudioProvider("zoom", …)`.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from connector_service.contract import ExternalMeetingOccurrence
from connector_service.live._demux import DemuxedFrame
from connector_service.live.glib_loop import GLibPump
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
from connector_service.signatures import ROLE_PARTICIPANT, zoom_meeting_sdk_signature

logger = logging.getLogger(__name__)

# Débits que le SDK sait produire (il n'expose PAS 16 kHz). 32 kHz suffit largement à la
# parole et divise par 1,5 le volume à transporter par rapport à 48 kHz ; le rééchantillonnage
# vers ce qu'attend le moteur STT se fait en aval, où il est déjà nécessaire pour les autres
# plateformes (Meet livre ~48 kHz).
SAMPLING_RATE_32K = 32000
SAMPLING_RATE_48K = 48000


class ZoomSdkError(RuntimeError):
    """Échec côté SDK Zoom (init, authentification, entrée en réunion)."""


@dataclass
class _Retained:
    """Objets que le SDK référence par POINTEUR BRUT et que Python ne doit pas libérer.

    Sans cette rétention, le ramasse-miettes détruit les objets de rappels dès la fin de
    l'expression qui les crée, et le SDK écrit dans de la mémoire libérée. Le symptôme est un
    segfault sans trace exploitable — c'est le premier plantage qu'a produit ce transport.
    """

    objects: list[Any] = field(default_factory=list)

    def keep(self, obj: Any) -> Any:
        """Retient `obj` et le rend, pour s'écrire en une seule expression."""
        self.objects.append(obj)
        return obj


def join_fields(meeting_number: str, *, display_name: str, passcode: str = "",
                sampling_rate_hz: int = SAMPLING_RATE_32K,
                zak: str = "", on_behalf_token: str = "",
                join_token: str = "") -> dict[str, Any]:
    """Champs de `JoinParam4WithoutLogin` — fonction PURE, donc testable sans le SDK.

    `isAudioOff`/`isVideoOff` sont le cœur du sujet : le bot est un AUDITEUR. Sur Jitsi il
    avait fallu neutraliser la capture par des réglages détournés, après qu'un bip a été
    entendu dans une vraie réunion ; ici c'est un paramètre d'entrée.

    `isMyVoiceInMix` est faux : le bot ne publie rien, l'inclure n'aurait aucun sens et
    polluerait le flux mixé.

    `zak` / `on_behalf_token` restent VIDES pour une réunion du compte propriétaire de l'app —
    c'est le régime qui dispense de revue Zoom. Ils ne servent qu'aux réunions externes, qui
    exigent en plus une app revue par Zoom (durci depuis mars 2026).
    """
    if not display_name:
        raise ValueError("nom affiché du bot requis — Zoom refuse une entrée anonyme")
    if sampling_rate_hz not in (SAMPLING_RATE_32K, SAMPLING_RATE_48K):
        raise ValueError(
            f"débit non géré par le SDK Zoom : {sampling_rate_hz} Hz "
            f"(attendu {SAMPLING_RATE_32K} ou {SAMPLING_RATE_48K})")
    return {
        "meetingNumber": int(meeting_number),
        "userName": display_name,
        "psw": passcode,
        "isAudioOff": True,
        "isVideoOff": True,
        "isMyVoiceInMix": False,
        "isAudioRawDataStereo": False,       # la parole est mono ; le stéréo doublerait le volume
        "userZAK": zak,
        "onBehalfToken": on_behalf_token,
        "join_token": join_token,
        "_sampling_rate_hz": sampling_rate_hz,   # appliqué séparément (énumération du SDK)
    }


def audio_frame_to_demuxed(buffer: bytes, *, node_id: int, name: str,
                           sample_rate_hz: int, channels: int) -> DemuxedFrame | None:
    """`AudioRawData` → `DemuxedFrame`. PURE, donc testable sans le SDK.

    Rend `None` pour une frame vide : le SDK en émet pendant les silences et les transitions,
    et les laisser passer ferait compter des frames sans audio (erreur déjà commise sur un
    gate, où un flux « qui coule » ne transportait que des zéros).
    """
    if not buffer:
        return None
    return DemuxedFrame(
        participant_id=str(node_id),
        payload=buffer,
        sample_rate_hz=sample_rate_hz,
        channels=channels or 1,
        participant_name=name,
        track_id=f"zoom-node-{node_id}",
    )


def zoom_sdk_demux_source(
    client_id: str, client_secret: str, meeting_number: str, *,
    display_name: str = "TranscrIA",
    passcode: str = "",
    sampling_rate_hz: int = SAMPLING_RATE_32K,
    zak: str = "",
    on_behalf_token: str = "",
    join_token: str = "",
    admission_timeout_s: float = 300.0,
    auth_timeout_s: float = 60.0,
    recording_permission_timeout_s: float = 120.0,
    max_queued_frames: int = 2000,
    web_domain: str = "https://zoom.us",
    on_phase: Callable[[ZoomSdkPhase], None] | None = None,
) -> Callable[[ExternalMeetingOccurrence], AsyncIterator[DemuxedFrame]]:
    """Source démuxée Zoom réelle (dep opt-in `zoom-meeting-sdk`). NON testée en CI.

    `admission_timeout_s` vaut 5 min par défaut : une salle d'attente exige une action de
    l'hôte, et abandonner au bout de quelques secondes rendrait le bot inutilisable en
    pratique.

    `recording_permission_timeout_s` couvre le même genre d'attente : sur un compte GRATUIT,
    l'hôte doit accepter À LA MAIN une fenêtre « Autoriser l'enregistrement » — le jeton
    d'enregistrement local, qui automatise cela, ne fonctionne pas sur ce type de compte.

    `max_queued_frames` borne la file entre les rappels du SDK et la boucle asyncio. Sans
    borne, un moteur STT qui ralentit ferait croître la mémoire pendant toute la réunion ;
    au-delà, on écarte les frames les PLUS ANCIENNES et on le journalise — un direct qui
    retarde de dix minutes n'a plus d'intérêt, alors qu'un trou signalé reste exploitable.
    """
    def _factory(occurrence: ExternalMeetingOccurrence) -> AsyncIterator[DemuxedFrame]:
        async def _open() -> AsyncIterator[DemuxedFrame]:
            import zoom_meeting_sdk as zoom  # dép opt-in

            loop = asyncio.get_running_loop()
            retained = _Retained()
            registry = ParticipantRegistry()
            frames: asyncio.Queue[DemuxedFrame | None] = asyncio.Queue()
            phase = ZoomSdkPhase.CONNECTING
            was_active = False
            dropped = 0
            stop = asyncio.Event()
            phase_changed = asyncio.Event()

            def _set_phase(new_phase: ZoomSdkPhase) -> None:
                nonlocal phase, was_active
                if new_phase is phase:
                    return
                phase = new_phase
                if new_phase is ZoomSdkPhase.ACTIVE:
                    was_active = True
                logger.info("Zoom SDK : phase → %s", new_phase.value)
                if on_phase is not None:
                    on_phase(new_phase)
                phase_changed.set()

            # --- Initialisation du SDK ------------------------------------------------ #
            # Tous les champs chaîne sont renseignés : laissés nuls, ils sont déréférencés
            # plus tard par le SDK (cf. piège nº 2 de l'en-tête).
            init = zoom.InitParam()
            init.strWebDomain = web_domain
            init.strSupportUrl = web_domain
            init.strBrandingName = display_name
            init.emLanguageID = zoom.SDK_LANGUAGE_ID.LANGUAGE_English
            init.enableLogByDefault = True
            init.uiLogFileSize = 5
            err = zoom.InitSDK(init)
            if err != zoom.SDKError.SDKERR_SUCCESS:
                raise ZoomSdkError(f"InitSDK a échoué : {err}")

            pump = GLibPump()
            pump_task = asyncio.ensure_future(pump.run(stop))
            meeting: Any = None          # nécessaire au nettoyage même si l'entrée échoue
            started_raw_recording = False

            try:
                # --- Authentification ------------------------------------------------ #
                auth_service = retained.keep(zoom.CreateAuthService())
                auth_outcome: dict[str, Any] = {}
                auth_done = asyncio.Event()

                def _on_auth(result: Any) -> None:
                    auth_outcome["diagnosis"] = describe_auth_result(
                        getattr(result, "name", str(result)))
                    loop.call_soon_threadsafe(auth_done.set)

                auth_service.SetEvent(retained.keep(zoom.AuthServiceEventCallbacks(
                    onAuthenticationReturnCallback=_on_auth)))

                context = zoom.AuthContext()
                context.jwt_token = zoom_meeting_sdk_signature(
                    client_id, client_secret, meeting_number, role=ROLE_PARTICIPANT)
                err = auth_service.SDKAuth(context)
                if err != zoom.SDKError.SDKERR_SUCCESS:
                    raise ZoomSdkError(f"SDKAuth a échoué : {err}")

                try:
                    await asyncio.wait_for(auth_done.wait(), timeout=auth_timeout_s)
                except asyncio.TimeoutError as exc:
                    raise ZoomSdkError(
                        f"pas de réponse d'authentification en {auth_timeout_s:.0f} s") from exc

                diagnosis = auth_outcome["diagnosis"]
                if not diagnosis.ok:
                    raise ZoomSdkError(f"authentification Zoom refusée : {diagnosis.message}")
                logger.info("Zoom SDK : %s", diagnosis.message)

                # --- Service de réunion et suivi d'état ------------------------------ #
                meeting = retained.keep(zoom.CreateMeetingService())

                def _on_status(status: Any, _result: int) -> None:
                    name = getattr(status, "name", str(status))
                    loop.call_soon_threadsafe(
                        _set_phase, interpret_meeting_status(name, phase))

                meeting.SetEvent(retained.keep(zoom.MeetingServiceEventCallbacks(
                    onMeetingStatusChangedCallback=_on_status)))

                # --- Registre des participants -------------------------------------- #
                participants_ctrl = meeting.GetMeetingParticipantsController()

                def _remember(node_id: int) -> None:
                    """Résout le nom d'un participant et le retient."""
                    info = participants_ctrl.GetUserByUserID(node_id)
                    if info is None:
                        registry.remember(node_id, "")
                        return
                    registry.remember(node_id, info.GetUserName() or "",
                                      is_bot=bool(info.IsMySelf()))

                # Signatures relevées sur les bindings : (Sequence[int], str) — la chaîne est
                # un libellé de sous-salle, sans usage ici.
                def _on_user_join(node_ids: Any, _room: str = "") -> None:
                    for node_id in list(node_ids or []):
                        _remember(int(node_id))

                def _on_user_left(node_ids: Any, _room: str = "") -> None:
                    for node_id in list(node_ids or []):
                        registry.forget(int(node_id))

                def _on_names_changed(node_ids: Any) -> None:
                    # Un participant qui se RENOMME en cours de réunion : sans ce rappel, les
                    # segments suivants garderaient l'ancien nom — limite que le pilote
                    # navigateur ne savait pas lever.
                    for node_id in list(node_ids or []):
                        _remember(int(node_id))

                participants_ctrl.SetEvent(retained.keep(
                    zoom.MeetingParticipantsCtrlEventCallbacks(
                        onUserJoinCallback=_on_user_join,
                        onUserLeftCallback=_on_user_left,
                        onUserNamesChangedCallback=_on_names_changed)))

                # --- Entrée en réunion ---------------------------------------------- #
                fields = join_fields(meeting_number, display_name=display_name,
                                     passcode=passcode, sampling_rate_hz=sampling_rate_hz,
                                     zak=zak, on_behalf_token=on_behalf_token,
                                     join_token=join_token)
                param = zoom.JoinParam()
                param.userType = zoom.SDKUserType.SDK_UT_WITHOUT_LOGIN
                # `param.param` EST la structure « sans connexion » (relevé sur les bindings) :
                # il n'y a pas d'union à déréférencer côté Python.
                without_login = param.param
                for name, value in fields.items():
                    if not name.startswith("_"):
                        setattr(without_login, name, value)
                # ⚠ Le SDK journalise cette valeur en ENTIER : « Audio Raw Data Sampling Rate: 0 »
                # signifie 32 kHz (l'énumération vaut 0 pour 32K et 1 pour 48K), pas « non
                # réglé ». Vérifié — cela ressemble à un oubli dans les journaux, ça n'en est
                # pas un.
                without_login.eAudioRawdataSamplingRate = (
                    zoom.AudioRawdataSamplingRate.AudioRawdataSamplingRate_48K
                    if sampling_rate_hz == SAMPLING_RATE_48K
                    else zoom.AudioRawdataSamplingRate.AudioRawdataSamplingRate_32K)

                err = meeting.Join(param)
                if err != zoom.SDKError.SDKERR_SUCCESS:
                    raise ZoomSdkError(f"Join a échoué : {err}")

                await _await_admission(phase_changed, lambda: phase, admission_timeout_s)
                if phase is not ZoomSdkPhase.ACTIVE:
                    raise ZoomSdkError(describe_failed_admission(
                        phase, timeout_s=admission_timeout_s,
                        reason=exit_reason(phase, was_active=was_active)))

                # RATTRAPAGE : le bot arrive après les participants déjà présents, et les
                # événements d'arrivée ne sont PAS rejoués pour eux. Sans ce parcours, leurs
                # noms resteraient inconnus toute la réunion.
                registry.replace_all(_snapshot_participants(participants_ctrl))
                logger.info("Zoom SDK : %d participant(s) déjà présent(s)",
                            len(registry.others()))

                # --- Abonnement à l'audio brut par participant ---------------------- #
                def _on_one_way_audio(raw: Any, node_id: int) -> None:
                    nonlocal dropped
                    if registry.is_self(int(node_id)):
                        return                      # ne pas se transcrire soi-même
                    frame = audio_frame_to_demuxed(
                        bytes(raw.GetBuffer() or b""),
                        node_id=int(node_id),
                        name=registry.name_of(int(node_id)),
                        sample_rate_hz=int(raw.GetSampleRate() or sampling_rate_hz),
                        channels=int(raw.GetChannelNum() or 1))
                    if frame is None:
                        return
                    # Le rappel arrive sur le fil qui pompe GLib — le fil asyncio (vérifié).
                    # `call_soon_threadsafe` reste correct dans les deux cas et coûte un
                    # réveil négligeable devant une frame de 10-20 ms.
                    loop.call_soon_threadsafe(_enqueue, frame)

                def _enqueue(frame: DemuxedFrame) -> None:
                    nonlocal dropped
                    if frames.qsize() >= max_queued_frames:
                        try:
                            frames.get_nowait()     # écarte la PLUS ANCIENNE
                            dropped += 1
                            if dropped % 100 == 1:
                                logger.warning(
                                    "file audio Zoom saturée (%d frames écartées) : "
                                    "le moteur STT ne suit pas", dropped)
                        except asyncio.QueueEmpty:  # pragma: no cover — course improbable
                            pass
                    frames.put_nowait(frame)

                # --- Rejoindre la session AUDIO, en restant muet --------------------- #
                # `isAudioOff` à l'entrée ne veut pas dire « entrer micro coupé » : le SDK ne
                # rejoint alors PAS la session audio du tout, et l'abonnement à l'audio brut
                # est refusé (`SDKERR_NOT_JOIN_AUDIO` — constaté en réunion réelle). Il faut
                # donc rejoindre l'audio explicitement, puis se couper.
                _join_audio_muted(zoom, meeting)

                # --- Droit d'enregistrement : CONDITION de l'audio brut -------------- #
                # Zoom ne délivre l'audio brut qu'à un participant qui a le droit
                # d'enregistrer. Sans lui, `subscribe()` réussit et AUCUNE frame n'arrive :
                # panne muette. On vérifie donc, on demande si besoin, et on échoue avec un
                # message qui dit quoi faire.
                recording = meeting.GetMeetingRecordingController()
                await _ensure_raw_recording_allowed(
                    zoom, recording, retained, loop,
                    timeout_s=recording_permission_timeout_s)

                err = recording.StartRawRecording()
                if err != zoom.SDKError.SDKERR_SUCCESS:
                    raise ZoomSdkError(
                        f"StartRawRecording refusé : {err}. L'audio brut ne circule pas tant "
                        f"que l'enregistrement brut n'est pas démarré.")
                started_raw_recording = True

                helper = retained.keep(zoom.GetAudioRawdataHelper())
                err = helper.subscribe(
                    retained.keep(zoom.ZoomSDKAudioRawDataDelegateCallbacks(
                        onOneWayAudioRawDataReceivedCallback=_on_one_way_audio)),
                    False)                          # False = sans les interprètes
                if err != zoom.SDKError.SDKERR_SUCCESS:
                    raise ZoomSdkError(f"abonnement à l'audio brut refusé : {err}")
                logger.info("Zoom SDK : audio brut par participant abonné")

                # --- Diffusion ------------------------------------------------------ #
                watcher = asyncio.ensure_future(
                    _watch_end(phase_changed, lambda: phase, frames))
                try:
                    while True:
                        frame = await frames.get()
                        if frame is None:           # sentinelle : réunion terminée
                            return
                        yield frame
                finally:
                    watcher.cancel()

            finally:
                # ORDRE IMPORTANT : nettoyer le SDK PENDANT que la pompe GLib tourne encore.
                # `Leave`, `StopRawRecording` et `CleanUPSDK` produisent des évènements que le
                # SDK doit pouvoir traiter ; couper la pompe d'abord les laisse en suspens et
                # le processus meurt par segfault en fin d'exécution (constaté).
                _cleanup(zoom, meeting, retained, dropped, pump=pump,
                         started_raw_recording=started_raw_recording)
                stop.set()
                pump_task.cancel()
                try:
                    await pump_task
                except asyncio.CancelledError:
                    pass

        return _open()
    return _factory


async def _await_admission(changed: asyncio.Event, phase_of: Callable[[], ZoomSdkPhase],
                           timeout_s: float) -> None:
    """Attend d'être ACTIF, ou qu'un état terminal soit atteint.

    Une salle d'attente peut durer : on n'abandonne que sur expiration du délai global ou sur
    un état dont on ne sort pas (réunion terminée, entrée refusée).
    """
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        current = phase_of()
        if current in (ZoomSdkPhase.ACTIVE, ZoomSdkPhase.ENDED, ZoomSdkPhase.FAILED):
            return
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return
        changed.clear()
        try:
            await asyncio.wait_for(changed.wait(), timeout=remaining)
        except asyncio.TimeoutError:
            return


async def _watch_end(changed: asyncio.Event, phase_of: Callable[[], ZoomSdkPhase],
                     frames: asyncio.Queue) -> None:
    """Clôt le flux quand la réunion se termine.

    Sans cette sentinelle, le consommateur attendrait indéfiniment une frame qui ne viendra
    plus : la session live ne se terminerait jamais et le conteneur ne rendrait pas la main.
    """
    while True:
        changed.clear()
        if phase_of() in (ZoomSdkPhase.ENDED, ZoomSdkPhase.FAILED):
            await frames.put(None)
            return
        await changed.wait()


def _snapshot_participants(controller: Any) -> list[Participant]:
    """Instantané de `GetParticipantsList()` → participants du projet.

    Tolérant aux trous : un identifiant dont l'info n'est pas encore disponible est retenu
    SANS nom plutôt qu'ignoré — sinon ses frames audio seraient orphelines.
    """
    participants: list[Participant] = []
    for node_id in list(controller.GetParticipantsList() or []):
        info = controller.GetUserByUserID(int(node_id))
        if info is None:
            participants.append(Participant(int(node_id), ""))
            continue
        participants.append(Participant(
            int(node_id), info.GetUserName() or "", is_bot=bool(info.IsMySelf())))
    return participants


def _join_audio_muted(zoom: Any, meeting: Any) -> None:
    """Rejoint la session audio (indispensable) et coupe immédiatement le micro.

    POURQUOI CE N'EST PAS REDONDANT AVEC `isAudioOff` : ce drapeau d'entrée empêche le SDK de
    rejoindre l'audio, il ne le fait pas rejoindre en muet. Sans `JoinVoip()`, Zoom refuse
    l'abonnement à l'audio brut avec `SDKERR_NOT_JOIN_AUDIO`.

    Le micro est coupé DANS LA FOULÉE. Le risque d'émettre est de toute façon nul dans le
    conteneur — la source audio par défaut y est un puits nul, donc du silence — mais on ne
    s'en remet pas à cette propriété de l'environnement : un bot qui souffle dans une réunion
    est exactement le défaut rencontré sur Jitsi.
    """
    audio = meeting.GetMeetingAudioController()
    err = audio.JoinVoip()
    if err != zoom.SDKError.SDKERR_SUCCESS:
        raise ZoomSdkError(
            f"impossible de rejoindre la session audio ({err}) — sans elle, Zoom refuse "
            f"l'abonnement à l'audio brut.")

    participants = meeting.GetMeetingParticipantsController()
    myself = participants.GetMySelfUser()
    if myself is not None:
        muted = audio.MuteAudio(myself.GetUserID())
        logger.info("Zoom SDK : session audio rejointe, micro coupé (%s)", muted)
    else:  # pragma: no cover — le SDK n'a pas encore publié notre fiche
        logger.warning("Zoom SDK : session audio rejointe, mais identité du bot indisponible "
                       "— micro non explicitement coupé")


async def _ensure_raw_recording_allowed(zoom: Any, recording: Any, retained: _Retained,
                                        loop: asyncio.AbstractEventLoop, *,
                                        timeout_s: float) -> None:
    """Obtient le droit d'enregistrer, sans lequel Zoom ne délivre aucun audio brut.

    Trois cas, tous rencontrables en exploitation :
    - le bot est hôte/co-hôte, ou le droit lui a déjà été donné → rien à faire ;
    - le droit peut être DEMANDÉ → on le demande, et l'hôte voit une fenêtre à accepter.
      C'est le cas courant sur un compte GRATUIT, où le jeton d'enregistrement local ne
      fonctionne pas : la seule voie est l'accord manuel de l'hôte, en séance ;
    - ni l'un ni l'autre → on échoue tout de suite, plutôt que de capter le vide.
    """
    readiness = interpret_raw_recording_readiness(
        getattr(recording.CanStartRawRecording(), "name", ""),
        can_request=(recording.IsSupportRequestLocalRecordingPrivilege()
                     == zoom.SDKError.SDKERR_SUCCESS))

    if readiness is RecordingPermission.GRANTED:
        logger.info("Zoom SDK : droit d'enregistrement déjà acquis")
        return
    if readiness is RecordingPermission.UNAVAILABLE:
        raise ZoomSdkError(
            "le bot n'a pas le droit d'enregistrer et ne peut pas le demander. Sans ce droit, "
            "Zoom ne délivre AUCUN audio brut. Remèdes : faire du bot un co-hôte, ou activer "
            "l'enregistrement local sur le compte de l'hôte.")

    outcome: dict[str, Any] = {}
    answered = asyncio.Event()

    def _on_status(status: Any) -> None:
        outcome["granted"], outcome["message"] = describe_privilege_outcome(
            getattr(status, "name", str(status)))
        loop.call_soon_threadsafe(answered.set)

    def _on_privilege_changed(can_record: bool) -> None:
        # L'hôte peut aussi accorder le droit SPONTANÉMENT, sans passer par notre demande.
        if can_record and not answered.is_set():
            outcome["granted"], outcome["message"] = True, (
                "l'hôte a accordé l'enregistrement")
            loop.call_soon_threadsafe(answered.set)

    recording.SetEvent(retained.keep(zoom.MeetingRecordingCtrlEventCallbacks(
        onLocalRecordingPrivilegeRequestStatusCallback=_on_status,
        onRecordPrivilegeChangedCallback=_on_privilege_changed)))

    err = recording.RequestLocalRecordingPrivilege()
    if err != zoom.SDKError.SDKERR_SUCCESS:
        raise ZoomSdkError(f"demande d'autorisation d'enregistrement refusée : {err}")

    logger.warning(
        "Zoom SDK : EN ATTENTE DE L'HÔTE — une fenêtre « Autoriser l'enregistrement » "
        "s'affiche dans la réunion ; elle doit être acceptée (%.0f s).", timeout_s)

    # On n'attend PAS seulement le rappel. L'hôte peut accorder le droit par d'autres voies
    # que la réponse à notre demande (nous passer co-hôte, cocher « autoriser
    # l'enregistrement » dans la liste des participants, une auto-approbation de compte), et
    # rien ne garantit qu'un rappel soit émis dans tous ces cas. On INTERROGE donc l'état en
    # parallèle : `CanStartRawRecording()` est la question réellement pertinente — « ai-je le
    # droit ? » — là où le rappel n'est qu'un des chemins pour y répondre.
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        if answered.is_set():
            break
        if getattr(recording.CanStartRawRecording(), "name", "") == "SDKERR_SUCCESS":
            outcome.setdefault("granted", True)
            outcome.setdefault("message", "droit d'enregistrement constaté (accordé par l'hôte)")
            break
        if asyncio.get_running_loop().time() >= deadline:
            raise ZoomSdkError(
                f"l'hôte n'a pas accordé l'autorisation d'enregistrement en {timeout_s:.0f} s. "
                f"Une fenêtre « TranscrIA demande l'autorisation d'enregistrer » doit être "
                f"ACCEPTÉE — lancer son propre enregistrement ne l'accorde pas. Alternative : "
                f"passer le bot co-hôte.")
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(answered.wait(), timeout=1.0)

    if not outcome.get("granted"):
        raise ZoomSdkError(str(outcome.get("message") or "autorisation d'enregistrement refusée"))
    logger.info("Zoom SDK : %s", outcome["message"])


def _cleanup(zoom: Any, meeting: Any, retained: _Retained, dropped: int, *,
             pump: GLibPump, started_raw_recording: bool = False) -> None:
    """Quitte la réunion et libère le SDK, sans jamais masquer l'erreur d'origine.

    L'ordre importe : arrêter la capture, quitter la réunion, PUIS `CleanUPSDK`, PUIS relâcher
    les objets retenus. Relâcher AVANT que le SDK ait fini laisserait ses pointeurs bruts sur
    de la mémoire libérée — le piège documenté en tête de module, mais à l'arrêt.

    Entre chaque étape, on POMPE : ces appels sont asynchrones côté SDK et ne s'achèvent que
    si ses évènements sont distribués. Sans cela, le processus meurt par segfault en fin
    d'exécution — observé en réunion réelle, et invisible autrement puisque le travail utile
    était déjà terminé.

    Le service de réunion est passé EXPLICITEMENT plutôt que déduit des objets retenus :
    reconnaître « celui qui a une méthode Leave » marcherait aujourd'hui et casserait au
    premier objet du SDK qui expose le même nom pour autre chose.
    """
    def _settle(rounds: int = 20) -> None:
        """Laisse le SDK finir, SANS `await`.

        Le chemin le plus courant est l'ANNULATION (durée maximale atteinte, arrêt demandé) :
        dans une tâche annulée, tout `await asyncio.sleep()` relève immédiatement
        `CancelledError` et le nettoyage n'irait pas au bout — le processus mourait alors par
        segfault, alors même que la réunion s'était bien passée (constaté).

        Bloquer brièvement la boucle est ici sans conséquence : on est en train de tout fermer.
        """
        for _ in range(rounds):
            pump.drain_once()
            time.sleep(0.01)

    if dropped:
        logger.warning("Zoom SDK : %d frame(s) audio écartée(s) au total", dropped)
    if started_raw_recording and meeting is not None:
        # Arrêter la capture AVANT de quitter : Zoom signale l'enregistrement aux
        # participants, et le laisser courir donnerait un indicateur mensonger.
        try:
            meeting.GetMeetingRecordingController().StopRawRecording()
        except Exception as exc:  # noqa: BLE001 — le nettoyage ne doit rien masquer
            logger.debug("StopRawRecording a échoué (sans conséquence) : %r", exc)
        _settle()
    if meeting is not None:
        try:
            meeting.Leave(zoom.LeaveMeetingCmd.LEAVE_MEETING)
        except Exception as exc:  # noqa: BLE001 — le nettoyage ne doit rien masquer
            logger.debug("Leave a échoué (sans conséquence) : %r", exc)
        _settle()
    try:
        zoom.CleanUPSDK()
    except Exception as exc:  # noqa: BLE001
        logger.warning("CleanUPSDK a échoué : %r", exc)
    _settle()
    retained.objects.clear()
