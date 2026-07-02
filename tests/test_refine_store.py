"""Store du chat d'affinage des livrables — pur filesystem, GPU-free.

Contrat : tout vit sous ``jobs/<id>/refine/`` — ``chat.json`` (historique append-only),
``request.json`` (demande en attente, consommée UNE fois), ``versions/v<N>/`` (snapshots
des artefacts avant chaque application).
"""
from pathlib import Path

from transcria.workflow.refine_store import RefineStore, extract_proposal


def _store(tmp_path) -> RefineStore:
    return RefineStore(jobs_dir=str(tmp_path), job_id="j1")


class TestChatHistory:
    def test_append_and_load_turns(self, tmp_path):
        s = _store(tmp_path)
        s.append_turn(role="user", kind="discuss", text="Peux-tu raccourcir la synthèse ?")
        s.append_turn(role="assistant", kind="discuss", text="Oui — je propose de…")
        turns = s.load_turns()
        assert [t["role"] for t in turns] == ["user", "assistant"]
        assert turns[0]["kind"] == "discuss"
        assert turns[0]["text"].startswith("Peux-tu")
        assert "ts" in turns[0]  # horodatage ISO UTC

    def test_empty_history(self, tmp_path):
        assert _store(tmp_path).load_turns() == []

    def test_history_capped_to_max_turns(self, tmp_path):
        s = _store(tmp_path)
        for i in range(12):
            s.append_turn(role="user", kind="discuss", text=f"tour {i}", max_turns=10)
        turns = s.load_turns()
        assert len(turns) == 10
        assert turns[0]["text"] == "tour 2"  # les plus anciens sont élagués

    def test_conversation_context_renders_recent_turns(self, tmp_path):
        """Le contexte conversationnel (relu par la LLM à chaque tour) est un texte
        compact des N derniers tours — c'est ce qui fait une vraie conversation."""
        s = _store(tmp_path)
        s.append_turn(role="user", kind="discuss", text="Question A ?")
        s.append_turn(role="assistant", kind="discuss", text="Réponse A.")
        s.append_turn(role="user", kind="apply", text="Applique A.")
        ctx = s.conversation_context(max_turns=2)
        assert "Question A" not in ctx          # élagué (seulement les 2 derniers)
        assert "Réponse A." in ctx and "Applique A." in ctx
        assert "UTILISATEUR" in ctx and "ASSISTANT" in ctx  # rôles lisibles

    def test_conversation_context_empty(self, tmp_path):
        assert _store(tmp_path).conversation_context() == ""


class TestExtractProposal:
    """La réponse discuss se termine par « --- / Proposition d'application : … » (contrat
    du prompt). L'extraction est SERVEUR (testée ici) — l'UI ne parse rien."""

    def test_extracts_proposal_and_strips_block(self):
        answer = (
            "La réunion compte 2 locuteurs : une cliente et un fromager.\n\n"
            "---\n"
            "Proposition d'application : mettre en avant les deux intervenants dans la synthèse."
        )
        text, proposal = extract_proposal(answer)
        assert proposal == "mettre en avant les deux intervenants dans la synthèse."
        assert "---" not in text and "Proposition" not in text
        assert text.startswith("La réunion compte 2 locuteurs")

    def test_aucune_means_no_proposal_text_kept(self):
        answer = "Réponse.\n\n---\nProposition d'application : aucune — la synthèse est fidèle."
        text, proposal = extract_proposal(answer)
        assert proposal is None
        assert text == answer   # bloc informatif conservé tel quel

    def test_no_separator_no_proposal(self):
        text, proposal = extract_proposal("Réponse simple sans proposition.")
        assert proposal is None and text == "Réponse simple sans proposition."

    def test_tail_without_label_untouched(self):
        answer = "Réponse.\n\n---\nNote finale sans le label attendu."
        text, proposal = extract_proposal(answer)
        assert proposal is None and text == answer

    def test_apostrophe_typographique_et_gras(self):
        answer = "Réponse.\n\n---\n**Proposition d’application** : raccourcir la synthèse."
        _, proposal = extract_proposal(answer)
        assert proposal == "raccourcir la synthèse."

    def test_empty_input(self):
        assert extract_proposal("") == ("", None)

    def test_label_sans_separateur_derniere_ligne(self):
        # Cas RÉEL observé : le modèle enchaîne après un tableau Markdown sans la
        # ligne « --- » — la proposition sur la dernière ligne doit être récupérée.
        answer = (
            "Récapitulatif :\n\n"
            "| Fichier | Occurrences |\n"
            "|---------|-------------|\n"
            "| resume.md | 3 |\n\n"
            "Proposition d'application : Appliquer les corrections listées ci-dessus."
        )
        text, proposal = extract_proposal(answer)
        assert proposal == "Appliquer les corrections listées ci-dessus."
        assert text.rstrip().endswith("| resume.md | 3 |")   # tableau conservé, label retiré

    def test_label_sans_separateur_aucune(self):
        answer = "Réponse complète.\nProposition d'application : aucune"
        text, proposal = extract_proposal(answer)
        assert proposal is None and text == answer

    def test_separateur_orphelin_avant_label_nettoye(self):
        # « --- » collé au label (sans ligne vide après) : séparateur non matché par le
        # chemin contractuel, mais le label en dernière ligne est accepté et le « --- »
        # résiduel est retiré du texte affiché.
        answer = "Réponse.\n---\nProposition d'application : corriger le titre."
        text, proposal = extract_proposal(answer)
        assert proposal == "corriger le titre."
        assert text == "Réponse."


class TestTurnProposal:
    def test_append_turn_stores_proposal(self, tmp_path):
        s = _store(tmp_path)
        s.append_turn(role="assistant", kind="discuss", text="Réponse.", proposal="raccourcir la synthèse")
        turn = s.load_turns()[-1]
        assert turn["proposal"] == "raccourcir la synthèse"

    def test_append_turn_without_proposal_has_no_key(self, tmp_path):
        s = _store(tmp_path)
        s.append_turn(role="assistant", kind="discuss", text="Réponse.")
        assert "proposal" not in s.load_turns()[-1]


class TestPendingRequest:
    def test_write_then_consume_request(self, tmp_path):
        s = _store(tmp_path)
        s.write_request(kind="apply", message="Mets l'accent sur les décisions budget")
        req = s.consume_request()
        assert req is not None
        assert req["kind"] == "apply" and "budget" in req["message"]
        assert s.consume_request() is None  # consommée une seule fois

    def test_has_active_request(self, tmp_path):
        s = _store(tmp_path)
        assert s.has_active_request() is False
        s.write_request(kind="discuss", message="a")
        assert s.has_active_request() is True
        s.consume_request()
        assert s.has_active_request() is False

    def test_requeue_request_after_retryable_skip(self, tmp_path):
        """Verrou LLM indisponible → la demande est RÉ-ÉCRITE (le tour n'est pas perdu)."""
        s = _store(tmp_path)
        s.write_request(kind="apply", message="ne pas perdre")
        req = s.consume_request()
        s.requeue_request(req)
        assert s.has_active_request() is True
        assert s.consume_request()["message"] == "ne pas perdre"


class TestVersions:
    def _seed_srt(self, tmp_path) -> Path:
        src = tmp_path / "j1" / "metadata"
        src.mkdir(parents=True, exist_ok=True)
        srt = src / "transcription_corrigee.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nBonjour\n", encoding="utf-8")
        return srt

    def test_snapshot_creates_versioned_copy(self, tmp_path):
        s = _store(tmp_path)
        srt = self._seed_srt(tmp_path)
        n = s.snapshot_artifacts([srt])
        assert n == 1
        assert s.list_versions() == [1]
        copy = tmp_path / "j1" / "refine" / "versions" / "v1" / "transcription_corrigee.srt"
        assert copy.is_file() and "Bonjour" in copy.read_text(encoding="utf-8")

    def test_snapshot_increments_and_skips_missing(self, tmp_path):
        s = _store(tmp_path)
        srt = self._seed_srt(tmp_path)
        s.snapshot_artifacts([srt])
        s.snapshot_artifacts([srt, tmp_path / "j1" / "absent.json"])  # absent = ignoré
        assert s.list_versions() == [1, 2]

    def test_restore_version_copies_back(self, tmp_path):
        s = _store(tmp_path)
        srt = self._seed_srt(tmp_path)
        s.snapshot_artifacts([srt])
        srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nModifié\n", encoding="utf-8")
        restored = s.restore_version(1)
        assert restored == ["transcription_corrigee.srt"]
        assert "Bonjour" in srt.read_text(encoding="utf-8")

    def test_restore_unknown_version_returns_empty(self, tmp_path):
        assert _store(tmp_path).restore_version(99) == []

    def test_restore_deletes_file_absent_at_snapshot(self, tmp_path):
        """render_options.json créé PAR l'apply : le revert doit le SUPPRIMER (retour
        exact à l'état d'avant — cas réel observé en E2E GPU 2026-07-02)."""
        s = _store(tmp_path)
        srt = self._seed_srt(tmp_path)
        options = tmp_path / "j1" / "context" / "render_options.json"
        assert not options.is_file()
        s.snapshot_artifacts([srt, options])       # options ABSENT au snapshot
        options.parent.mkdir(parents=True, exist_ok=True)
        options.write_text('{"sections": {"transcript": false}}', encoding="utf-8")

        restored = s.restore_version(1)

        assert "render_options.json" in restored
        assert not options.is_file()                # supprimé = état d'avant restauré
