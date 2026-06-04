#!/usr/bin/env python3
"""Arbitrage LLM expérimental entre trois sorties STT.

Entrées attendues : trois jobs conservés, typiquement A=Cohere, B=Whisper,
C=Whisper hotwords. Le script construit des fenêtres alignées sur les tours
locuteurs quand ils existent, appelle la LLM d'arbitrage par lots, puis écrit
un JSON auditable et un SRT candidat.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from build_hybrid_transcript import (  # noqa: E402
    _clean_text,
    _format_time,
    _generic_hallucinations,
    _load_lexicon_terms,
    _low_word_ratio,
    _max_no_speech_prob,
    _overlap,
    _slice_segments,
    _term_hits,
    _words,
    _worst_reliability,
    build_windows,
    load_segments,
    split_text_for_srt,
)

logging.basicConfig(
    format="%(asctime)s [arbitrate_hybrid_llm] %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
    stream=sys.stderr,
    force=True,
)
logger = logging.getLogger("arbitrate_hybrid_llm")


@dataclass(frozen=True)
class Candidate:
    code: str
    label: str
    job_id: str
    job_dir: Path
    segments_path: Path


@dataclass(frozen=True)
class SpeakerInfo:
    speaker_id: str
    name: str
    role: str
    gender: str
    source: str


@dataclass(frozen=True)
class LlmCallResult:
    content: str
    finish_reason: str
    completion_tokens: int | None
    response: dict[str, Any]


def _load_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("JSON illisible ignoré: %s (%s)", path, exc)
        return None


def _parse_candidate(raw: str, jobs_dir: Path) -> Candidate:
    """Parse A:cohere=job_id, A=job_id ou A:cohere=/tmp/result.json."""
    if "=" not in raw:
        raise SystemExit("--candidate doit utiliser A:label=job_id ou A=job_id")
    left, job_id = raw.split("=", 1)
    if ":" in left:
        code, label = left.split(":", 1)
    else:
        code, label = left, left
    code = _clean_text(code).upper()
    label = _clean_text(label)
    job_id = _clean_text(job_id)
    if code not in {"A", "B", "C"}:
        raise SystemExit(f"Code candidat invalide {code!r}; attendu A, B ou C")
    if not label or not job_id:
        raise SystemExit("--candidate contient un label ou un job_id vide")
    candidate_result = Path(job_id)
    if candidate_result.exists() and candidate_result.suffix.lower() == ".json":
        data = _load_json(candidate_result) or {}
        job_id = _clean_text(data.get("job_id"))
        if not job_id:
            raise SystemExit(f"JSON résultat sans job_id: {candidate_result}")
    job_dir = jobs_dir / job_id
    return Candidate(
        code=code,
        label=label,
        job_id=job_id,
        job_dir=job_dir,
        segments_path=job_dir / "metadata" / "transcription_segments.json",
    )


def _speaker_turns(job_dir: Path) -> list[dict]:
    data = _load_json(job_dir / "speakers" / "speaker_turns.json") or {}
    turns = data.get("exclusive_turns") or data.get("turns") or []
    normalized: list[dict] = []
    for raw in turns:
        if not isinstance(raw, dict):
            continue
        try:
            start = float(raw.get("start", 0.0))
            end = float(raw.get("end", start))
        except (TypeError, ValueError):
            continue
        speaker = _clean_text(raw.get("speaker") or raw.get("label"))
        if speaker and end > start:
            normalized.append({"start": start, "end": end, "speaker": speaker})
    return sorted(normalized, key=lambda item: (item["start"], item["end"]))


def _speaker_infos(job_dir: Path) -> dict[str, SpeakerInfo]:
    stats = _load_json(job_dir / "speakers" / "speaker_stats.json") or {}
    mapping = _load_json(job_dir / "speakers" / "speaker_mapping.json") or {}
    participants = _load_json(job_dir / "context" / "participants.json") or []
    participants_by_id = {
        str(item.get("id")): item
        for item in participants
        if isinstance(item, dict) and item.get("id")
    }
    mapped_by_speaker = {
        str(item.get("speaker_id")): item
        for item in mapping.get("speakers", [])
        if isinstance(item, dict) and item.get("speaker_id")
    }
    raw_mapping = mapping.get("mapping") if isinstance(mapping.get("mapping"), dict) else {}

    infos: dict[str, SpeakerInfo] = {}
    for raw in stats.get("speakers") or []:
        if not isinstance(raw, dict):
            continue
        speaker_id = _clean_text(raw.get("speaker_id") or raw.get("label"))
        if not speaker_id:
            continue
        mapped = mapped_by_speaker.get(speaker_id, {})
        participant_id = ""
        raw_mapped = raw_mapping.get(speaker_id)
        if isinstance(raw_mapped, dict):
            participant_id = str(raw_mapped.get("participant_id") or "")
        participant = participants_by_id.get(participant_id, {})
        name = _clean_text(mapped.get("mapped_name") or participant.get("name") or raw.get("mapped_name") or speaker_id)
        role = _clean_text(participant.get("role") or participant.get("function") or raw.get("role") or "")
        gender = _clean_text(mapped.get("gender") or participant.get("gender") or raw.get("gender") or "")
        source = _clean_text(mapped.get("match_source") or raw.get("gender_source") or "")
        infos[speaker_id] = SpeakerInfo(
            speaker_id=speaker_id,
            name=name,
            role=role,
            gender=gender,
            source=source,
        )
    return infos


def _speaker_for_segment(segment: dict, turns: list[dict]) -> str:
    speaker = _clean_text(segment.get("speaker"))
    if speaker:
        return speaker
    overlaps: dict[str, float] = {}
    for turn in turns:
        duration = _overlap(float(segment["start"]), float(segment["end"]), turn)
        if duration > 0:
            overlaps[turn["speaker"]] = overlaps.get(turn["speaker"], 0.0) + duration
    if not overlaps:
        return "SPEAKER_XX"
    return max(overlaps.items(), key=lambda item: item[1])[0]


def _speakers_in_window(turns: list[dict], start: float, end: float) -> list[str]:
    seen: list[str] = []
    for turn in turns:
        if _overlap(start, end, turn) <= 0:
            continue
        speaker = str(turn["speaker"])
        if speaker not in seen:
            seen.append(speaker)
    return seen


def _speaker_line(speaker_id: str, infos: dict[str, SpeakerInfo]) -> str:
    info = infos.get(speaker_id)
    if not info:
        return f"{speaker_id}: nom inconnu"
    details = [info.name or speaker_id]
    if info.role:
        details.append(f"rôle={info.role}")
    if info.gender:
        details.append(f"genre={info.gender}")
    if info.source:
        details.append(f"source={info.source}")
    return f"{speaker_id}: " + ", ".join(details)


def _candidate_window(
    candidate: Candidate,
    segments: list[dict],
    turns: list[dict],
    start: float,
    end: float,
    lexicon_terms: list[str],
) -> dict:
    selected = _slice_segments(segments, start, end)
    lines: list[str] = []
    for segment in selected:
        speaker = _speaker_for_segment(segment, turns)
        text = _clean_text(segment.get("text", ""))
        if text:
            lines.append(f"{speaker}: {text}")
    text = _clean_text(" ".join(_clean_text(segment.get("text", "")) for segment in selected))
    return {
        "code": candidate.code,
        "label": candidate.label,
        "text": text,
        "speaker_text": "\n".join(lines),
        "reliability": _worst_reliability(selected),
        "no_speech_prob": _max_no_speech_prob(selected),
        "low_word_ratio": _low_word_ratio(selected),
        "word_count": len(_words(text)),
        "segment_count": len(selected),
        "term_hits": _term_hits(text, lexicon_terms),
        "generic_hallucinations": _generic_hallucinations(text),
        "source_indices": [int(segment.get("_index", -1)) for segment in selected],
    }


def _context_summary(job_dir: Path, max_chars: int) -> str:
    meeting = _load_json(job_dir / "context" / "meeting_context.json") or {}
    parts = [
        meeting.get("title") or meeting.get("title_suggere") or "",
        meeting.get("meeting_type") or meeting.get("type_suggere") or "",
        meeting.get("objective") or meeting.get("objectif_suggere") or "",
        meeting.get("notes") or meeting.get("notes_suggeres") or "",
    ]
    text = _clean_text(" | ".join(str(part) for part in parts if part))
    if not text:
        summary_md = job_dir / "summary" / "summary.md"
        text = _clean_text(summary_md.read_text(encoding="utf-8", errors="replace") if summary_md.exists() else "")
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0] + "..."
    return text


def build_units(
    candidates: list[Candidate],
    jobs_dir: Path,
    speaker_job: str | None,
    lexicon_terms: list[str],
    window_s: float,
    context_chars: int,
) -> dict:
    loaded = [(candidate, load_segments(candidate.segments_path)) for candidate in candidates]
    reference = jobs_dir / (speaker_job or candidates[0].job_id)
    turns = _speaker_turns(reference)
    infos = _speaker_infos(reference)
    if turns:
        windows = build_windows([turns], window_s)
        logger.info("%d fenêtres construites depuis les tours locuteurs", len(windows))
    else:
        windows = build_windows([segments for _, segments in loaded], window_s)
        logger.warning("Aucun tour locuteur trouvé; fallback fenêtres fixes (%d)", len(windows))

    units: list[dict] = []
    for index, (start, end) in enumerate(windows):
        speakers = _speakers_in_window(turns, start, end)
        units.append({
            "segment_id": f"win_{index:05d}",
            "start": start,
            "end": end,
            "speakers": speakers,
            "speaker_context": [_speaker_line(speaker, infos) for speaker in speakers],
            "candidates": [
                _candidate_window(candidate, segments, turns, start, end, lexicon_terms)
                for candidate, segments in loaded
            ],
        })

    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "reference_job_id": speaker_job or candidates[0].job_id,
        "context_summary": _context_summary(reference, context_chars),
        "speaker_count": len(infos),
        "sources": [
            {
                "code": candidate.code,
                "label": candidate.label,
                "job_id": candidate.job_id,
                "segments_path": str(candidate.segments_path),
            }
            for candidate, _ in loaded
        ],
        "lexicon_terms": lexicon_terms,
        "units": units,
    }


def _technical_line(candidate: dict) -> str:
    nsp = candidate["no_speech_prob"]
    low = candidate["low_word_ratio"]
    fields = [
        f"rel={candidate['reliability']}",
        f"mots={candidate['word_count']}",
        f"segments={candidate['segment_count']}",
        f"no_speech={nsp:.2f}" if isinstance(nsp, float) else "no_speech=n/a",
        f"low_word={low:.0%}" if isinstance(low, float) else "low_word=n/a",
        f"termes={candidate['term_hits']}" if candidate["term_hits"] else "termes=[]",
    ]
    if candidate["generic_hallucinations"]:
        fields.append(f"hallucinations={candidate['generic_hallucinations']}")
    return ", ".join(fields)


def _available_choices(dataset: dict) -> list[str]:
    return [str(source["code"]) for source in dataset.get("sources") or []]


def _build_batch_prompt(dataset: dict, units: list[dict]) -> tuple[str, str]:
    choices = _available_choices(dataset)
    choice_text = ", ".join(choices)
    json_choice_text = "|".join([*choices, "D"])
    system_prompt = (
        "Tu es l'arbitre de transcription de TranscrIA. "
        f"Tu dois choisir la meilleure version parmi {choice_text} pour chaque segment, "
        "ou D si aucune version n'est suffisamment fiable et qu'une relecture ou une relance est nécessaire. "
        f"Tu ne dois jamais réécrire le texte. Tu choisis uniquement {json_choice_text}. "
        "Tiens compte des locuteurs, du contexte, des termes métier et des signaux techniques. "
        "Ne choisis pas une version qui mélange clairement deux locuteurs si une autre respecte mieux l'alternance. "
        "Réponds uniquement en JSON valide avec la forme "
        f'{{"decisions":[{{"segment_id":"win_00000","choice":"{json_choice_text}",'
        '"confidence":"high|medium|low","reason":"court","risks":["..."]}]}.'
    )
    lines = [
        f"Contexte réunion: {dataset.get('context_summary') or '(non disponible)'}",
        "",
        "Candidats:",
    ]
    for source in dataset["sources"]:
        lines.append(f"- {source['code']} = {source['label']} (job {source['job_id']})")
    lines += [
        "",
        "Rappel: D signifie aucune version fiable / à relire ou relancer. D n'est pas une erreur.",
        "",
        "Segments à arbitrer:",
        "",
    ]
    for unit in units:
        lines.extend([
            f"## {unit['segment_id']} [{unit['start']:.1f}s -> {unit['end']:.1f}s]",
            "Locuteurs:",
        ])
        if unit["speaker_context"]:
            lines.extend(f"- {line}" for line in unit["speaker_context"])
        else:
            lines.append("- non disponibles")
        for candidate in unit["candidates"]:
            lines.extend([
                "",
                f"{candidate['code']} ({candidate['label']}) — {_technical_line(candidate)}",
                candidate["speaker_text"] or candidate["text"] or "(vide)",
            ])
        lines.append("")
    return system_prompt, "\n".join(lines)


def filter_units_from_hybrid_report(dataset: dict, hybrid_json: Path) -> dict:
    report = _load_json(hybrid_json)
    if not isinstance(report, dict):
        raise SystemExit(f"Rapport hybride invalide: {hybrid_json}")
    review_keys = {
        (round(float(window.get("start", 0.0)), 3), round(float(window.get("end", 0.0)), 3))
        for window in report.get("windows") or []
        if isinstance(window, dict) and str(window.get("decision") or "") == "review"
    }
    filtered = dict(dataset)
    filtered["units"] = [
        unit for unit in dataset.get("units") or []
        if (round(float(unit.get("start", 0.0)), 3), round(float(unit.get("end", 0.0)), 3)) in review_keys
    ]
    filtered["unit_filter"] = {
        "source": str(hybrid_json),
        "mode": "review_windows",
        "requested_windows": len(review_keys),
        "kept_units": len(filtered["units"]),
    }
    return filtered


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _extract_json_string_field(text: str, field: str) -> str:
    pattern = rf'"{re.escape(field)}"\s*:\s*"'
    match = re.search(pattern, text)
    if not match:
        return ""
    start = match.end()
    stop_match = re.search(r'"\s*,\s*"(?:segment_id|choice|confidence|reason|risks)"\s*:', text[start:])
    if not stop_match:
        stop_match = re.search(r'"\s*[,}]', text[start:])
    if not stop_match:
        return ""
    return _clean_text(text[start:start + stop_match.start()])


def _parse_llm_response_lenient(text: str) -> dict:
    """Recover choices from almost-JSON LLM output.

    The model sometimes inserts unescaped quotes inside free-text reasons. The
    strict parser is preferred, but arbitration must not discard otherwise valid
    choices because a reason contains a quoted fragment.
    """
    raw = _strip_code_fence(text)
    decisions: list[dict] = []
    parts = re.split(r'(?=\{\s*"segment_id"\s*:)', raw)
    for part in parts:
        if '"segment_id"' not in part:
            continue
        segment_id = _extract_json_string_field(part, "segment_id")
        choice = _extract_json_string_field(part, "choice").upper()
        if choice not in {"A", "B", "C", "D"} or not segment_id:
            continue
        confidence = _extract_json_string_field(part, "confidence").lower()
        if confidence not in {"high", "medium", "low"}:
            confidence = "low"
        reason = _extract_json_string_field(part, "reason")
        risks: list[str] = []
        risks_match = re.search(r'"risks"\s*:\s*\[(.*?)\]', part, flags=re.DOTALL)
        if risks_match:
            risks = [
                _clean_text(item)
                for item in re.findall(r'"((?:[^"\\]|\\.)*)"', risks_match.group(1), flags=re.DOTALL)
                if _clean_text(item)
            ]
        decisions.append({
            "segment_id": segment_id,
            "choice": choice,
            "confidence": confidence,
            "reason": reason or "décision récupérée depuis une réponse JSON non stricte",
            "risks": risks,
        })
    if not decisions:
        raise ValueError("Réponse LLM invalide: aucune décision récupérable")
    return {"decisions": decisions, "parse_warning": "json_lenient_recovery"}


def parse_llm_response(text: str) -> dict:
    raw = _strip_code_fence(text)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not match:
            return _parse_llm_response_lenient(text)
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return _parse_llm_response_lenient(text)
    if not isinstance(data, dict) or not isinstance(data.get("decisions"), list):
        raise ValueError("Réponse LLM invalide: champ decisions manquant")
    for item in data["decisions"]:
        if not isinstance(item, dict):
            raise ValueError("Décision invalide: entrée non objet")
        choice = str(item.get("choice") or "").upper()
        if choice not in {"A", "B", "C", "D"}:
            raise ValueError(f"Décision invalide pour {item.get('segment_id')}: {choice!r}")
        item["choice"] = choice
    return data


def call_llm(system_prompt: str, user_content: str, port: int, model_id: str, timeout: int, max_tokens: int) -> LlmCallResult:
    url = f"http://127.0.0.1:{port}/v1/chat/completions"
    payload = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.1,
        "top_p": 0.9,
        "max_tokens": max_tokens,
        "stream": False,
    }
    logger.info("Appel LLM %s modèle=%s prompt=%d mots", url, model_id, len(user_content.split()))
    start = time.monotonic()
    response = requests.post(url, json=payload, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") if isinstance(choice, dict) else {}
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    content = str((message or {}).get("content") or "")
    finish_reason = str(choice.get("finish_reason") or "")
    completion_tokens = usage.get("completion_tokens")
    logger.info(
        "Réponse LLM reçue en %.1fs finish_reason=%s completion_tokens=%s content_chars=%d",
        time.monotonic() - start,
        finish_reason or "n/a",
        completion_tokens if completion_tokens is not None else "n/a",
        len(content),
    )
    return LlmCallResult(
        content=content,
        finish_reason=finish_reason,
        completion_tokens=int(completion_tokens) if isinstance(completion_tokens, int) else None,
        response=data,
    )


def _batches(items: list[dict], size: int) -> list[list[dict]]:
    return [items[index:index + size] for index in range(0, len(items), size)]


def _write_prompt_files(output_dir: Path, namespace: str, batch_id: str, system_prompt: str, user_prompt: str) -> dict[str, str]:
    prompt_dir = output_dir / "prompts" / namespace
    prompt_dir.mkdir(parents=True, exist_ok=True)
    system_path = prompt_dir / f"{batch_id}_system.txt"
    user_path = prompt_dir / f"{batch_id}_user.txt"
    system_path.write_text(system_prompt, encoding="utf-8")
    user_path.write_text(user_prompt, encoding="utf-8")
    return {
        "system_prompt_path": str(system_path),
        "user_prompt_path": str(user_path),
    }


def _candidate_by_choice(unit: dict, choice: str) -> dict | None:
    for candidate in unit.get("candidates") or []:
        if str(candidate.get("code") or "").upper() == choice:
            return candidate
    return None


def _enrich_decision_audit(unit: dict, decision: dict) -> dict:
    choice = str(decision.get("choice") or "").upper()
    warnings: list[str] = []
    if choice == "D":
        warnings.append("manual_review_required")
        return {**decision, "audit_warnings": warnings}

    candidate = _candidate_by_choice(unit, choice)
    if not candidate:
        warnings.append("selected_candidate_missing")
        return {**decision, "audit_warnings": warnings}

    reliability = str(candidate.get("reliability") or "")
    no_speech_prob = candidate.get("no_speech_prob")
    low_word_ratio = candidate.get("low_word_ratio")
    generic_hallucinations = candidate.get("generic_hallucinations") or []

    if reliability == "degrade":
        warnings.append("selected_candidate_degrade")
    elif reliability == "suspect":
        warnings.append("selected_candidate_suspect")
    if decision.get("confidence") == "high" and reliability != "ok":
        warnings.append("high_confidence_on_non_ok_candidate")
    if isinstance(no_speech_prob, float) and no_speech_prob >= 0.65:
        warnings.append("selected_high_no_speech_prob")
    if isinstance(low_word_ratio, float) and low_word_ratio >= 0.15:
        warnings.append("selected_high_low_word_ratio")
    if generic_hallucinations:
        warnings.append("selected_generic_hallucination")

    return {
        **decision,
        "selected_label": candidate.get("label"),
        "selected_reliability": reliability,
        "selected_no_speech_prob": no_speech_prob,
        "selected_low_word_ratio": low_word_ratio,
        "selected_word_count": candidate.get("word_count"),
        "selected_generic_hallucinations": generic_hallucinations,
        "audit_warnings": warnings,
    }


def run_arbitration(dataset: dict, args: argparse.Namespace) -> dict:
    output_dir = args.output_json.parent
    namespace = args.output_json.stem
    decisions: dict[str, dict] = {}
    batches_report: list[dict] = []
    raw_dir = output_dir / "raw_llm" / namespace
    raw_dir.mkdir(parents=True, exist_ok=True)

    for batch_index, units in enumerate(_batches(dataset["units"], args.batch_size)):
        batch_id = f"batch_{batch_index:04d}"
        system_prompt, user_prompt = _build_batch_prompt(dataset, units)
        prompt_paths = _write_prompt_files(output_dir, namespace, batch_id, system_prompt, user_prompt)
        report = {
            "batch_id": batch_id,
            "segment_ids": [unit["segment_id"] for unit in units],
            "status": "dry_run" if args.dry_run else "pending",
            "parsed": False,
            **prompt_paths,
            "system_prompt_chars": len(system_prompt),
            "user_prompt_chars": len(user_prompt),
            "user_prompt_words": len(user_prompt.split()),
        }
        if args.dry_run:
            batches_report.append(report)
            continue
        try:
            llm_result = call_llm(system_prompt, user_prompt, args.arbitrage_port, args.model_id, args.timeout, args.max_tokens)
            raw_path = raw_dir / f"{batch_id}.txt"
            api_response_path = raw_dir / f"{batch_id}.response.json"
            raw_path.write_text(llm_result.content, encoding="utf-8")
            api_response_path.write_text(json.dumps(llm_result.response, ensure_ascii=False, indent=2), encoding="utf-8")
            report.update({
                "raw_response_path": str(raw_path),
                "api_response_path": str(api_response_path),
                "finish_reason": llm_result.finish_reason,
                "completion_tokens": llm_result.completion_tokens,
            })
            if not llm_result.content.strip():
                if llm_result.finish_reason == "length":
                    raise ValueError("Réponse LLM vide: budget max_tokens épuisé dans le raisonnement")
                raise ValueError("Réponse LLM vide: champ message.content absent")
            parsed = parse_llm_response(llm_result.content)
            units_by_id = {str(unit.get("segment_id")): unit for unit in units}
            for item in parsed["decisions"]:
                segment_id = str(item.get("segment_id"))
                decisions[segment_id] = _enrich_decision_audit(units_by_id.get(segment_id, {}), item)
            report.update({"status": "done", "parsed": True})
            if parsed.get("parse_warning"):
                report["parse_warning"] = parsed["parse_warning"]
        except Exception as exc:
            logger.error("Batch %s échoué: %s", batch_id, exc)
            report.update({"status": "error", "error": str(exc)})
            for unit in units:
                decisions[unit["segment_id"]] = {
                    "segment_id": unit["segment_id"],
                    "choice": "D",
                    "confidence": "low",
                    "reason": "échec parsing/appel LLM",
                    "risks": [str(exc)],
                    "audit_warnings": ["manual_review_required", "llm_batch_error"],
                }
        batches_report.append(report)

    return {
        "batches": batches_report,
        "decisions": decisions,
    }


def _selected_text(unit: dict, decision: dict | None) -> tuple[str, str]:
    choice = str((decision or {}).get("choice") or "D").upper()
    if choice == "D":
        best = max(unit["candidates"], key=lambda item: (item["reliability"] == "ok", item["word_count"]))
        return best["text"], "D"
    for candidate in unit["candidates"]:
        if candidate["code"] == choice:
            return candidate["text"], choice
    return "", "D"


def write_srt(dataset: dict, arbitration: dict, output: Path, max_words: int) -> None:
    lines: list[str] = []
    index = 1
    for unit in dataset["units"]:
        decision = arbitration["decisions"].get(unit["segment_id"])
        text, choice = _selected_text(unit, decision)
        chunks = split_text_for_srt(text, max_words)
        if not chunks:
            continue
        start = float(unit["start"])
        end = float(unit["end"])
        duration = max(0.5, end - start)
        chunk_duration = duration / len(chunks)
        for offset, chunk in enumerate(chunks):
            cue_start = start + offset * chunk_duration
            cue_end = min(end, cue_start + chunk_duration)
            prefix = "[À relire] " if choice == "D" else ""
            lines.extend([
                str(index),
                f"{_format_time(cue_start)} --> {_format_time(cue_end)}",
                prefix + chunk,
                "",
            ])
            index += 1
    output.write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Arbitre A/B/C par lots LLM avec contexte locuteurs.")
    parser.add_argument("--jobs-dir", type=Path, default=Path("jobs"))
    parser.add_argument(
        "--candidate",
        action="append",
        required=True,
        help="Format A:cohere=job_id, B:whisper=job_id, C:whisper_hotwords=job_id",
    )
    parser.add_argument("--speaker-job", help="Job de référence pour speaker_turns/speaker_stats. Défaut: candidat A")
    parser.add_argument("--lexicon-json", type=Path, action="append", default=[])
    parser.add_argument("--window-s", type=float, default=45.0)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--context-chars", type=int, default=1200)
    parser.add_argument("--arbitrage-port", type=int, default=8080)
    parser.add_argument("--model-id", default="qwen3-35b-arbitrage-ud-q8_k_xl")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--review-from-hybrid-json",
        type=Path,
        help="N'arbitre que les fenêtres decision=review d'un rapport build_hybrid_transcript.",
    )
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-srt", type=Path, required=True)
    parser.add_argument("--max-srt-words", type=int, default=18)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    candidates = [_parse_candidate(raw, args.jobs_dir) for raw in args.candidate]
    codes = [candidate.code for candidate in candidates]
    if len(codes) != len(set(codes)):
        raise SystemExit("Les codes candidats doivent être uniques")
    if "A" not in codes or "B" not in codes or len(codes) not in {2, 3}:
        raise SystemExit("Il faut les candidats A et B, avec C optionnel")
    for candidate in candidates:
        if not candidate.segments_path.exists():
            raise SystemExit(f"Segments introuvables pour {candidate.code}: {candidate.segments_path}")
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_srt.parent.mkdir(parents=True, exist_ok=True)

    lexicon_terms = _load_lexicon_terms(args.lexicon_json)
    dataset = build_units(candidates, args.jobs_dir, args.speaker_job, lexicon_terms, args.window_s, args.context_chars)
    if args.review_from_hybrid_json:
        dataset = filter_units_from_hybrid_report(dataset, args.review_from_hybrid_json)
    arbitration = run_arbitration(dataset, args)
    result = {
        "tool": "arbitrate_hybrid_llm",
        "version": 1,
        "dry_run": args.dry_run,
        "dataset": dataset,
        "arbitration": arbitration,
    }
    args.output_json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    write_srt(dataset, arbitration, args.output_srt, args.max_srt_words)
    logger.info(
        "Arbitrage écrit: %s, SRT=%s, fenêtres=%d, batches=%d",
        args.output_json,
        args.output_srt,
        len(dataset["units"]),
        len(arbitration["batches"]),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
