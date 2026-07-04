"""Appel LLM direct du mode discuss (``transcria.workflow.refine_llm``) — GPU-free.

Construction des messages OpenAI-chat (contexte inline, historique rejoué, troncature
de la transcription) et complétion HTTP (backend courant, POST injectable).
"""
from __future__ import annotations

from types import SimpleNamespace

from transcria.workflow.refine_llm import build_discuss_messages, chat_completion


def _messages(**overrides):
    kwargs = dict(
        system_prompt="Tu es l'assistant d'affinage.",
        summary="Synthèse de la réunion.",
        srt_text="1\n00:00:00,000 --> 00:00:01,000\nSPEAKER_00(Alice): Bonjour.\n",
        structured_json='{"decisions": []}',
        render_options_json='{"theme": ""}',
        review_points=[],
        history=[],
        user_message="Question ?",
    )
    kwargs.update(overrides)
    return build_discuss_messages(**kwargs)


class TestBuildDiscussMessages:
    def test_system_carries_prompt_and_deliverables(self):
        msgs = _messages(review_points=["Variantes lexique non résolues : X / Y."])
        assert msgs[0]["role"] == "system"
        system = msgs[0]["content"]
        assert "assistant d'affinage" in system
        assert "Synthèse de la réunion." in system
        assert "SPEAKER_00(Alice)" in system
        assert "Variantes lexique non résolues" in system

    def test_current_message_is_last_user_turn(self):
        msgs = _messages(user_message="Ma demande précise")
        assert msgs[-1] == {"role": "user", "content": "Ma demande précise"}

    def test_history_replayed_with_roles_system_turns_skipped(self):
        history = [
            {"role": "user", "text": "Premier ?"},
            {"role": "assistant", "text": "Réponse."},
            {"role": "system", "text": "Options de rendu modifiées."},   # notification UI
            {"role": "assistant", "text": ""},                            # vide : ignoré
        ]
        msgs = _messages(history=history)
        roles = [m["role"] for m in msgs]
        assert roles == ["system", "user", "assistant", "user"]

    def test_assistant_proposal_reinjected_in_replay(self):
        # La proposition est stockée à part (turn.proposal) mais faisait partie de la
        # réponse : le rejeu la réintègre pour la continuité (« ta proposition… »).
        history = [{"role": "assistant", "text": "Je peux corriger.",
                    "proposal": "corriger le titre du document"}]
        msgs = _messages(history=history)
        assert "Proposition d'application : corriger le titre du document" in msgs[1]["content"]

    def test_long_transcript_truncated_with_marker(self):
        msgs = _messages(srt_text="x" * 500, max_transcript_chars=100)
        system = msgs[0]["content"]
        assert "transcription tronquée" in system
        assert "x" * 101 not in system

    def test_empty_fields_yield_placeholders(self):
        system = _messages(summary="", srt_text="", structured_json="")[0]["content"]
        assert "(vide)" in system


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class TestChatCompletion:
    def _config(self):
        # Backend « script » (llama-server) : base_url dérivée du port.
        return {
            "services": {"arbitrage_llm_port": 8099},
            "workflow": {"arbitration_llm": {"model_id": "local/arbitrage"}},
        }

    def test_posts_openai_payload_and_returns_content(self):
        seen = {}

        def post(url, json=None, timeout=None):
            seen.update(url=url, payload=json, timeout=timeout)
            return _FakeResponse({"choices": [{"message": {"content": "Réponse LLM."}}]})

        answer = chat_completion(self._config(), [{"role": "user", "content": "?"}],
                                 timeout_s=30, max_tokens=123, post=post)

        assert answer == "Réponse LLM."
        assert seen["url"].endswith("/v1/chat/completions")
        assert seen["payload"]["model"] == "arbitrage"          # préfixe local/ retiré
        assert seen["payload"]["max_tokens"] == 123
        assert seen["timeout"] == 30
        # Modèles thinking : sans cela le budget de tokens part en raisonnement.
        assert seen["payload"]["chat_template_kwargs"] == {"enable_thinking": False}

    def test_retry_without_template_kwargs_on_rejection(self):
        # Backend qui rejette le champ non standard → seconde tentative sans lui.
        calls = []

        def post(url, json=None, timeout=None):
            calls.append(dict(json))
            if "chat_template_kwargs" in json:
                resp = SimpleNamespace()
                def boom():
                    raise RuntimeError("HTTP 400: unknown field")
                resp.raise_for_status = boom
                return resp
            return _FakeResponse({"choices": [{"message": {"content": "OK sans kwargs."}}]})

        answer = chat_completion(self._config(), [{"role": "user", "content": "?"}], post=post)
        assert answer == "OK sans kwargs."
        assert len(calls) == 2 and "chat_template_kwargs" not in calls[1]

    def test_think_blocks_stripped(self):
        def post(url, json=None, timeout=None):
            return _FakeResponse({"choices": [{"message": {
                "content": "<think>raisonnement interne</think>Réponse visible."}}]})

        answer = chat_completion(self._config(), [], post=post)
        assert answer == "Réponse visible."
        assert "raisonnement" not in answer

    def test_empty_choices_yields_empty_string(self):
        def post(url, json=None, timeout=None):
            return _FakeResponse({"choices": []})

        assert chat_completion(self._config(), [], post=post) == ""

    def test_http_error_propagates(self):
        # L'appelant (run_refine) est best-effort : l'exception doit REMONTER.
        def post(url, json=None, timeout=None):
            resp = SimpleNamespace()
            def boom():
                raise RuntimeError("HTTP 500")
            resp.raise_for_status = boom
            return resp

        try:
            chat_completion(self._config(), [], post=post)
            raise AssertionError("une exception était attendue")
        except RuntimeError as exc:
            assert "500" in str(exc)


class TestBudgetEtTroncatureHonnete:
    """C2.5 — budget dérivé du contexte réel + troncature début+fin ANNONCÉE."""

    def test_transcription_courte_intacte(self):
        from transcria.workflow.refine_llm import truncate_transcript
        text, meta = truncate_transcript("court", 1000)
        assert text == "court" and meta == {"truncated": False}

    def test_troncature_garde_debut_et_fin(self):
        from transcria.workflow.refine_llm import truncate_transcript
        srt = "\n".join(f"{i}\n00:{i // 60:02d}:{i % 60:02d},000 --> 00:00:59,000\nphrase {i}"
                        for i in range(2000))
        text, meta = truncate_transcript(srt, 10000)
        assert meta["truncated"] is True
        assert "phrase 0" in text            # le début est là
        assert "phrase 1999" in text         # la FIN est là (les décisions s'y prennent)
        assert "PAS visible ici" in text     # le LLM est prévenu
        assert meta["gap_from"].count(":") == 2 and meta["gap_to"].count(":") == 2

    def test_budget_explicite_prioritaire(self):
        from transcria.workflow.refine_llm import compute_transcript_budget_chars
        cfg = {"workflow": {"refine": {"max_transcript_chars": 12345}}}
        assert compute_transcript_budget_chars(cfg) == 12345

    def test_budget_par_defaut_sans_gpu(self, monkeypatch):
        import transcria.workflow.refine_llm as m
        # simuler l'absence de GPU (frontale) : le défaut honnête s'applique
        import torch
        monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
        assert m.compute_transcript_budget_chars({}) == m.DEFAULT_MAX_TRANSCRIPT_CHARS
