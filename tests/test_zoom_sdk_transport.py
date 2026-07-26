"""Parties PURES du transport Zoom/SDK natif + pompage de la boucle GLib.

Le SDK n'est pas importé : `zoom_sdk_transport` ne l'importe qu'à l'intérieur de sa fonction
d'ouverture (dép opt-in), exactement comme `livekit_transport`. Le module est donc importable
en CI, et ses fonctions pures y sont vérifiables.
"""
from __future__ import annotations

import asyncio
import struct

import pytest

from connector_service.live.glib_loop import GLibPump
from connector_service.live.zoom_sdk_transport import (
    SAMPLING_RATE_32K,
    SAMPLING_RATE_48K,
    audio_frame_to_demuxed,
    join_fields,
)


# --------------------------------------------------------------------------- #
#  Paramètres d'entrée en réunion
# --------------------------------------------------------------------------- #
def test_entree_silencieuse_par_defaut():
    """LE point qui compte : le bot est un AUDITEUR. Sur Jitsi il avait fallu neutraliser la
    capture par des réglages détournés, après qu'un bip a été entendu dans une vraie réunion ;
    ici c'est un paramètre d'entrée, et ce test le verrouille."""
    fields = join_fields("5786297113", display_name="TranscrIA")
    assert fields["isAudioOff"] is True
    assert fields["isVideoOff"] is True
    assert fields["isMyVoiceInMix"] is False


def test_audio_mono():
    """Le stéréo doublerait le volume transporté sans rien apporter à la parole."""
    assert join_fields("5786297113", display_name="X")["isAudioRawDataStereo"] is False


def test_numero_de_reunion_entier():
    """Le SDK attend un entier, pas la forme affichée dans les invitations."""
    assert join_fields("5786297113", display_name="X")["meetingNumber"] == 5786297113


def test_jetons_d_autorisation_vides_par_defaut():
    """Une réunion du compte propriétaire de l'app n'exige NI ZAK NI OBF — c'est le régime qui
    dispense de revue Zoom. Les remplir par défaut ferait échouer ce cas nominal."""
    fields = join_fields("5786297113", display_name="X")
    assert fields["userZAK"] == ""
    assert fields["onBehalfToken"] == ""
    assert fields["join_token"] == ""


def test_jetons_transmis_quand_fournis():
    """Réunion externe : les emplacements existent et doivent être respectés."""
    fields = join_fields("5786297113", display_name="X",
                         zak="zak-abc", on_behalf_token="obf-def", join_token="jt-ghi")
    assert fields["userZAK"] == "zak-abc"
    assert fields["onBehalfToken"] == "obf-def"
    assert fields["join_token"] == "jt-ghi"


def test_code_secret_transmis():
    assert join_fields("5786297113", display_name="X", passcode="s3cr3t")["psw"] == "s3cr3t"


def test_nom_affiche_obligatoire():
    """Zoom refuse une entrée anonyme : échouer ici, avec un message clair, vaut mieux qu'un
    refus opaque au moment de rejoindre."""
    with pytest.raises(ValueError, match="[Nn]om affiché"):
        join_fields("5786297113", display_name="")


@pytest.mark.parametrize("debit", [SAMPLING_RATE_32K, SAMPLING_RATE_48K])
def test_debits_geres_acceptes(debit):
    assert join_fields("5786297113", display_name="X",
                       sampling_rate_hz=debit)["_sampling_rate_hz"] == debit


@pytest.mark.parametrize("debit", [8000, 16000, 44100, 96000])
def test_debit_non_gere_refuse(debit):
    """Le SDK n'expose QUE 32 et 48 kHz — pas 16 kHz, contrairement aux autres transports.
    Demander autre chose échouerait côté SDK, sans message utile."""
    with pytest.raises(ValueError, match="débit"):
        join_fields("5786297113", display_name="X", sampling_rate_hz=debit)


# --------------------------------------------------------------------------- #
#  Conversion des frames audio
# --------------------------------------------------------------------------- #
def _pcm(*valeurs: int) -> bytes:
    return struct.pack(f"<{len(valeurs)}h", *valeurs)


def test_frame_convertie_avec_identite_du_locuteur():
    """C'est l'apport décisif du SDK natif : l'audio arrive attribué. Le pilote navigateur
    documentait cette identité comme non résoluble."""
    frame = audio_frame_to_demuxed(_pcm(1000, -1000), node_id=42, name="Alice",
                                   sample_rate_hz=32000, channels=1)
    assert frame is not None
    assert frame.participant_id == "42"
    assert frame.participant_name == "Alice"
    assert frame.sample_rate_hz == 32000
    assert frame.channels == 1
    assert frame.track_id == "zoom-node-42"
    assert frame.payload == _pcm(1000, -1000)


def test_frame_vide_ecartee():
    """Le SDK émet des frames vides pendant les silences et les transitions. Les laisser
    passer ferait COMPTER des frames sans audio — l'erreur déjà commise sur un gate, où un
    flux « qui coule » ne transportait que des zéros."""
    assert audio_frame_to_demuxed(b"", node_id=1, name="X",
                                  sample_rate_hz=32000, channels=1) is None


def test_nombre_de_canaux_nul_retombe_sur_mono():
    """Une frame à 0 canal ferait diviser par zéro le calcul d'horloge média en aval."""
    frame = audio_frame_to_demuxed(_pcm(5), node_id=1, name="X",
                                   sample_rate_hz=32000, channels=0)
    assert frame is not None and frame.channels == 1


# --------------------------------------------------------------------------- #
#  Pompage de la boucle GLib
# --------------------------------------------------------------------------- #
class _FakeGLib:
    """Boucle GLib factice : une file d'évènements à traiter. Permet de tester la logique de
    pompage en CI, où libglib n'est pas garantie présente."""

    def __init__(self, events: int) -> None:
        self.remaining = events
        self.processed = 0

    def primitives(self):
        def pending() -> bool:
            return self.remaining > 0

        def iterate() -> None:
            self.remaining -= 1
            self.processed += 1

        return pending, iterate


def test_pompage_traite_tous_les_evenements_en_attente():
    glib = _FakeGLib(events=7)
    pump = GLibPump(glib.primitives())
    assert pump.drain_once() == 7
    assert glib.processed == 7
    assert pump.iterations == 7
    assert not pump.saturated_ticks


def test_pompage_sans_evenement_ne_fait_rien():
    pump = GLibPump(_FakeGLib(events=0).primitives())
    assert pump.drain_once() == 0
    assert pump.iterations == 0


def test_plafond_par_tranche_protege_la_boucle_asyncio():
    """Sans plafond, un SDK qui produit des évènements en continu monopoliserait le fil et
    affamerait asyncio : la transcription s'arrêterait sans qu'aucune erreur ne le signale."""
    glib = _FakeGLib(events=10_000)
    pump = GLibPump(glib.primitives(), max_iterations_per_tick=50)
    assert pump.drain_once() == 50
    assert glib.remaining == 9950
    assert pump.saturated_ticks == 1


def test_pompage_asynchrone_s_arrete_sur_demande():
    glib = _FakeGLib(events=1_000_000)
    pump = GLibPump(glib.primitives(), interval_s=0.001, max_iterations_per_tick=10)

    async def _main() -> None:
        stop = asyncio.Event()
        task = asyncio.ensure_future(pump.run(stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(_main())
    assert pump.iterations > 0
    assert glib.processed == pump.iterations


def test_la_boucle_asyncio_reste_reactive_pendant_le_pompage():
    """Propriété centrale de la conception : asyncio reste la boucle PRINCIPALE. Si le
    pompage l'affamait, tout l'intérêt de ne pas céder le contrôle à GLib disparaîtrait."""
    glib = _FakeGLib(events=1_000_000)
    pump = GLibPump(glib.primitives(), interval_s=0.001, max_iterations_per_tick=10)
    ticks = 0

    async def _main() -> None:
        nonlocal ticks
        stop = asyncio.Event()

        async def concurrent() -> None:
            nonlocal ticks
            while not stop.is_set():
                ticks += 1
                await asyncio.sleep(0.002)

        tasks = [asyncio.ensure_future(pump.run(stop)),
                 asyncio.ensure_future(concurrent())]
        await asyncio.sleep(0.08)
        stop.set()
        await asyncio.gather(*tasks)

    asyncio.run(_main())
    assert ticks > 1, "la tâche concurrente n'a pas été ordonnancée : asyncio est affamé"


def test_derniere_tranche_traitee_a_l_arret():
    """Les évènements de FERMETURE du SDK (départ de réunion, nettoyage) sont émis au dernier
    moment : les perdre laisserait des ressources natives pendantes."""
    glib = _FakeGLib(events=0)
    pump = GLibPump(glib.primitives(), interval_s=0.001)

    async def _main() -> None:
        stop = asyncio.Event()
        task = asyncio.ensure_future(pump.run(stop))
        await asyncio.sleep(0.01)
        glib.remaining = 5            # évènements produits juste avant l'arrêt
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(_main())
    assert glib.processed == 5
