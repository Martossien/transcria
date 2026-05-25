import logging
import time

from transcria.jobs.models import Job, JobState
from transcria.jobs.store import JobStore
from transcria.gpu.vram_manager import VRAMManager
from transcria.gpu.gpu_session import GPUSession, GPUSessionError
from transcria.logging_setup import get_structured_logger

logger = logging.getLogger(__name__)


class WorkflowRunner:
    def __init__(self, store: JobStore, config: dict | None = None):
        self.store = store
        self.config = config or {}
        self.vram = VRAMManager(config=self.config)

    @staticmethod
    def _cuda_available() -> bool:
        try:
            import torch
            return bool(torch.cuda.is_available())
        except Exception:
            return False

    def run_analyze(self, job: Job, audio_path: str) -> dict:
        from pathlib import Path
        from transcria.audio.analyzer import AudioAnalyzer

        result = AudioAnalyzer.analyze(Path(audio_path))
        self.store.update(job.id, state=JobState.ANALYZED.value)
        return result

    def run_summary(self, job: Job, audio_path: str, config: dict) -> dict:
        sl = get_structured_logger(__name__)
        sl.set_context(job_id=job.id, step="summary")

        self.store.update_state(job.id, JobState.SUMMARY_RUNNING)
        t0 = time.monotonic()
        sl.info("━━━ DÉBUT résumé ━━━")

        backend = config.get("models", {}).get("stt_backend", "cohere")
        sl.info("[1/3] STT rapide — chargement GPU", backend=backend)
        result = self._run_quick_transcription(job, audio_path, config, sl)
        sl.info(
            "[1/3] STT rapide terminé — %d segments, %.1fs",
            result.get("segment_count", 0),
            time.monotonic() - t0,
            backend=backend,
        )
        if result.get("error") and not result.get("transcript_text"):
            sl.error("[1/3] STT rapide ÉCHEC — abandon résumé", error=result["error"], backend=backend)
            return result

        sl.info("[2/4] Analyse de scène audio — début")
        self._run_audio_scene_before_participants(job, audio_path, config, sl)

        sl.info("[3/4] Pyannote diarization — début")
        self._run_pyannote_after_transcription(job, audio_path, config)
        sl.info("[3/4] Pyannote diarization terminé, %.1fs écoulées", time.monotonic() - t0)

        sl.info("[4/4] LLM résumé via arbitrage — début")
        self._run_llm_summary(job, result, config, sl)
        sl.info("[4/4] LLM résumé terminé, %.1fs écoulées", time.monotonic() - t0)

        self.store.update_state(job.id, JobState.SUMMARY_DONE)
        sl.info("━━━ FIN résumé ━━━ (%.1fs total)", time.monotonic() - t0,
                transcript_chars=len(result.get("transcript_text", "")))
        return result

    @staticmethod
    def _get_fs(config: dict, job_id: str):
        from transcria.jobs.filesystem import JobFilesystem
        return JobFilesystem(
            config.get("storage", {}).get("jobs_dir", "./jobs"), job_id
        )

    def _run_audio_scene_before_participants(
        self, job: Job, audio_path: str, config: dict, sl
    ) -> dict:
        """Produit audio_scene.json avant l'étape participants si la scène est activée."""
        from pathlib import Path

        scene_cfg = config.get("workflow", {}).get("audio_scene", {}) or {}
        if not scene_cfg.get("enabled", False):
            sl.debug("[summary] Analyse de scène désactivée")
            return {}

        fs = self._get_fs(config, job.id)
        existing = fs.load_json("metadata/audio_scene.json") or {}
        if existing:
            sl.info("[summary] Analyse de scène déjà disponible")
            return existing

        try:
            from transcria.audio.scene_analyzer import AudioSceneAnalyzer
            from transcria.quality.audio_quality import AudioQualityEvaluator

            analyzer = AudioSceneAnalyzer(config)
            scene = analyzer.analyze(Path(audio_path))
            if not scene:
                sl.warning("[summary] Analyse de scène indisponible")
                return {}

            fs.save_json("metadata/audio_scene.json", scene)
            summary = fs.load_json("summary/summary.json") or {}
            audio_analysis = fs.load_json("metadata/audio_analysis.json") or {}
            evaluation = AudioQualityEvaluator(config).evaluate(
                audio_analysis,
                summary,
                audio_scene=scene,
            )
            fs.save_json("metadata/audio_quality_decision.json", evaluation)
            sl.info(
                "[summary] Analyse de scène terminée",
                has_gender_data=(scene.get("gender") or {}).get("has_gender_data"),
                gender_segments=len(scene.get("gender_segments") or []),
                quality_level=evaluation.get("level"),
            )
            return scene
        except Exception as exc:
            sl.warning("[summary] Analyse de scène ignorée", error=str(exc))
            return {}

    def _run_quick_transcription(
        self, job: Job, audio_path: str, config: dict, sl
    ) -> dict:
        from pathlib import Path
        from transcria.stt.summary import SummaryGenerator
        from transcria.stt.transcriber_factory import get_backend_vram_mb

        backend = config.get("models", {}).get("stt_backend", "cohere")
        vram_mb = get_backend_vram_mb(backend, config)
        try:
            with GPUSession(
                self.vram, f"{backend}-summary", vram_mb
            ) as gs:
                generator = SummaryGenerator(config)
                result = generator.generate_quick_summary(
                    job, Path(audio_path), gpu_index=gs.gpu_index
                )
                sl.info(
                    "STT rapide OK",
                    backend=backend,
                    segments=result.get("segment_count", 0),
                    transcript_chars=len(result.get("transcript_text", "")),
                )
        except GPUSessionError as exc:
            sl.warning("VRAM insuffisante pour le STT rapide", backend=backend, required_vram_mb=vram_mb, error=str(exc))
            self.store.update_state(job.id, JobState.FAILED, str(exc))
            return {
                "error": str(exc),
                "transcript_text": "",
                "summary_text": "Résumé indisponible.",
            }
        except Exception as exc:
            sl.exception("Échec STT rapide", backend=backend)
            self.vram.offload_all()
            self.store.update_state(job.id, JobState.FAILED, str(exc))
            return {
                "error": str(exc),
                "transcript_text": "",
                "summary_text": "Résumé indisponible.",
            }

        return result

    def _run_pyannote_after_transcription(
        self, job: Job, audio_path: str, config: dict
    ) -> None:
        if not config.get("workflow", {}).get("enable_speaker_detection", True):
            return

        try:
            speakers_result = self.run_speaker_detection(job, audio_path, config)
            if not speakers_result.get("available") or not speakers_result.get("speakers"):
                return

            fs = self._get_fs(config, job.id)
            meeting_ctx = fs.load_json("context/meeting_context.json") or {}
            meeting_ctx["speaker_count_pyannote"] = len(speakers_result["speakers"])
            fs.save_json("context/meeting_context.json", meeting_ctx)
            audio_scene = fs.load_json("metadata/audio_scene.json") or {}
            speaker_genders = self._inject_speaker_genders(fs, audio_scene)
            self._write_diarization_context(
                fs, speakers_result, audio_scene, speaker_genders
            )

            logger.info("pyannote: %d locuteurs détectés",
                        len(speakers_result["speakers"]))
        except Exception as exc:
            logger.warning("pyannote après transcription ignoré: %s", exc)

    def _run_llm_summary(
        self, job: Job, result: dict, config: dict, sl
    ) -> None:
        llm_config = config.get("workflow", {}).get("summary_llm", {})
        if not llm_config.get("enabled"):
            sl.info("LLM résumé désactivé dans la config")
            return
        if not result.get("transcript_text"):
            sl.warning("LLM résumé sauté — transcription vide")
            return

        from transcria.gpu.opencode_runner import OpenCodeRunner

        fs = self._get_fs(config, job.id)
        transcript_path = fs.job_dir / "summary" / "quick_transcript.txt"
        context_path = fs.job_dir / "context" / "job_context.yaml"
        diarization_ctx_path = fs.job_dir / "summary" / "diarization_context.md"

        api_model_id = config.get("services", {}).get("arbitrage_api_model_id")
        arbitrage_port = config.get("services", {}).get("arbitrage_llm_port", 8080)
        sl.info(
            "LLM résumé: vérification LLM d'arbitrage (modèle attendu: %s, port %d)",
            api_model_id or "non contraint",
            arbitrage_port,
        )
        launched = self.vram.ensure_arbitrage_llm_ready(expected_model_id=api_model_id)

        if not launched:
            sl.warning("LLM d'arbitrage non disponible — résumé LLM sauté (transcription rapide conservée)")
            return

        try:
            model_id = llm_config.get("model_id")
            opencode_bin = config.get("workflow", {}).get(
                "arbitration_llm", {}
            ).get("opencode_bin")
            runner = OpenCodeRunner(
                str(fs.job_dir / "summary"),
                model=model_id,
                opencode_bin=opencode_bin,
                config=config,
            )
            parsed = runner.run_summary(
                str(transcript_path),
                str(context_path),
                str(diarization_ctx_path),
            )
            self._apply_llm_suggestions(fs, result, parsed, sl)
        except Exception as exc:
            logger.warning("Erreur opencode: %s", exc)

    @staticmethod
    def _apply_llm_suggestions(fs, result: dict, parsed: dict, sl) -> None:
        summary_text = parsed.get("summary_text", "")
        if not summary_text or summary_text.strip() == "Résumé indisponible.":
            logger.warning("_apply_llm_suggestions: résumé indisponible — meeting_context non mis à jour")
            return

        result["summary_text"] = summary_text
        meeting_ctx = fs.load_json("context/meeting_context.json") or {}

        suggestion_fields = [
            "title_suggere", "type_suggere", "sujet_suggere",
            "objectif_suggere", "notes_suggeres", "participants_detectes",
        ]
        for field in suggestion_fields:
            if parsed.get(field):
                meeting_ctx[field] = parsed[field]

        empty_fields = [f for f in suggestion_fields if not parsed.get(f)]
        if empty_fields:
            logger.warning("_apply_llm_suggestions: champs LLM non renseignés — %s", empty_fields)

        if parsed.get("speaker_count", 0) > 0:
            meeting_ctx["speaker_count_llm"] = parsed["speaker_count"]
        termes_suspects = parsed.get("termes_suspects") or []
        meeting_ctx["termes_suspects"] = termes_suspects
        meeting_ctx["termes_suspects_parse_status"] = parsed.get("termes_suspects_parse_status", "missing")
        parse_warning = parsed.get("termes_suspects_parse_warning", "")
        if parse_warning:
            meeting_ctx["termes_suspects_parse_warning"] = parse_warning
        else:
            meeting_ctx.pop("termes_suspects_parse_warning", None)

        meeting_ctx["summary_llm"] = summary_text
        # Stocker les rôles LLM dans meeting_context pour que l'UI puisse les afficher
        # et qu'ils puissent être réappliqués après la création du mapping
        speaker_roles = parsed.get("speaker_roles", {})
        if speaker_roles:
            meeting_ctx["speaker_roles_llm"] = speaker_roles
        fs.save_json("context/meeting_context.json", meeting_ctx)

        # Tentative d'application immédiate des rôles (fonctionne si speaker_mapping.json existe déjà)
        if speaker_roles:
            WorkflowRunner._apply_speaker_roles(fs, speaker_roles, sl)

        # summary_text commence déjà par "# Résumé de contrôle" (écrit par opencode).
        # On n'ajoute que la section transcript en fin de fichier.
        transcript_short = result.get("transcript_short", "")
        fs.save_text(
            "summary/summary.md",
            summary_text
            + (
                f"\n\n---\n\n## Extrait de transcription\n\n{transcript_short}\n"
                if transcript_short
                else "\n"
            ),
        )
        sl.info("Résumé LLM généré", chars=len(summary_text), termes_suspects=len(termes_suspects))

    @staticmethod
    def _normalize_speaker_role_info(info: dict) -> dict:
        """Normalise les anciens formats où le label était inclus dans le rôle."""
        import re

        label = str(info.get("label", "") or "").strip()
        role = str(info.get("role", "") or "").strip()
        if not label and role:
            split = re.split(r"\s+[—–-]\s+", role, maxsplit=1)
            if len(split) == 2 and split[0].strip() and split[1].strip():
                label = split[0].strip()
                role = split[1].strip()
        return {"label": label, "role": role}

    @staticmethod
    def _apply_speaker_roles(fs, speaker_roles: dict, sl) -> None:
        """Met à jour participants.json avec les rôles déduits par la LLM pour chaque SPEAKER_XX."""
        mapping_data = fs.load_json("speakers/speaker_mapping.json") or {}
        mapping = mapping_data.get("mapping", {})
        participants = fs.load_json("context/participants.json") or []
        if not isinstance(participants, list):
            participants = []

        # Index participants par id et par nom (insensible à la casse)
        by_id = {p["id"]: p for p in participants if p.get("id")}
        by_name = {p["name"].lower(): p for p in participants if p.get("name")}

        updated = 0
        created = 0
        for speaker_id, info in speaker_roles.items():
            normalized = WorkflowRunner._normalize_speaker_role_info(info)
            role = normalized["role"]
            label = normalized["label"]
            if not role:
                continue

            # Trouver le participant via speaker_mapping → participant_id ou nom
            participant = None
            spk_map = mapping.get(speaker_id, {})
            pid = spk_map.get("participant_id", "")
            name = spk_map.get("name", "")

            if pid and pid in by_id:
                participant = by_id[pid]
            elif name and name.lower() in by_name:
                participant = by_name[name.lower()]

            if participant is not None:
                if label and participant.get("name") in ("", speaker_id):
                    participant["name"] = label
                if not participant.get("role"):
                    participant["role"] = role
                    updated += 1
                else:
                    current_role = str(participant.get("role", "") or "").strip()
                    current_normalized = WorkflowRunner._normalize_speaker_role_info(
                        {"label": "", "role": current_role}
                    )
                    if current_normalized["label"] and current_normalized["role"]:
                        participant["role"] = current_normalized["role"]
                        updated += 1
            else:
                # Créer une entrée minimale si participants.json est vide ou SPEAKER_XX inconnu
                new_p = {
                    "id": speaker_id.lower().replace("_", ""),
                    "name": label or name or speaker_id,
                    "function": "",
                    "service": "",
                    "role": role,
                    "is_animator": False,
                    "expected": True,
                    "comment": "",
                }
                participants.append(new_p)
                by_id[new_p["id"]] = new_p
                created += 1

        if updated or created:
            fs.save_json("context/participants.json", participants)
            sl.info("Rôles LLM → participants.json : %d mis à jour, %d créés", updated, created)

        # Propager les noms LLM dans speaker_stats.json et speaker_mapping.json
        # même si participants.json était déjà à jour (appel idempotent).
        # Ne jamais remplacer un nom déjà validé par l'utilisateur : la LLM ne
        # sert ici qu'à préremplir les champs encore vides ou restés SPEAKER_XX.
        speakers_data = fs.load_json("speakers/speaker_stats.json") or {}
        spk_stats = speakers_data.get("speakers", [])
        mapping_data = fs.load_json("speakers/speaker_mapping.json") or {}
        spk_map = mapping_data.get("mapping", {})
        spk_map_speakers = mapping_data.get("speakers", [])
        propagated = 0
        mapping_changed = False
        for speaker_id, info in speaker_roles.items():
            norm = WorkflowRunner._normalize_speaker_role_info(info)
            label = norm["label"]
            if not label:
                continue
            for spk in spk_stats:
                if spk.get("speaker_id") == speaker_id:
                    current = str(spk.get("mapped_name", "") or "").strip()
                    if current in {"", speaker_id}:
                        spk["mapped_name"] = label
                        propagated += 1
            if speaker_id in spk_map:
                current = str(spk_map[speaker_id].get("name", "") or "").strip()
                if current in {"", speaker_id}:
                    spk_map[speaker_id]["name"] = label
                    mapping_changed = True
            for ms in spk_map_speakers:
                if ms.get("speaker_id") == speaker_id:
                    current = str(ms.get("mapped_name", "") or "").strip()
                    if current in {"", speaker_id}:
                        ms["mapped_name"] = label
                        mapping_changed = True
        if propagated:
            fs.save_json("speakers/speaker_stats.json", {"speakers": spk_stats})
        if mapping_changed:
            if spk_map or spk_map_speakers:
                fs.save_json(
                    "speakers/speaker_mapping.json",
                    {"mapping": spk_map, "speakers": spk_map_speakers},
                )
        if propagated:
            sl.info("Rôles LLM → speaker_stats.json propagés : %d locuteur(s)", propagated)

    @staticmethod
    def _truncate_at_word(text: str, max_chars: int = 120) -> str:
        """Coupe à max_chars caractères en respectant la frontière de mot la plus proche."""
        if len(text) <= max_chars:
            return text
        cut = text[:max_chars].rsplit(" ", 1)
        return (cut[0] if len(cut) > 1 else text[:max_chars]) + "…"

    @staticmethod
    def _build_labeled_segments(
        fs, speakers_result: dict
    ) -> list[tuple[str, str]]:
        """Pour chaque segment ASR, attribue le texte à un locuteur uniquement si
        un seul SPEAKER_XX a des tours pyannote dans ce segment.

        Dès que deux locuteurs distincts se chevauchent avec le segment, le texte
        contient les deux voix et ne peut pas être attribué sans timestamps mot par
        mot — le segment est ignoré sans alignement mot-à-mot fiable.
        Retourne une liste ordonnée (speaker_id, texte).
        """
        turns_data = speakers_result.get("turns") or []
        segments_data = (fs.load_json("summary/summary.json") or {}).get("segments") or []
        if not turns_data or not segments_data:
            return []

        result = []
        for seg in segments_data:
            text = seg.get("text", "").strip()
            if not text:
                continue
            s_start, s_end = seg.get("start", 0.0), seg.get("end", 0.0)
            if s_end <= s_start:
                continue

            # Chevauchement par locuteur
            overlap: dict[str, float] = {}
            for turn in turns_data:
                ov = min(turn["end"], s_end) - max(turn["start"], s_start)
                if ov > 0:
                    spk = turn["speaker"]
                    overlap[spk] = overlap.get(spk, 0.0) + ov

            if not overlap:
                continue  # aucun tour pyannote — segment ignoré

            # N'attribuer que si UN SEUL locuteur distinct a des tours dans ce segment.
            # Dès que deux locuteurs différents se chevauchent avec le segment ASR,
            # le texte contient les deux voix — impossible de l'attribuer sans timestamps
            # mot par mot fiable.
            unique_speakers = set(overlap.keys())
            if len(unique_speakers) == 1:
                label = next(iter(unique_speakers))
                result.append((label, WorkflowRunner._truncate_at_word(text, 200)))

        return result

    @staticmethod
    def _extract_name_hints(labeled_clean: list) -> tuple[dict, list]:
        """
        Retourne deux structures pour aider le LLM à identifier les prénoms :
        - spk_tops : mots en majuscule en milieu de phrase par locuteur (prénoms potentiels)
        - address_hints : (locuteur_A, prénom, locuteur_B) quand A termine son tour
          en appelant B par son prénom (apostrophe directe)
        """
        import re
        from collections import defaultdict, Counter

        _SKIP = frozenset({
            "Le", "La", "Les", "Un", "Une", "Des", "Du", "De", "Ce", "Ça", "Ca",
            "Je", "Tu", "Il", "Elle", "On", "Nous", "Vous", "Ils", "Elles", "Y",
            "Et", "Ou", "Mais", "Donc", "Car", "Or", "Si", "Ni",
            "Euh", "Ben", "Bon", "Ah", "Oh", "Non", "Oui", "Ouais", "OK",
            "Alors", "Apres", "Après", "Parce", "Quand", "Comme", "Avec",
            "Pour", "Dans", "Sur", "Par", "Entre", "Vers",
            "Tout", "Tous", "Toute", "Toutes", "Cette", "Ces",
            "Mon", "Ton", "Son", "Ma", "Ta", "Sa", "Notre", "Votre", "Leur", "Leurs",
            "Aussi", "Même", "Encore", "Voilà", "Voila", "Ici", "Là", "Bien", "Très",
            "Ça", "Cela", "Celui", "Celle", "Ceux", "Celles", "Moi", "Toi", "Lui", "Eux",
        })

        spk_caps: dict = defaultdict(Counter)
        for label, text in labeled_clean:
            words = text.rstrip("…").split()
            for i, word in enumerate(words):
                if i == 0:
                    continue
                prev = words[i - 1].rstrip()
                if prev and prev[-1] in ".!?":
                    continue
                # Nettoyer ponctuation et caractères non-latins
                bare = re.sub(r"[,\.!?;:«»\"\'()\[\]؀-ۿ一-鿿぀-ヿ]+", "", word).strip()
                if not bare or not bare[0].isupper() or bare in _SKIP or len(bare) < 3:
                    continue
                if bare.isupper():  # sigle tout en majuscules — ignorer
                    continue
                spk_caps[label][bare] += 1

        address_hints = []
        for i in range(len(labeled_clean) - 1):
            curr_label, curr_text = labeled_clean[i]
            next_label, _ = labeled_clean[i + 1]
            if curr_label == next_label:
                continue
            clean = curr_text.rstrip("…").strip()
            m = re.search(r"\b([A-ZÁÀÂÉÈÊËÎÏÔÙÛÜÇ][a-záàâéèêëîïôùûüç]{2,})[,\s]*$", clean)
            if m:
                name = m.group(1)
                if name not in _SKIP and len(name) >= 3:
                    address_hints.append((curr_label, name, next_label))

        spk_tops = {
            spk: [w for w, _ in counter.most_common(8)]
            for spk, counter in spk_caps.items()
            if counter
        }
        return spk_tops, address_hints

    @staticmethod
    def _assign_speaker_genders(
        gender_segments: list,
        turns: list,
        min_overlap_s: float = 1.0,
    ) -> dict:
        """Croise les segments genre horodatés avec les tours pyannote.

        Retourne {speaker_id: {"gender": "male"|"female"|"", "male_s": float, "female_s": float}}.
        Le genre n'est attribué que si le total de chevauchement >= min_overlap_s
        et que l'un des deux sexes domine l'autre.
        """
        if not gender_segments or not turns:
            return {}

        accum: dict = {}
        for turn in turns:
            spk = turn.get("speaker") or turn.get("speaker_id", "")
            t_start = float(turn.get("start", 0.0))
            t_end = float(turn.get("end", 0.0))
            if not spk or t_end <= t_start:
                continue
            if spk not in accum:
                accum[spk] = {"male_s": 0.0, "female_s": 0.0}
            for seg in gender_segments:
                s_start = float(seg.get("start", 0.0))
                s_end = float(seg.get("end", 0.0))
                label = seg.get("label", "")
                overlap = min(t_end, s_end) - max(t_start, s_start)
                if overlap <= 0 or label not in ("male", "female"):
                    continue
                accum[spk][f"{label}_s"] += overlap

        result: dict = {}
        for spk, counts in accum.items():
            male_s = counts["male_s"]
            female_s = counts["female_s"]
            total = male_s + female_s
            if total < min_overlap_s:
                gender = ""
            elif male_s > female_s:
                gender = "male"
            elif female_s > male_s:
                gender = "female"
            else:
                gender = ""
            result[spk] = {"gender": gender, "male_s": round(male_s, 2), "female_s": round(female_s, 2)}
        return result

    def _inject_speaker_genders(
        self, fs, audio_scene: dict
    ) -> dict:
        """Attribue acoustiquement le genre à chaque locuteur et met à jour speaker_stats.json.

        Lit les tours depuis speaker_turns.json (format flat, écrit par SpeakerDetector
        et DiarizerService). Ne remplace jamais un choix utilisateur déjà présent.
        Retourne le dict {speaker_id: {"gender", "male_s", "female_s"}}.
        """
        import time as _time
        sl = get_structured_logger(__name__)

        gender_segments = (audio_scene or {}).get("gender_segments") or []
        if not gender_segments:
            sl.info("[gender] Pas de segments genre horodatés — attribution locuteur ignorée")
            return {}

        # Charger les tours depuis speaker_turns.json (format plat, écrit par diarizer)
        turns_data = fs.load_json("speakers/speaker_turns.json") or {}
        turns = turns_data.get("turns") or []

        if not turns:
            sl.info("[gender] Aucun tour de parole disponible — attribution locuteur ignorée")
            return {}

        t0 = _time.monotonic()
        speaker_genders = self._assign_speaker_genders(gender_segments, turns)
        elapsed = round(_time.monotonic() - t0, 3)

        # Mettre à jour speaker_stats.json uniquement si le champ gender est vide
        speakers_data = fs.load_json("speakers/speaker_stats.json") or {}
        _raw_stats = speakers_data.get("speakers") or []
        # DiarizerService écrit aussi un champ "stats" avec speaking_time/turn_count.
        # On l'utilise pour reconstruire le format complet quand les speakers sont des strings
        # (cas sep=1 : run_diarization tourne sur vocals.wav → cache miss → réécrit le format string).
        _diar_stats = speakers_data.get("stats") or {}
        spk_stats = []
        for s in _raw_stats:
            if isinstance(s, str):
                extra = _diar_stats.get(s, {})
                spk_stats.append({
                    "speaker_id": s,
                    "label": s,
                    "speaking_time_seconds": extra.get("speaking_time_seconds", 0),
                    "turn_count": extra.get("turn_count", 0),
                    "mapped_to": None,
                    "mapped_name": None,
                    "validation": "pending",
                    "gender": "",
                })
            else:
                spk_stats.append(s)
        updated = 0
        for spk in spk_stats:
            spk_id = spk.get("speaker_id", "")
            if spk_id not in speaker_genders:
                continue
            if spk.get("gender"):
                continue  # ne pas écraser un choix utilisateur
            gender = speaker_genders[spk_id]["gender"]
            if gender:
                spk["gender"] = gender
                updated += 1

        if updated:
            fs.save_json("speakers/speaker_stats.json", {"speakers": spk_stats})

        detail = " | ".join(
            f"{sid}={v['gender'] or '?'} ({v['female_s']:.1f}s♀/{v['male_s']:.1f}s♂)"
            for sid, v in speaker_genders.items()
        )
        sl.info(
            "[gender] Genre par locuteur estimé",
            duree=elapsed,
            detail=detail,
            mis_a_jour=updated,
        )
        return speaker_genders

    @staticmethod
    def _build_gender_section(audio_scene: dict) -> list:
        """Construit la section genre vocal pour le contexte de diarisation.

        Retourne une liste de lignes Markdown ou ``[]`` si aucune donnée de genre.
        La détection est globale (non attribuée par locuteur) — la section fournit
        un indice supplémentaire au LLM d'identification.
        """
        gender = (audio_scene or {}).get("gender") or {}
        if not gender.get("has_gender_data"):
            return []

        dominant = gender.get("dominant")
        male_ratio = float(gender.get("male_ratio") or 0.0)
        female_ratio = float(gender.get("female_ratio") or 0.0)

        stats_labels = ((audio_scene or {}).get("stats") or {}).get("labels") or {}
        male_dur = float((stats_labels.get("male") or {}).get("duration_s", 0.0))
        female_dur = float((stats_labels.get("female") or {}).get("duration_s", 0.0))

        if dominant == "male":
            dominant_label, dominant_pct = "Masculin", round(male_ratio * 100, 1)
        elif dominant == "female":
            dominant_label, dominant_pct = "Féminin", round(female_ratio * 100, 1)
        else:
            dominant_label, dominant_pct = "Indéterminé", 50.0

        lines = [
            "",
            "## Genre vocal estimé (analyse acoustique globale)",
            "",
            "*(Estimation par fréquence fondamentale — indicatif,"
            " non attribué par locuteur)*",
            "",
            f"- Genre dominant : **{dominant_label}** ({dominant_pct}% de la parole genrée)",
            f"- Parole masculine estimée : {male_dur:.1f}s"
            f" | féminine : {female_dur:.1f}s",
        ]

        if dominant_pct >= 80 and dominant in ("male", "female"):
            adj = "masculine" if dominant == "male" else "féminine"
            lines.append(
                f"- Indice fort : {dominant_pct}% de la parole genrée est {adj}"
            )

        return lines

    @staticmethod
    def _write_diarization_context(
        fs, speakers_result: dict, audio_scene: dict | None = None,
        speaker_genders: dict | None = None,
    ) -> str | None:
        speakers = speakers_result.get("speakers") or []
        if not speakers:
            return None

        labeled = WorkflowRunner._build_labeled_segments(fs, speakers_result)

        total_time = sum(float(spk.get("speaking_time_seconds", 0) or 0) for spk in speakers)
        lines = [
            "# Données de diarization acoustique",
            "",
            f"**Nombre de locuteurs détectés :** {len(speakers)}",
            "",
            "| Locuteur | Temps de parole | Tours de parole | Part du temps |",
            "|---|---:|---:|---:|",
        ]
        for spk in sorted(speakers, key=lambda s: float(s.get("speaking_time_seconds", 0) or 0), reverse=True):
            speaking_time = float(spk.get("speaking_time_seconds", 0) or 0)
            turns = int(spk.get("turn_count", 0) or 0)
            pct = round(100 * speaking_time / total_time, 1) if total_time > 0 else 0
            speaker_id = spk.get("speaker_id", spk.get("label", "SPEAKER_XX"))
            lines.append(
                f"| {speaker_id} "
                f"| {speaking_time:.1f}s ({speaking_time / 60:.1f}min) "
                f"| {turns} | {pct}% |"
            )

        # Ne garder que les segments clairement attribués (hors mixte et inconnus)
        labeled_clean = [(lbl, txt) for lbl, txt in labeled if lbl not in ("mixte", "?")]
        if labeled_clean:
            lines.extend([
                "",
                "## Transcription labellisée (attribution acoustique)",
                "",
                "*(uniquement les segments où un seul locuteur parle nettement)*",
                "",
            ])
            for label, text in labeled_clean:
                lines.append(f"**[{label}]** {text}")

            # Résumé des phrases certaines par locuteur (hors mixte)
            from collections import defaultdict
            by_spk: dict = defaultdict(list)
            for label, text in labeled:
                if label not in ("mixte", "?"):
                    by_spk[label].append(f'« {text} »')

            if by_spk:
                lines.extend([
                    "",
                    "## Ce que dit chaque locuteur (phrases acoustiquement certaines, hors segments mixtes)",
                    "",
                    "*(Source primaire pour identifier les rôles — ces phrases ont été produites"
                    " physiquement par ce SPEAKER_XX)*",
                    "",
                ])
                for spk_id in sorted(by_spk.keys()):
                    lines.append(f"- **{spk_id}** : {' | '.join(by_spk[spk_id])}")

            # Section indices prénoms
            spk_tops, address_hints = WorkflowRunner._extract_name_hints(labeled_clean)
            if spk_tops or address_hints:
                lines.extend([
                    "",
                    "## Indices pour identifier les prénoms des locuteurs",
                    "",
                    "*(Ces données sont des indices bruts — le LLM doit raisonner sur leur pertinence)*",
                    "",
                ])
                if address_hints:
                    lines.append("### Apostrophes directes détectées (fin de tour → changement de locuteur)")
                    lines.append("")
                    lines.append("*(Si SPEAKER_A termine son tour en prononçant un prénom et que SPEAKER_B prend la parole,"
                                 " SPEAKER_B est probablement ce prénom)*")
                    lines.append("")
                    seen_hints: set = set()
                    for curr_spk, name, next_spk in address_hints:
                        key = (curr_spk, name, next_spk)
                        if key not in seen_hints:
                            lines.append(f"- {curr_spk} dit « …{name} » → {next_spk} prend la parole")
                            seen_hints.add(key)
                if spk_tops:
                    lines.extend(["", "### Noms propres en milieu de phrase par locuteur"])
                    lines.append("")
                    lines.append("*(mots en majuscule hors début de phrase et hors sigles —"
                                 " peuvent être des personnes mentionnées ou le prénom du locuteur lui-même)*")
                    lines.append("")
                    for spk_id in sorted(spk_tops.keys()):
                        names = spk_tops[spk_id]
                        if names:
                            lines.append(f"- **{spk_id}** : {', '.join(names)}")

        # Section genre vocal global (si analyse de scène disponible)
        gender_lines = WorkflowRunner._build_gender_section(audio_scene or {})
        if gender_lines:
            lines.extend(gender_lines)

        # Section genre par locuteur (si attribution acoustique disponible)
        if speaker_genders:
            _GENDER_FR = {"male": "Masculin", "female": "Féminin"}
            _GENDER_SYM = {"male": "♂", "female": "♀"}
            per_spk_lines = [
                "",
                "## Genre vocal par locuteur (estimation acoustique)",
                "",
                "*(Croisement tours pyannote × segments YIN — indicatif)*",
                "",
            ]
            for sid in sorted(speaker_genders.keys()):
                v = speaker_genders[sid]
                gender = v.get("gender", "")
                label = _GENDER_FR.get(gender, "Indéterminé")
                sym = _GENDER_SYM.get(gender, "?")
                female_s = v.get("female_s", 0.0)
                male_s = v.get("male_s", 0.0)
                per_spk_lines.append(
                    f"- **{sid}** : {label} {sym}"
                    f" ({female_s:.1f}s♀ / {male_s:.1f}s♂)"
                )
            lines.extend(per_spk_lines)

        lines.extend(
            [
                "",
                "**Consigne :** utilise la section 'Ce que dit chaque locuteur' comme données primaires"
                " pour attribuer les SPEAKER_XX à leurs rôles. Déduis le rôle de chaque locuteur depuis"
                " ce qu'il dit dans ses segments certains (qui pose des questions, qui offre, qui commande,"
                " qui réagit, qui encaisse). Ne renverse pas ce mapping : si SPEAKER_XX dit un impératif"
                " ('Goûtez', 'Tenez', 'Regardez') ou annonce un prix, il est l'animateur/hôte/vendeur."
                " Le nombre de locuteurs détectés acoustiquement prime sur les noms mentionnés dans la transcription."
                " Pour les prénoms : utilise en priorité les apostrophes directes ci-dessus"
                " (un locuteur qui appelle la personne suivante par son prénom en fin de tour)."
                " Si un prénom apparaît dans la liste 'Noms propres' d'un locuteur dans un contexte"
                " d'auto-désignation (ex : 'moi, Prénom' ou 'je suis Prénom'), c'est un indice fort.",
                "",
            ]
        )
        content = "\n".join(lines)
        fs.save_text("summary/diarization_context.md", content)
        return content

    def run_speaker_detection(self, job: Job, audio_path: str, config: dict) -> dict:
        from pathlib import Path

        self.store.update_state(job.id, JobState.SPEAKER_DETECTION_RUNNING)
        try:
            from transcria.stt.speaker_detection import SpeakerDetector

            detector = SpeakerDetector(config)
            if self._cuda_available():
                with GPUSession(self.vram, "pyannote", self.vram.pyannote_vram_mb) as gpu:
                    device = f"cuda:{gpu.gpu_index}"
                    logger.info(
                        "[speaker_detection] GPU sélectionné: %s (%d Mo réservés)",
                        device, self.vram.pyannote_vram_mb,
                    )
                    result = detector.detect(job, Path(audio_path), device=device)
            else:
                logger.info("[speaker_detection] CUDA indisponible — pyannote sur CPU")
                device = "cpu"
                result = detector.detect(job, Path(audio_path), device=device)
            self.store.update_state(job.id, JobState.SPEAKER_DETECTION_DONE)
            return result
        except GPUSessionError as exc:
            logger.error("[speaker_detection] VRAM insuffisante: %s", exc)
            self.store.update_state(job.id, JobState.FAILED, str(exc))
            return {"error": str(exc), "speakers": []}
        except Exception as exc:
            logger.exception("Échec détection locuteurs")
            self.store.update_state(job.id, JobState.FAILED, str(exc))
            return {"error": str(exc), "speakers": []}

    def run_transcription(self, job: Job, audio_path: str, config: dict) -> dict:
        from pathlib import Path

        self.store.update_state(job.id, JobState.TRANSCRIBING)

        from transcria.stt.transcriber_factory import get_backend_vram_mb

        backend = config.get("models", {}).get("stt_backend", "cohere")
        required_vram_mb = get_backend_vram_mb(backend, config)
        gpu = self.vram.ensure_free(required_vram_mb)
        if gpu is None:
            self.store.update_state(job.id, JobState.FAILED, "VRAM insuffisante")
            return {"error": "VRAM insuffisante pour la transcription"}

        try:
            from transcria.stt.transcription import Transcriber

            transcriber = Transcriber(config, gpu_index=gpu)
            result = transcriber.transcribe(job, Path(audio_path))
            self.vram.track_model(f"{backend}-transcription", gpu, required_vram_mb)
            return result
        except Exception as exc:
            logger.exception("Échec transcription")
            self.vram.offload_all()
            self.store.update_state(job.id, JobState.FAILED, str(exc))
            return {"error": str(exc)}

    def run_diarization(self, job: Job, audio_path: str, config: dict) -> dict:
        from pathlib import Path

        self.store.update_state(job.id, JobState.DIARIZING)
        try:
            from transcria.stt.diarization import DiarizerService

            if self._cuda_available():
                with GPUSession(self.vram, "pyannote", self.vram.pyannote_vram_mb) as gpu:
                    device = f"cuda:{gpu.gpu_index}"
                    logger.info(
                        "[diarization] GPU sélectionné: %s (%d Mo réservés)",
                        device, self.vram.pyannote_vram_mb,
                    )
                    diarizer = DiarizerService(config, device=device)
                    result = diarizer.diarize(job, Path(audio_path))
                    diarizer.offload()
            else:
                logger.info("[diarization] CUDA indisponible — pyannote sur CPU")
                diarizer = DiarizerService(config, device="cpu")
                result = diarizer.diarize(job, Path(audio_path))
                diarizer.offload()

            # Attribution genre par locuteur — audio_scene.json disponible à ce stade
            # (PipelineService le produit avant d'appeler run_diarization)
            fs = self._get_fs(config, job.id)
            audio_scene = fs.load_json("metadata/audio_scene.json") or {}
            self._inject_speaker_genders(fs, audio_scene)

            return result
        except GPUSessionError as exc:
            logger.error("[diarization] VRAM insuffisante: %s", exc)
            self.store.update_state(job.id, JobState.FAILED, str(exc))
            return {"error": str(exc)}
        except Exception as exc:
            logger.exception("Échec diarisation")
            return {"error": str(exc)}

    def run_quality_checks(self, job: Job, config: dict) -> dict:
        self.store.update_state(job.id, JobState.QUALITY_CHECKING)
        try:
            from transcria.quality.quality_report import QualityReporter

            reporter = QualityReporter(config)
            result = reporter.run_all_checks(job)
            self.store.update_state(job.id, JobState.QUALITY_CHECKED)
            return result
        except Exception as exc:
            logger.exception("Échec contrôle qualité")
            self.store.update_state(job.id, JobState.FAILED, str(exc))
            return {"error": str(exc)}

    def run_correction(self, job: Job, config: dict) -> dict:
        """Phase 3: correction du SRT via opencode + LLM d'arbitrage."""
        from transcria.context.central_lexicon_service import filter_lexicon_by_srt_presence
        from transcria.gpu.opencode_runner import OpenCodeRunner
        from transcria.jobs.filesystem import JobFilesystem

        llm_cfg = config.get("workflow", {}).get("arbitration_llm", {})
        if llm_cfg.get("enabled") is False:
            logger.info("Correction SRT ignorée (workflow.arbitration_llm.enabled=false)")
            return {"success": True, "skipped": True, "reason": "arbitration_llm.enabled=false"}

        fs = JobFilesystem(config.get("storage", {}).get("jobs_dir", "./jobs"), job.id)
        srt_path = fs.job_dir / "metadata" / "transcription.srt"
        context_path = fs.job_dir / "context" / "job_context.yaml"
        lexicon_path = fs.job_dir / "context" / "session_lexicon.json"
        filtered_lexicon_path = fs.job_dir / "context" / "session_lexicon_filtered.json"

        if not srt_path.is_file():
            return {"success": False, "error": "SRT source introuvable"}

        lexicon_path_for_correction = lexicon_path
        if lexicon_path.is_file():
            lexicon = fs.load_json("context/session_lexicon.json") or []
            srt_text = fs.load_text("metadata/transcription.srt") or ""
            if isinstance(lexicon, list):
                filtered_lexicon, filter_stats = filter_lexicon_by_srt_presence(lexicon, srt_text)
                fs.save_json("context/session_lexicon_filtered.json", filtered_lexicon)
                lexicon_path_for_correction = filtered_lexicon_path
                logger.info(
                    "Préfiltrage lexique avant correction: job=%s total=%d conservés=%d retirés=%d terme=%d variante=%d priorité=%d",
                    job.id,
                    filter_stats.get("total", 0),
                    filter_stats.get("kept", 0),
                    filter_stats.get("filtered_out", 0),
                    filter_stats.get("kept_by_term_presence", 0),
                    filter_stats.get("kept_by_variant_presence", 0),
                    filter_stats.get("kept_by_priority", 0),
                )
                if filter_stats.get("kept", 0) > 80:
                    logger.warning(
                        "Lexique volumineux transmis à la correction: job=%s entrées=%d",
                        job.id,
                        filter_stats.get("kept", 0),
                    )
            else:
                logger.warning("Lexique de session ignoré avant correction: format inattendu job=%s", job.id)

        api_model_id = config.get("services", {}).get("arbitrage_api_model_id")
        arbitrage_port = config.get("services", {}).get("arbitrage_llm_port", 8080)
        logger.info(
            "Phase 3: correction SRT — vérification LLM d'arbitrage (modèle attendu: %s, port %d)",
            api_model_id or "non contraint",
            arbitrage_port,
        )
        launched = self.vram.ensure_arbitrage_llm_ready(expected_model_id=api_model_id)
        if not launched:
            return {"success": False, "error": "LLM d'arbitrage non disponible"}

        try:
            opencode_bin = config.get("workflow", {}).get("arbitration_llm", {}).get("opencode_bin")
            runner = OpenCodeRunner(
                str(fs.job_dir / "metadata"),
                opencode_bin=opencode_bin,
                config=config,
            )
            result = runner.run_correction(str(srt_path), str(context_path), str(lexicon_path_for_correction))
            if result["success"] and result["corrected_srt"]:
                fs.save_text("metadata/transcription_corrigee.srt", result["corrected_srt"])
                if result["report"]:
                    fs.save_text("metadata/correction_report.md", result["report"])
                logger.info("Correction SRT terminée (%d caractères)", len(result["corrected_srt"]))
                if result.get("warning"):
                    logger.warning("Correction SRT terminée avec avertissement: %s", result["warning"])
            return result
        except Exception as exc:
            logger.exception("Échec correction SRT")
            return {"success": False, "error": str(exc)}

    def build_export(self, job: Job, config: dict) -> dict:
        try:
            from transcria.exports.package_builder import PackageBuilder

            builder = PackageBuilder(config)
            result = builder.build_package(job)
            if isinstance(result, dict) and result.get("error"):
                self.store.update_state(job.id, JobState.FAILED, result["error"])
                self.vram.offload_all()
                return result
            self.store.update_state(job.id, JobState.EXPORT_READY)
            self.vram.offload_all()
            return result
        except Exception as exc:
            logger.exception("Échec construction package")
            self.store.update_state(job.id, JobState.FAILED, str(exc))
            return {"error": str(exc)}
