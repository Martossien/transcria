"""Appel DIRECT à la LLM d'arbitrage pour le tour « discuss » du chat d'affinage.

Le mode discussion est en lecture seule (aucun fichier modifié) : un unique appel
``/v1/chat/completions`` — API OpenAI-compatible exposée par les trois backends
(llama-server, Ollama, vLLM) — suffit, et remplace la boucle agentique opencode
(plusieurs allers-retours LLM + lectures de fichiers ≈ 45-55 s/tour mesurés) par une
seule génération. Le mode « apply » garde opencode : il édite des fichiers sous
garde-fous déterministes.

Pur (construction de messages) + un POST HTTP injectable — testable sans GPU.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_TRUNCATION_NOTE = "\n[… transcription tronquée : la fin n'est pas montrée ici …]"
# Blocs de raisonnement (« thinking ») que certains templates de chat renvoient
# inline dans le contenu — jamais montrés à l'utilisateur.
_THINK_BLOCK = re.compile(r"(?s)<think>.*?</think>")

DEFAULT_MAX_TRANSCRIPT_CHARS = 60000
DEFAULT_MAX_ANSWER_TOKENS = 2000


def build_discuss_messages(
    *,
    system_prompt: str,
    summary: str,
    srt_text: str,
    structured_json: str,
    render_options_json: str,
    review_points: list[str],
    history: list[dict],
    user_message: str,
    max_transcript_chars: int = DEFAULT_MAX_TRANSCRIPT_CHARS,
) -> list[dict]:
    """Messages OpenAI-chat du tour discuss : system = contrat + livrables inline,
    puis l'historique rejoué en VRAIS tours user/assistant, puis la demande courante.

    Les tours ``system`` du fil (options de rendu, restaurations) ne sont pas rejoués :
    ce sont des notifications UI, pas de la conversation.
    """
    if max_transcript_chars > 0 and len(srt_text) > max_transcript_chars:
        srt_text = srt_text[:max_transcript_chars] + _TRUNCATION_NOTE
    context = (
        f"{system_prompt.strip()}\n\n"
        "## Livrables actuels de la réunion\n\n"
        f"### Synthèse\n{summary.strip() or '(vide)'}\n\n"
        f"### Transcription corrigée (SRT)\n{srt_text.strip() or '(vide)'}\n\n"
        f"### Données structurées (JSON)\n{structured_json.strip() or '{}'}\n\n"
        f"### Options de rendu du document\n{render_options_json.strip() or '{}'}\n"
    )
    if review_points:
        context += (
            "\n### Points à vérifier signalés par le contrôle qualité\n"
            + "\n".join(f"- {p}" for p in review_points)
            + "\n"
        )
    messages: list[dict] = [{"role": "system", "content": context}]
    for turn in history:
        role = turn.get("role")
        text = str(turn.get("text") or "").strip()
        if not text or role not in ("user", "assistant"):
            continue
        if role == "assistant" and turn.get("proposal"):
            # La proposition est stockée à part (turn.proposal) mais fait partie de la
            # réponse d'origine : la rejouer garde la continuité (« ta proposition… »).
            text += f"\n\nProposition d'application : {turn['proposal']}"
        messages.append({"role": role, "content": text})
    messages.append({"role": "user", "content": user_message})
    return messages


def chat_completion(
    config: dict,
    messages: list[dict],
    *,
    timeout_s: int = 900,
    max_tokens: int = DEFAULT_MAX_ANSWER_TOKENS,
    post=None,
) -> str:
    """Une complétion de chat sur la LLM d'arbitrage (backend courant de la config).

    Lève en cas d'erreur HTTP/réseau — l'appelant (``run_refine``) est best-effort et
    transforme toute exception en tour assistant explicatif.
    """
    from transcria.gpu.llm_backend import create_llm_backend

    backend = create_llm_backend(config)
    model = backend.model_id or "arbitrage"
    # opencode consomme « local/<modèle> » ; l'API OpenAI-compatible attend le nom nu.
    if model.startswith("local/"):
        model = model[len("local/"):]
    url = f"{backend.base_url.rstrip('/')}/chat/completions"
    if post is None:
        import requests
        post = requests.post
    logger.info("Affinage discuss : complétion directe sur %s (model=%s)", url, model)
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.4,
        "max_tokens": max_tokens,
        "stream": False,
        # Modèles « thinking » (Qwen3.x…) : sans cela, le raisonnement part dans
        # reasoning_content et consomme TOUT le budget de tokens → content vide
        # (observé en réel sur llama-server). Honoré par llama-server et vLLM.
        "chat_template_kwargs": {"enable_thinking": False},
    }
    resp = post(url, json=payload, timeout=timeout_s)
    try:
        resp.raise_for_status()
    except Exception:
        # Backend qui rejette le champ non standard chat_template_kwargs : une
        # seconde tentative sans lui (le thinking éventuel est alors filtré plus bas).
        logger.info("Affinage discuss : retry sans chat_template_kwargs")
        payload.pop("chat_template_kwargs", None)
        resp = post(url, json=payload, timeout=timeout_s)
        resp.raise_for_status()
    data = resp.json()
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    return _THINK_BLOCK.sub("", str(content)).strip()
