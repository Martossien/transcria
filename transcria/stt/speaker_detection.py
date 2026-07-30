import logging
import re
from pathlib import Path

from transcria.ingestion.manifest import parse_participants_manifest
from transcria.ingestion.manifest_turns import turns_from_manifest
from transcria.jobs.filesystem import JobFilesystem
from transcria.jobs.models import Job
from transcria.stt.diarizer_factory import create_diarizer

logger = logging.getLogger(__name__)


class SpeakerDetector:
    def __init__(self, config: dict):
        self.config = config

    def detect(self, job: Job, audio_path: Path, device: str = "cuda:0", progress_callback=None) -> dict:
        fs = JobFilesystem(self.config.get("storage", {}).get("jobs_dir", "./jobs"), job.id)

        diar_result = fs.load_json("speakers/speaker_turns.json")
        if diar_result is None:
            # RÉUNION avec manifeste (décision utilisateur 2026-07-29) : les tours viennent
            # des PISTES — segmentation exacte, parole simultanée comprise, jamais de
            # sur-découpage d'une voix unique. pyannote ne sert que sans manifeste…
            # sauf pour SOUS-diariser une piste salle (vague 5, lot B2) : une piste non
            # solo peut cacher plusieurs personnes derrière un micro — pyannote SUR LA
            # PISTE les scinde en PISTE_<pid>_S1… ; une seule voix trouvée → rien ne
            # change (le nom de la piste reste proposé, cas fluide).
            diar_result = self._turns_from_meeting_manifest(
                fs, device=device, progress_callback=progress_callback
            )
        if diar_result is None:
            ds = create_diarizer(self.config, device=device, progress_callback=progress_callback)
            diar_result = ds.diarize(job, audio_path)
        elif diar_result.get("available") and fs.load_json("speakers/speaker_clips.json") is None:
            ds = create_diarizer(self.config, device=device)
            ds._extract_clips(
                audio_path,
                diar_result.get("turns", []),
                diar_result.get("speakers", []),
                fs,
            )

        if not diar_result.get("available"):
            return {
                "available": False,
                "message": diar_result.get("message", "Détection locuteurs indisponible."),
                "speakers": [],
            }

        speakers = []
        manifest_names = diar_result.get("names", {}) if diar_result.get("source") == "manifest" else {}
        for spk in diar_result.get("speakers", []):
            stats = diar_result.get("stats", {}).get(spk, {})
            entry = {
                "speaker_id": spk,
                "label": spk,
                "speaking_time_seconds": stats.get("speaking_time_seconds", 0),
                "turn_count": stats.get("turn_count", 0),
                "mapped_to": None,
                "mapped_name": None,
                "validation": "pending",
            }
            if manifest_names.get(spk):
                # Nom connu de la plateforme → PRÉ-REMPLI à l'étape 5 (suggestion, jamais
                # une validation : l'humain reste le juge).
                entry["suggested_name"] = manifest_names[spk]
                entry["suggested_source"] = "meeting"
            speakers.append(entry)

        fs.save_json("speakers/speaker_stats.json", {"speakers": speakers})
        return {"available": True, "speakers": speakers, "turns": diar_result.get("turns", [])}


    def _turns_from_meeting_manifest(self, fs, device: str | None = None,
                                     progress_callback=None) -> dict | None:
        """Tours de parole depuis `metadata/participants_manifest.json` s'il existe et
        valide — sinon None (le diarizeur classique prend la main). Les pistes salle
        (manifeste v2) passent d'abord par la sous-diarisation par piste."""
        raw = fs.load_json("metadata/participants_manifest.json")
        if not raw:
            return None
        manifest, _reason = parse_participants_manifest(raw)
        if manifest is None:
            return None
        sub_by_pid = self._subdiarize_tracks(fs, manifest, device, progress_callback)
        result = turns_from_manifest(manifest, sub_by_pid=sub_by_pid)
        if not result.get("available"):
            return None
        fs.save_json("speakers/speaker_turns.json", result)
        return result

    # Sous une poignée de secondes de parole, scinder une « salle » en plusieurs voix
    # n'est ni fiable ni utile — et chaque passe pyannote par piste a un coût réel.
    _SUBDIAR_MIN_SPEECH_S = 10.0

    def _subdiarize_tracks(self, fs, manifest, device: str | None,
                           progress_callback) -> dict[str, dict]:
        """Sous-diarisation PAR PISTE des participants non `solo` (vague 5, lot B2).

        Une piste étant alignée sur la timeline commune (D5.1), les timestamps pyannote
        SUR la piste SONT ceux de la réunion — aucune conversion. Rend, pour les seules
        pistes où PLUSIEURS voix sont détectées, {pid: {speakers, turns, exclusive_turns,
        clusters}} avec les voix renommées `PISTE_<pid>_S1`… ; tout est journalisé dans
        `speakers/track_diarization.json` (relisible : pistes examinées, ignorées et
        pourquoi). Best-effort intégral : indisponible ou en échec → {} (une salle reste
        UN locuteur nommable, le comportement historique)."""
        tracks_dir = Path(fs.job_dir) / "input" / "tracks"
        candidates: list[tuple] = []
        skipped: dict[str, str] = {}
        for p in manifest.participants:
            if not p.track:
                continue
            if p.kind == "solo":                  # D5.3 : piste nommée solo, jamais diarisée
                skipped[p.id] = "solo"
                continue
            path = tracks_dir / (p.track.removeprefix("track_") + ".wav")
            if not path.is_file():
                skipped[p.id] = "piste absente"
                continue
            speech_s = sum(end - start for start, end in p.speech_windows)
            if speech_s < self._SUBDIAR_MIN_SPEECH_S:
                skipped[p.id] = f"parole insuffisante ({speech_s:.1f}s)"
                continue
            candidates.append((p, path))
        if not candidates:
            if skipped:
                fs.save_json("speakers/track_diarization.json",
                             {"version": 1, "tracks": {}, "skipped": skipped})
            return {}

        diarizer = create_diarizer(self.config, device=device,
                                   progress_callback=progress_callback)
        diarize_audio = getattr(diarizer, "diarize_audio", None)
        if not callable(diarize_audio):
            # sortformer/remote n'offrent pas l'API par fichier : comportement historique.
            logger.info("Sous-diarisation par piste indisponible (backend sans "
                        "diarize_audio) — pistes salle = un locuteur")
            return {}
        sub_by_pid: dict[str, dict] = {}
        try:
            for p, path in candidates:
                res = diarize_audio(path)
                if not res.get("available"):
                    skipped[p.id] = res.get("error") or res.get("message") or "échec"
                    continue
                clusters = sorted({str(t.get("speaker")) for t in res.get("turns", [])})
                if len(clusters) < 2:
                    # Une seule voix : la piste reste son propre locuteur (nom proposé
                    # directement quand la plateforme le connaît — cas fluide D5.3).
                    skipped[p.id] = f"{len(clusters)} voix"
                    continue
                rename = {spk: f"PISTE_{p.id}_S{i + 1}" for i, spk in enumerate(clusters)}
                sub_by_pid[p.id] = {
                    "clusters": len(clusters),
                    "speakers": sorted(rename.values()),
                    "turns": self._rename_turns(res.get("turns", []), rename),
                    "exclusive_turns": self._rename_turns(res.get("exclusive_turns", []), rename),
                }
                logger.info("Sous-diarisation piste %s (%s) : %d voix distinctes",
                            p.id, p.name or "sans nom", len(clusters))
        finally:
            diarizer.offload()
        fs.save_json("speakers/track_diarization.json",
                     {"version": 1, "tracks": sub_by_pid, "skipped": skipped})
        return sub_by_pid

    @staticmethod
    def _rename_turns(turns: list[dict], rename: dict[str, str]) -> list[dict]:
        return [{**t, "speaker": rename.get(str(t.get("speaker")), str(t.get("speaker")))}
                for t in turns]

    @staticmethod
    def _clean_name(raw_name: str, speaker_id: str) -> str:
        cleaned = re.sub(r"\s*\(SPEAKER_\d+[^)]*\)\s*", "", raw_name).strip()
        cleaned = re.sub(r"\s*\(\s*\d+\s*tours?\s*[^)]*\)\s*", "", cleaned).strip()
        cleaned = re.sub(r"\s*\(\s*~\d+\s*min[^)]*\)\s*", "", cleaned).strip()
        return cleaned if cleaned else speaker_id

    @staticmethod
    def save_mapping(job_id: str, jobs_dir: str, mapping: dict) -> bool:
        fs = JobFilesystem(jobs_dir, job_id)
        speakers_data = fs.load_json("speakers/speaker_stats.json") or {}
        speakers = speakers_data.get("speakers", [])

        for spk in speakers:
            spk_id = spk.get("speaker_id")
            if spk_id in mapping:
                spk["mapped_to"] = mapping[spk_id].get("participant_id")
                raw_name = mapping[spk_id].get("name", spk_id)
                spk["mapped_name"] = SpeakerDetector._clean_name(raw_name, spk_id)
                spk["gender"] = mapping[spk_id].get("gender", "")
                spk["validation"] = "user_validated"

        fs.save_json("speakers/speaker_stats.json", {"speakers": speakers})
        fs.save_json("speakers/speaker_mapping.json", {"mapping": mapping, "speakers": speakers})
        participant_list = mapping.get("__participants__", [])
        if participant_list:
            fs.save_json("context/participants.json", participant_list)

        return True
