"""Vague 3 — machine d'états des sessions de réunion (PURE) + chiffrement meeting_ref.

La machine est LE contrat (docs/UI_REUNIONS_WORKFLOW.md §6.1) : le mapping des codes de
sortie du bot, les transitions légales, l'annulable/replanifiable. Un runner périmé qui
propose un état illégal ne doit rien écraser — c'est ici que ça se prouve.
"""
from __future__ import annotations

import pytest

from transcria.ingestion import session_states as st
from transcria.ingestion.meeting_ref_crypto import (
    MeetingRefKeyMissing,
    decrypt_meeting_ref,
    encrypt_meeting_ref,
)


class TestExitCodeMapping:
    def test_contrat_bot_0123(self):
        assert st.state_for_exit_code(0, attempts=1, max_attempts=4) == st.DONE
        assert st.state_for_exit_code(1, attempts=1, max_attempts=4) == st.NOT_ADMITTED
        assert st.state_for_exit_code(2, attempts=1, max_attempts=4) == st.FAILED_RETRYABLE
        assert st.state_for_exit_code(3, attempts=1, max_attempts=4) == st.FAILED_FINAL

    def test_essais_epuises_terminal(self):
        assert st.state_for_exit_code(2, attempts=4, max_attempts=4) == st.FAILED_FINAL

    def test_code_inconnu_terminal(self):
        assert st.state_for_exit_code(137, attempts=0, max_attempts=4) == st.FAILED_FINAL


class TestTransitions:
    def test_cycle_nominal_complet(self):
        chain = [st.PLANNED, st.CLAIMED, st.JOINING, st.WAITING_ADMISSION,
                 st.IN_MEETING, st.INGESTING, st.DONE]
        for a, b in zip(chain, chain[1:]):
            assert st.can_transition(a, b), (a, b)

    def test_terminaux_sont_des_impasses(self):
        for terminal in st.TERMINAL_STATES:
            for target in st.ALL_STATES:
                assert not st.can_transition(terminal, target), (terminal, target)

    def test_runner_perime_ne_reveille_pas_une_session_annulee(self):
        assert not st.can_transition(st.CANCELLED, st.JOINING)

    def test_retryable_redevient_claimable(self):
        assert st.can_transition(st.FAILED_RETRYABLE, st.PLANNED)

    def test_lease_expire_rend_le_claim(self):
        assert st.can_transition(st.CLAIMED, st.PLANNED)

    def test_annulable_inclut_in_meeting_mais_pas_ingesting(self):
        assert st.IN_MEETING in st.CANCELLABLE_STATES
        assert st.INGESTING not in st.CANCELLABLE_STATES   # l'audio existe, on ne le perd pas

    def test_evenements_runner_jamais_terminaux(self):
        assert st.state_for_runner_event("in_meeting") == st.IN_MEETING
        assert st.state_for_runner_event("done") is None   # l'issue passe par /result


class TestMeetingRefCrypto:
    def test_aller_retour(self, monkeypatch):
        from cryptography.fernet import Fernet
        monkeypatch.setenv("TRANSCRIA_MEETING_REF_KEY", Fernet.generate_key().decode())
        stored = encrypt_meeting_ref("https://zoom.us/j/123?pwd=secret")
        assert stored.startswith("enc1:") and "secret" not in stored
        assert decrypt_meeting_ref(stored) == "https://zoom.us/j/123?pwd=secret"

    def test_cle_absente_erreur_explicite_sans_repli_clair(self, monkeypatch):
        monkeypatch.delenv("TRANSCRIA_MEETING_REF_KEY", raising=False)
        with pytest.raises(MeetingRefKeyMissing):
            encrypt_meeting_ref("x")

    def test_jamais_de_passthrough_clair(self, monkeypatch):
        from cryptography.fernet import Fernet
        monkeypatch.setenv("TRANSCRIA_MEETING_REF_KEY", Fernet.generate_key().decode())
        with pytest.raises(ValueError):
            decrypt_meeting_ref("https://zoom.us/j/123")   # une base non chiffrée doit CRIER

    def test_cle_changee_message_sans_la_valeur(self, monkeypatch):
        from cryptography.fernet import Fernet
        monkeypatch.setenv("TRANSCRIA_MEETING_REF_KEY", Fernet.generate_key().decode())
        stored = encrypt_meeting_ref("ref")
        monkeypatch.setenv("TRANSCRIA_MEETING_REF_KEY", Fernet.generate_key().decode())
        with pytest.raises(ValueError, match="indéchiffrable"):
            decrypt_meeting_ref(stored)
