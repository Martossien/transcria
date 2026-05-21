"""
Worker subprocess : analyse acoustique de scène audio.

Exécuté via :
    python -m transcria.audio._scene_analysis_worker <audio_path> [<config_json>]

Principe en cascade (inspiré des approches classiques de segmentation audio) :

    1. Activité énergétique par seuillage RMS
    2. Classification spectrale (flatness, ZCR) → speech / music / noise
    3. Estimation du genre via fréquence fondamentale (F0/YIN) — optionnel
    4. Statistiques et signaux de décision

Les fonctions pures (_compute_stats, _compute_gender_stats, _compute_signals,
_frames_to_segments) n'ont aucune dépendance lourde et sont testables unitairement.
Les fonctions d'analyse audio (_classify_scene_frames, _estimate_gender_for_speech,
_analyze_audio) importent librosa localement pour rester isolées.
"""

import copy
import json
import logging
import sys
from collections import defaultdict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

_SPEECH_LABELS = frozenset({"speech", "male", "female"})
_MUSIC_LABELS = frozenset({"music"})
_NOISE_LABELS = frozenset({"noise"})
_NO_ENERGY_LABELS = frozenset({"noEnergy"})
_PROBLEM_LABELS = _MUSIC_LABELS | _NOISE_LABELS | _NO_ENERGY_LABELS

_DEFAULT_CONFIG: dict = {
    "detect_gender": True,
    "thresholds": {
        # Seuil énergétique : ratio de l'énergie RMS moyenne en dessous duquel
        # une trame est considérée inactive
        "energy_ratio": 0.03,
        # Durée minimale d'un segment (segments plus courts ignorés)
        "min_segment_s": 0.3,
        # Spectral flatness > seuil → bruit (spectre plat)
        "noise_flatness_min": 0.40,
        # Spectral flatness < seuil ET ZCR < seuil → musique (harmoniques stables)
        "music_flatness_max": 0.12,
        "music_zcr_max": 0.10,
        # Pitch médian ≥ seuil → voix féminine
        "female_pitch_hz": 165.0,
        # Durée minimale pour exposer une zone non vocale comme point de vigilance
        "problem_segment_min_s": 2.0,
    },
}

# Paramètres de fenêtrage STFT (identiques pour tous les extracteurs afin
# de garantir un alignement temporel cohérent entre les features)
_HOP_LENGTH = 512
_FRAME_LENGTH = 2048


# ---------------------------------------------------------------------------
# Fonctions pures — testables sans librosa
# ---------------------------------------------------------------------------


def _compute_stats(segments: list) -> dict:
    """Calcule la durée et le ratio par label.

    Paramètres
    ----------
    segments : liste de (label, start_sec, stop_sec)

    Retourne
    --------
    {
        "labels": {label: {"duration_s": float, "ratio": float}},
        "total_duration_s": float,
    }
    """
    durations: dict = defaultdict(float)
    for label, start, stop in segments:
        durations[label] += stop - start

    total = sum(durations.values())
    if total == 0.0:
        return {"labels": {}, "total_duration_s": 0.0}

    return {
        "labels": {
            label: {
                "duration_s": round(dur, 3),
                "ratio": round(dur / total, 4),
            }
            for label, dur in durations.items()
        },
        "total_duration_s": round(total, 3),
    }


def _compute_gender_stats(segments: list) -> dict:
    """Calcule la distribution homme/femme à partir des labels 'male'/'female'.

    Retourne
    --------
    {
        "has_gender_data": bool,
        "male_ratio": float,          # ratio sur la parole genrée uniquement
        "female_ratio": float,
        "dominant": "male" | "female" | None,
    }
    """
    male_dur = sum(stop - start for label, start, stop in segments if label == "male")
    female_dur = sum(stop - start for label, start, stop in segments if label == "female")
    total_gendered = male_dur + female_dur

    if total_gendered == 0.0:
        return {
            "has_gender_data": False,
            "male_ratio": 0.0,
            "female_ratio": 0.0,
            "dominant": None,
        }

    male_ratio = male_dur / total_gendered
    female_ratio = female_dur / total_gendered

    if male_ratio > female_ratio:
        dominant = "male"
    elif female_ratio > male_ratio:
        dominant = "female"
    else:
        dominant = None  # égalité parfaite

    return {
        "has_gender_data": True,
        "male_ratio": round(male_ratio, 4),
        "female_ratio": round(female_ratio, 4),
        "dominant": dominant,
    }


def _compute_signals(stats: dict, gender_stats: dict) -> dict:
    """Dérive les signaux de décision à partir des statistiques de scène.

    Retourne
    --------
    {
        "has_music": bool,
        "has_noise": bool,
        "speech_ratio": float,   # fraction du total occupée par la parole
                                 # (speech + male + female)
        "music_ratio": float,
        "noise_ratio": float,
        "no_energy_ratio": float,
        "non_speech_ratio": float,
        "gender": {has_gender_data, dominant, male_ratio, female_ratio},
    }
    """
    labels = stats.get("labels", {})
    total = stats.get("total_duration_s", 0.0)

    has_music = any(lbl in _MUSIC_LABELS for lbl in labels)
    has_noise = any(lbl in _NOISE_LABELS for lbl in labels)

    speech_dur = sum(
        info["duration_s"]
        for lbl, info in labels.items()
        if lbl in _SPEECH_LABELS
    )
    music_dur = sum(
        info["duration_s"]
        for lbl, info in labels.items()
        if lbl in _MUSIC_LABELS
    )
    noise_dur = sum(
        info["duration_s"]
        for lbl, info in labels.items()
        if lbl in _NOISE_LABELS
    )
    no_energy_dur = sum(
        info["duration_s"]
        for lbl, info in labels.items()
        if lbl in _NO_ENERGY_LABELS
    )
    speech_ratio = round(speech_dur / total, 4) if total > 0.0 else 0.0
    music_ratio = round(music_dur / total, 4) if total > 0.0 else 0.0
    noise_ratio = round(noise_dur / total, 4) if total > 0.0 else 0.0
    no_energy_ratio = round(no_energy_dur / total, 4) if total > 0.0 else 0.0
    non_speech_ratio = round((music_dur + noise_dur + no_energy_dur) / total, 4) if total > 0.0 else 0.0

    return {
        "has_music": has_music,
        "has_noise": has_noise,
        "speech_ratio": speech_ratio,
        "music_ratio": music_ratio,
        "noise_ratio": noise_ratio,
        "no_energy_ratio": no_energy_ratio,
        "non_speech_ratio": non_speech_ratio,
        "gender": gender_stats,
    }


def _segments_to_dicts(segments: list) -> list:
    """Convertit les tuples internes en dicts JSON stables."""
    return [_segment_to_dict(label, start, stop) for label, start, stop in segments]


def _segment_to_dict(label: str, start: float, stop: float) -> dict:
    """Sérialise un segment audio en conservant une précision milliseconde."""
    return {
        "label": label,
        "start": round(start, 3),
        "end": round(stop, 3),
        "duration_s": round(stop - start, 3),
    }


def _problem_segments(segments: list, min_duration_s: float) -> list:
    """Retourne les longues zones non vocales utiles pour la relecture qualité."""
    return [
        _segment_to_dict(label, start, stop)
        for label, start, stop in segments
        if label in _PROBLEM_LABELS and stop - start >= min_duration_s
    ]


def _frames_to_segments(labels: list, frame_duration: float, min_duration_s: float) -> list:
    """Convertit une séquence de labels frame par frame en liste de segments.

    Les segments dont la durée est inférieure à ``min_duration_s`` sont ignorés.
    """
    if not labels:
        return []

    segments = []
    current_label = labels[0]
    current_start_frame = 0

    for i, label in enumerate(labels[1:], 1):
        if label != current_label:
            start = current_start_frame * frame_duration
            end = i * frame_duration
            if end - start >= min_duration_s:
                segments.append((current_label, round(start, 3), round(end, 3)))
            current_label = label
            current_start_frame = i

    # Dernier segment
    start = current_start_frame * frame_duration
    end = len(labels) * frame_duration
    if end - start >= min_duration_s:
        segments.append((current_label, round(start, 3), round(end, 3)))

    return segments


# ---------------------------------------------------------------------------
# Analyse audio — importent librosa localement
# ---------------------------------------------------------------------------


def _classify_scene_frames(signal, sr: int, thresholds: dict) -> tuple:
    """Classifie chaque trame audio en noEnergy / speech / music / noise.

    Paramètres
    ----------
    signal    : tableau numpy, audio mono 1-D
    sr        : fréquence d'échantillonnage (Hz)
    thresholds: dict de seuils configurables

    Retourne
    --------
    (frame_labels: list[str], frame_duration_s: float)
    """
    import librosa
    import numpy as np

    energy_ratio = float(thresholds.get("energy_ratio", 0.03))
    noise_flatness_min = float(thresholds.get("noise_flatness_min", 0.40))
    music_flatness_max = float(thresholds.get("music_flatness_max", 0.12))
    music_zcr_max = float(thresholds.get("music_zcr_max", 0.10))

    rms = librosa.feature.rms(
        y=signal, frame_length=_FRAME_LENGTH, hop_length=_HOP_LENGTH
    )[0]
    flatness = librosa.feature.spectral_flatness(
        y=signal, n_fft=_FRAME_LENGTH, hop_length=_HOP_LENGTH
    )[0]
    zcr = librosa.feature.zero_crossing_rate(
        y=signal, frame_length=_FRAME_LENGTH, hop_length=_HOP_LENGTH
    )[0]

    positive_rms = rms[rms > 0]
    mean_rms = float(np.mean(positive_rms)) if positive_rms.size > 0 else 0.0
    energy_threshold = mean_rms * energy_ratio

    frame_labels: list = []
    for r, f, z in zip(rms, flatness, zcr):
        if r <= energy_threshold:
            frame_labels.append("noEnergy")
        elif f >= noise_flatness_min:
            frame_labels.append("noise")
        elif f <= music_flatness_max and z <= music_zcr_max:
            frame_labels.append("music")
        else:
            frame_labels.append("speech")

    frame_duration = _HOP_LENGTH / sr
    return frame_labels, frame_duration


def _estimate_gender_for_speech(
    signal, sr: int, segments: list, female_pitch_hz: float
) -> list:
    """Sub-classifie les segments 'speech' en 'male' ou 'female' via analyse de pitch.

    Utilise l'algorithme YIN pour estimer la fréquence fondamentale (F0) de chaque
    segment de parole, puis compare la médiane au seuil configurable.

    Les segments trop courts (< 100 ms) ou dont le pitch est hors plage vocale
    restent étiquetés 'speech' plutôt que d'être mal classés.
    """
    import librosa
    import numpy as np

    min_voiced_hz = 50.0
    max_voiced_hz = 600.0
    min_segment_samples = int(sr * 0.10)  # 100 ms minimum pour une estimation fiable

    # Plage de recherche YIN : C2 (~65 Hz) à C6 (~1047 Hz)
    fmin = librosa.note_to_hz("C2")
    fmax = librosa.note_to_hz("C6")

    result = []
    for label, start, stop in segments:
        if label != "speech":
            result.append((label, start, stop))
            continue

        start_sample = int(start * sr)
        stop_sample = int(stop * sr)
        seg = signal[start_sample:stop_sample]

        if len(seg) < min_segment_samples:
            logger.debug(
                "Segment [%.2f-%.2f] trop court pour estimation de pitch", start, stop
            )
            result.append(("speech", start, stop))
            continue

        try:
            f0 = librosa.yin(seg, fmin=fmin, fmax=fmax, sr=sr)
            voiced = f0[(f0 > min_voiced_hz) & (f0 < max_voiced_hz)]

            if voiced.size == 0:
                result.append(("speech", start, stop))
                continue

            median_f0 = float(np.median(voiced))
            gender = "female" if median_f0 >= female_pitch_hz else "male"
            logger.debug(
                "Segment [%.2f-%.2f] : F0 médiane = %.1f Hz → %s",
                start, stop, median_f0, gender,
            )
            result.append((gender, start, stop))

        except Exception as exc:
            logger.debug(
                "Estimation de pitch échouée sur [%.2f-%.2f] : %s", start, stop, exc
            )
            result.append(("speech", start, stop))

    return result


def _analyze_audio(audio_path: str, config: dict) -> list:
    """Pipeline complet : charge l'audio, classifie les trames, retourne les segments.

    Paramètres
    ----------
    audio_path : chemin vers le fichier audio (formats supportés par librosa/soundfile)
    config     : dict fusionné avec ``_DEFAULT_CONFIG``

    Retourne
    --------
    Liste de (label, start_sec, stop_sec), incluant les segments 'noEnergy'
    afin de produire des ratios et zones horodatées complets.
    """
    import librosa

    thresholds = config.get("thresholds", {})
    detect_gender = bool(config.get("detect_gender", True))
    min_segment_s = float(thresholds.get("min_segment_s", 0.3))
    female_pitch_hz = float(thresholds.get("female_pitch_hz", 165.0))

    sr_target = 16_000  # cohérent avec le reste du pipeline
    signal, sr = librosa.load(audio_path, sr=sr_target, mono=True)
    logger.info(
        "[scene_worker] Chargement OK : %.1fs @ %d Hz", len(signal) / sr, sr
    )

    frame_labels, frame_duration = _classify_scene_frames(signal, sr, thresholds)
    segments = _frames_to_segments(frame_labels, frame_duration, min_segment_s)
    active_segments = [(lab, s, e) for lab, s, e in segments if lab != "noEnergy"]

    logger.info(
        "[scene_worker] %d segments dont %d actifs avant classification genre",
        len(segments),
        len(active_segments),
    )

    if detect_gender:
        segments = _estimate_gender_for_speech(signal, sr, segments, female_pitch_hz)

    return segments


# ---------------------------------------------------------------------------
# Point d'entrée subprocess
# ---------------------------------------------------------------------------


def _merge_config(base: dict, override: dict) -> dict:
    """Fusionne ``override`` dans une copie profonde de ``base``."""
    result = copy.deepcopy(base)
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(result.get(key), dict):
            result[key].update(val)
        else:
            result[key] = val
    return result


def main(argv: list) -> int:
    """
    Usage : python -m transcria.audio._scene_analysis_worker <audio_path> [<config_json>]

    Écrit un objet JSON sur stdout. Retourne 0 si succès, 1 si erreur.
    """
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    if len(argv) < 2:
        print(
            "Usage: _scene_analysis_worker <audio_path> [<config_json>]",
            file=sys.stderr,
        )
        return 1

    audio_path = argv[1]
    config = copy.deepcopy(_DEFAULT_CONFIG)

    if len(argv) > 2:
        try:
            config = _merge_config(config, json.loads(argv[2]))
        except json.JSONDecodeError as exc:
            print(f"Config JSON invalide : {exc}", file=sys.stderr)
            return 1

    try:
        segments = _analyze_audio(audio_path, config)
    except FileNotFoundError:
        print(f"Fichier audio introuvable : {audio_path}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Erreur analyse audio : {exc}", file=sys.stderr)
        return 1

    stats = _compute_stats(segments)
    gender_stats = _compute_gender_stats(segments)
    signals = _compute_signals(stats, gender_stats)
    problem_min_s = float(
        config.get("thresholds", {}).get("problem_segment_min_s", 2.0)
    )

    gender_segments = [
        {"start": round(start, 3), "end": round(stop, 3), "label": label}
        for label, start, stop in segments
        if label in ("male", "female")
    ]
    print(json.dumps({
        **signals,
        "stats": stats,
        "scene_segments": _segments_to_dicts(segments),
        "problem_segments": _problem_segments(segments, problem_min_s),
        "gender_segments": gender_segments,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
