import json
from datetime import datetime, timezone

import yaml

from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job


class JobContextBuilder:
    @staticmethod
    def build(job: Job, jobs_dir: str, config: dict | None = None) -> dict:
        fs = JobFilesystem(jobs_dir, job.id)

        meeting = fs.load_json("context/meeting_context.json") or {}
        participants = fs.load_json("context/participants.json") or []
        speakers_data = fs.load_json("speakers/speaker_mapping.json") or {}
        lexicon = fs.load_json("context/session_lexicon.json") or []
        quality_hints = JobContextBuilder._build_quality_hints(fs)

        context = {
            "job_id": job.id,
            "owner_user_id": job.owner_id,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "meeting": {
                "title": meeting.get("title", ""),
                "type": meeting.get("meeting_type", ""),
                "date": meeting.get("date", ""),
                "language": meeting.get("language", "fr"),
                "summary_control": meeting.get("summary") or meeting.get("summary_llm", ""),
                "notes": meeting.get("notes", ""),
                **({"type_specific": meeting["type_specific_data"]}
                   if meeting.get("type_specific_data") else {}),
            },
            "participants": [
                {
                    "id": p.get("id", ""),
                    "name": p.get("name", ""),
                    "function": p.get("function", ""),
                    "role": p.get("role", ""),
                    "expected": p.get("expected", True),
                }
                for p in participants
            ],
            "speakers": [
                {
                    "speaker_id": s.get("speaker_id", ""),
                    "mapped_to": s.get("mapped_to"),
                    "mapped_name": s.get("mapped_name", ""),
                    "speaking_time_seconds": s.get("speaking_time_seconds", 0),
                    "validation": s.get("validation", "pending"),
                }
                for s in speakers_data.get("speakers", [])
            ],
            "lexicon": [
                {
                    "term": t.get("term", ""),
                    "category": t.get("category", ""),
                    "priority": t.get("priority", "normale"),
                    "variants": t.get("variants", []),
                    "replace_by": t.get("replace_by", ""),
                    "comment": t.get("comment", ""),
                    "contexts": t.get("contexts", []),
                }
                for t in lexicon
            ],
            "processing": {
                "default_stt_model": (config or {}).get("models", {}).get(
                    "cohere_model", "cohere-transcribe-03-2026"
                ),
                "diarization_model": (config or {}).get("models", {}).get(
                    "pyannote_model", "pyannote/speaker-diarization-community-1"
                ),
            },
        }
        if quality_hints:
            context["quality_hints"] = quality_hints

        fs.save_text("context/job_context.yaml", yaml.dump(context, allow_unicode=True, default_flow_style=False))
        fs.save_text("context/job_context.json", json.dumps(context, ensure_ascii=False, indent=2, default=str))
        return context

    @staticmethod
    def _build_quality_hints(fs: JobFilesystem) -> dict:
        """Construit des indices prudents pour la LLM, sans valeur d'autorité."""
        preflight = fs.load_json("metadata/audio_preflight.json") or {}
        quality_decision = fs.load_json("metadata/audio_quality_decision.json") or {}
        audio_scene = fs.load_json("metadata/audio_scene.json") or {}
        segments = fs.load_json("metadata/transcription_segments.json") or []

        audio = JobContextBuilder._build_audio_quality_hint(preflight, quality_decision, audio_scene)
        segment_hints = JobContextBuilder._build_segment_quality_hints(segments)
        if not audio and not segment_hints:
            return {}

        return {
            "usage": (
                "Indices de prudence uniquement. Ils signalent des zones où la transcription peut être moins fiable, "
                "mais ne justifient jamais d'inventer un mot, un nom, un rôle ou une correction."
            ),
            "audio": audio,
            "segments": segment_hints,
        }

    @staticmethod
    def _build_audio_quality_hint(preflight: dict, quality_decision: dict, audio_scene: dict) -> dict:
        flags = [str(flag) for flag in preflight.get("flags", []) if flag]
        level = preflight.get("risk_level") or quality_decision.get("level") or ""
        reasons = JobContextBuilder._audio_flag_labels(flags)

        scene_summary = {}
        if audio_scene:
            scene_summary = {
                "has_music": bool(audio_scene.get("has_music", False)),
                "has_noise": bool(audio_scene.get("has_noise", False)),
                "speech_ratio": audio_scene.get("speech_ratio"),
                "music_ratio": audio_scene.get("music_ratio"),
                "noise_ratio": audio_scene.get("noise_ratio"),
                "problem_segments": [
                    {
                        "label": s.get("label", ""),
                        "start": s.get("start"),
                        "end": s.get("end"),
                        "duration_s": s.get("duration_s"),
                    }
                    for s in (audio_scene.get("problem_segments") or [])[:10]
                    if isinstance(s, dict)
                ],
            }

        metrics = {
            "rms": preflight.get("rms"),
            "estimated_snr_db": preflight.get("estimated_snr_db"),
            "silence_ratio": preflight.get("silence_ratio"),
            "bandwidth_95_hz": preflight.get("bandwidth_95_hz"),
            "bandwidth_99_hz": preflight.get("bandwidth_99_hz"),
            "clipping_ratio": preflight.get("clipping_ratio"),
        }
        metrics = {k: v for k, v in metrics.items() if v is not None}

        hint = {
            "level": level,
            "flags": flags,
            "reasons": reasons,
            "metrics": metrics,
            "scene": scene_summary,
        }
        return {k: v for k, v in hint.items() if v not in ("", [], {})}

    @staticmethod
    def _build_segment_quality_hints(segments: list) -> list[dict]:
        if not isinstance(segments, list):
            return []

        hints = []
        for index, segment in enumerate(segments, start=1):
            if not isinstance(segment, dict):
                continue
            reliability = segment.get("reliability") or segment.get("reliability_level")
            reasons = segment.get("reliability_reasons") or segment.get("quality_reasons") or []
            no_speech_prob = segment.get("no_speech_prob")
            avg_word_probability = segment.get("avg_word_probability")
            is_suspect = reliability in {"suspect", "degrade"} or bool(reasons)
            if not is_suspect:
                continue

            text = str(segment.get("text", "") or "").strip()
            hints.append({
                "segment_index": index,
                "start": segment.get("start"),
                "end": segment.get("end"),
                "speaker": segment.get("speaker", ""),
                "level": reliability or "suspect",
                "reasons": [str(reason) for reason in reasons if reason],
                "no_speech_prob": no_speech_prob,
                "avg_word_probability": avg_word_probability,
                "text_excerpt": text[:180],
            })
            if len(hints) >= 20:
                break
        return hints

    @staticmethod
    def _audio_flag_labels(flags: list[str]) -> list[str]:
        labels = {
            "audio_tres_faible": "volume très faible",
            "audio_faible": "volume faible",
            "snr_faible": "bruit de fond proche de la voix",
            "bande_etroite": "bande passante limitée",
            "clipping_detecte": "saturation détectée",
            "risque_transcription_non_fiable": "risque de transcription moins fiable",
        }
        return [labels.get(flag, flag) for flag in flags]
