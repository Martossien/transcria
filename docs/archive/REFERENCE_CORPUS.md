# Corpus de référence & déterminisme (C4.2)

> Chantier C4.2 (docs/archive/RELEASE_0.2.0.md). Détecter les régressions SUBTILES du pipeline
> (qualité de transcription, résumé, diarisation) qu'aucun test unitaire ne voit, et
> vérifier que le pipeline est reproductible.

## Corpus

5 à 8 audios réels représentatifs, avec une sortie humaine-validée stockée hors dépôt
(données réelles → jamais versionnées ; contenu abstrait uniquement dans les tests).

| Critère | Cible |
|---|---|
| Langue | français |
| Locuteurs | 1 à 4 (limite Sortformer) |
| Qualités | studio, salle de réunion, téléphone/visio |
| Durées | 1 court (< 5 min), 1 moyen (~30 min), 1 long (≥ 2 h) |
| Cas piégeux | chevauchements, musique/silence, jargon métier (lexique) |

`tests/test2.mp3` (réunion fromagerie) sert de référence rapide pour les E2E de
développement. Le corpus complet est constitué par l'opérateur sur la machine GPU.

## Méthode

Harnais : `scripts/verify_split_topology.py` (`run_job`) — le même qui pilote les E2E
réels. Pour chaque audio :

1. **Diff qualité** : la sortie du pipeline est comparée à la référence humaine
   (SRT + résumé + participants). Un écart au-delà d'un seuil = régression à expliquer.
2. **Déterminisme** : le même audio passé DEUX fois → les sorties doivent être
   identiques, ou l'écart (température LLM, seeds) est DOCUMENTÉ et borné.

## Exécution (operator-run, GPU)

```bash
export TRANSCRIA_GPU_E2E=1
python scripts/verify_split_topology.py --audio <ref>.mp3 --profile word_corrige
# rejouer 2× le même audio et diff les livrables
```

Ce chantier est **operator-run** (Docker + GPU, non exécutable en CI ni dans le flux
assistant). Les invariants de qualité GPU-free (SRT bien formé, ordre des timestamps,
DOCX/ZIP ouvrables, résumé non tronqué, formes incohérentes signalées) sont, eux,
vérifiés à CHAQUE run par `transcria/quality/` (§8.3 du plan) — ils constituent le
filet automatique permanent ; le corpus est le filet humain périodique.
