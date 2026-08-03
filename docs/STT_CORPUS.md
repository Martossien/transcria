# Corpus STT — calibration et relecture

> Statut : manifeste initial. Les catégories ci-dessous servent à organiser les
> benches ; elles ne remplacent pas la lecture humaine des SRT.

## Règle de décision

Ne jamais décider un backend ou un réglage VAD sur un seul fichier. En
particulier, `test5.wav` est un cas extrême quasiment impossible : il est utile
pour détecter les effondrements catastrophiques, pas pour choisir la configuration
production.

Les conclusions de production doivent venir d'un panel équilibré :

1. Réunions représentatives : priorité haute pour les réglages par défaut.
2. Réunions difficiles mais plausibles : bruit, distance micro, overlaps.
3. Cas extrêmes : stress tests, exclus des moyennes décisionnelles.
4. Vraies réunions fournies ponctuellement : meilleure source, à anonymiser si besoin.

Les scripts de bench trient et documentent. La décision qualité reste une
relecture SRT avec verdict humain.

## Panel recommandé VAD v1

Ce panel évite de surpondérer `test5.wav`.

| Rôle | Fichier | Durée | Usage |
|---|---:|---:|---|
| Représentatif calme | `archives/audio_tests/extrait_reunions/audio1494_calme.wav` | 48s | Vérifier que VAD OFF/ON ne dégrade pas un audio propre |
| Réunion municipale | `archives/audio_tests/extrait_reunions/test7_mairie_debut.wav` | 60s | Vocabulaire légal/municipal, plusieurs locuteurs |
| Réunion générale | `archives/audio_tests/extrait_reunions/reu1138_debut.wav` | 60s | Profil réunion courant |
| Technique interne | `archives/audio_tests/extrait_reunions/freetrans_debut.wav` | 60s | Lexique métier, overlaps courts |
| Difficile plausible | `archives/audio_tests/extrait_reunions/cse_bruit_debut.wav` | 60s | Bruit continu, hallucinations connues |
| Stress test uniquement | `archives/audio_tests/test5.wav` | 29s | Ne pas inclure dans les moyennes décisionnelles |

Commande recommandée pour le panel décisionnel sans `test5` :

```bash
venv/bin/python scripts/bench_audio.py \
  --audio \
    archives/audio_tests/extrait_reunions/audio1494_calme.wav \
    archives/audio_tests/extrait_reunions/test7_mairie_debut.wav \
    archives/audio_tests/extrait_reunions/reu1138_debut.wav \
    archives/audio_tests/extrait_reunions/freetrans_debut.wav \
    archives/audio_tests/extrait_reunions/cse_bruit_debut.wav \
  --matrix vad \
  --gpu-pool 3 \
  --workers 1 \
  --output-dir bench_results/vad_representatif_v1
```

Analyse :

```bash
venv/bin/python scripts/bench_analyze.py \
  --bench-dir bench_results/vad_representatif_v1/audio1494_calme \
  --bench-dir bench_results/vad_representatif_v1/test7_mairie_debut \
  --bench-dir bench_results/vad_representatif_v1/reu1138_debut \
  --bench-dir bench_results/vad_representatif_v1/freetrans_debut \
  --bench-dir bench_results/vad_representatif_v1/cse_bruit_debut \
  --output bench_results/vad_representatif_v1/analysis.md \
  --csv bench_results/vad_representatif_v1/analysis.csv
```

## Inventaire initial

| Fichier | Durée | Catégorie proposée | Décision |
|---|---:|---|---|
| `archives/audio_tests/extrait_reunions/audio1278_calme.wav` | 42s | représentatif calme | utilisable |
| `archives/audio_tests/extrait_reunions/audio1494_calme.wav` | 48s | représentatif calme | utilisable |
| `archives/audio_tests/extrait_reunions/test7_mairie_debut.wav` | 60s | réunion municipale | utilisable |
| `archives/audio_tests/extrait_reunions/test7_mairie_milieu.wav` | 60s | réunion municipale | utilisable |
| `archives/audio_tests/extrait_reunions/reu1138_debut.wav` | 60s | réunion générale | utilisable |
| `archives/audio_tests/extrait_reunions/reu1240_16min.wav` | 60s | réunion générale | utilisable |
| `archives/audio_tests/extrait_reunions/reu1508_23min.wav` | 60s | réunion générale | utilisable |
| `archives/audio_tests/extrait_reunions/reu1732_debut.wav` | 60s | réunion générale | utilisable |
| `archives/audio_tests/extrait_reunions/freetrans_debut.wav` | 60s | technique interne | utilisable |
| `archives/audio_tests/extrait_reunions/freetrans_milieu.wav` | 60s | technique interne | utilisable |
| `archives/audio_tests/extrait_reunions/freetrans2_milieu.wav` | 60s | technique interne | utilisable |
| `archives/audio_tests/extrait_reunions/cse_bruit_debut.wav` | 60s | difficile plausible | utilisable, poids limite |
| `archives/audio_tests/extrait_reunions/cse_bruit_10min.wav` | 60s | difficile plausible | utilisable, poids limite |
| `archives/audio_tests/extrait_reunions/cse_calme_fin.wav` | 58s | CSE calme | utilisable |
| `archives/audio_tests/extrait_reunions/malraux1_propre.mp3` | 32s | propre monologue/littéraire | utilisable |
| `archives/audio_tests/extrait_reunions/test3_propre.mp3` | 16s | extrait très court | appoint seulement |
| `archives/audio_tests/extrait_reunions/test4_propre.mp3` | 49s | propre | utilisable |
| `archives/audio_tests/cse_excerpt_10m_15m.wav` | 900s | long CSE | utiliser par extrait ciblé |
| `archives/audio_tests/reunion1.m4a` | 2790s | vraie réunion longue | à découper avant bench |
| `archives/audio_tests/test7.mp3` | 900s | réunion longue | à découper avant bench |
| `archives/audio_tests/test5.wav` | 29s | stress test extrême | exclure des moyennes décisionnelles |
| `archives/audio_tests/test6.m4a` | 288s | difficile à qualifier | à découper/relire |
| `tests/test1.mp3` | 29s | test court | non décisionnel |
| `tests/test2.mp3` | 73s | test dialogue | appoint |

## Besoin futur : vraies réunions

Une vraie réunion de référence (216 min, 26 locuteurs, ~33 800 mots, transcription
humaine professionnelle) est disponible en local et sert de référence au banc publié
dans [STT_BENCHMARK_REAL_MEETINGS.md](STT_BENCHMARK_REAL_MEETINGS.md). Les chemins et
l'outillage d'extraction sont documentés hors dépôt (`docs/private/`) — les
enregistrements réels ne sont jamais publiés. L'extraction d'une référence DOCX passe
par `scripts/extract_reference_docx.py` (`--output-json` / `--output-srt`).

Les vraies réunions restent prioritaires si elles couvrent :

- micro de salle correct mais non studio ;
- réunion mairie/CSE avec vocabulaire métier ;
- 2 à 6 locuteurs ;
- bruit de fond modéré ;
- au moins un extrait où le contenu attendu est connu.

Même sans transcription de référence complète, un verdict humain par SRT suffit
pour calibrer : `bon`, `acceptable`, `mauvais`, `inutilisable`, avec 2 ou 3 notes
sur les erreurs graves.
