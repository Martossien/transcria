# Attribution des locuteurs à l'étape Résumé — étude des solutions

> Statut : **étude, aucune décision prise** (2026-10-10). Déclenchée par des retours
> d'utilisateurs : à l'étape Participants, la description que la LLM rédige pour chaque
> locuteur est juste, mais elle tombe parfois sur le **mauvais numéro** (SPEAKER_XX).
> Le défaut jumeau (nom et fonction dans le même champ) était un défaut de parseur, corrigé
> le même jour ; celui-ci est structurel et mérite un choix.

## 1. Le constat, et pourquoi ce n'est ni du hasard ni un décalage de numéros

Ce que l'interface et la LLM lisent est **le même fichier** de tours de parole
(`speakers/speaker_turns.json`). La fourchette de locuteurs est envoyée **avant** le
lancement du résumé (`wizard.js` : `speaker-hint` puis `/summary`), donc le backend retenu
par `auto` est le même pour le résumé et pour le pipeline final, et le checkpoint de
diarisation (clé : modèle + empreinte audio + paramètres) garantit des numéros identiques
d'un bout à l'autre. Une redétection manuelle à l'étape 5 réutilise ce checkpoint. Les
numéros ne bougent pas.

Le problème est **ce que la LLM reçoit pour relier un rôle à un numéro** :

1. La **transcription rapide** du résumé est produite **avant** la diarisation, par blocs
   de parole issus du VAD (silence ≥ 400 ms) ou de 30 s fixes. Un bloc contient
   couramment plusieurs locuteurs — sur `tests/test2.mp3`, un bloc de 21 s porte toute une
   conversation à deux voix. Ces blocs n'ont **aucune étiquette de locuteur**.
2. Le **contexte de diarisation** (`summary/diarization_context.md`) n'étiquette que les
   segments où **un seul** locuteur a des tours (`build_labeled_segments`, règle stricte).
   Sur ce même fichier : **3 lignes étiquetées pour 28 tours de parole**.
3. La LLM comprend donc les rôles d'après le contenu (ce qu'elle fait bien), puis doit
   **deviner** quel numéro porte chacun à partir de trois phrases. Quand ces phrases sont
   rares ou peu discriminantes, la bonne description tombe sur le mauvais numéro.

Ce n'est pas aléatoire : c'est une inférence faite sur une évidence trop mince, et plus la
réunion est dense (interruptions, voix qui se chevauchent), plus l'évidence s'amincit.

## 2. Contraintes à respecter par toute solution

- **Plus de 8 locuteurs** : Nemotron 3 plafonne à 8 ; au-delà, `auto` bascule sur pyannote
  (minutes de GPU). Toute solution doit rester valable avec pyannote, et ne pas supposer
  Nemotron.
- **Stabilité des numéros** entre résumé, étape 5 et pipeline final : c'est ce qui fait que
  le nom validé par l'humain arrive sur la bonne voix dans le SRT et le DOCX. Aucune
  solution ne doit introduire une seconde numérotation.
- **Backends sans horodatage mot à mot** (Cohere, moteurs servis en `json`) : une solution
  fondée sur les mots ne couvre pas tout le monde.
- **Coût** : l'étape Résumé est l'attente la plus visible du parcours (l'utilisateur est
  devant l'écran) ; le pipeline final, lui, tourne en file.
- **Topologie split** : le résumé tourne sur le worker GPU, la diarisation peut être
  distante (`remote`) — la solution doit passer par les mêmes coutures que le pipeline.

## 3. Les solutions

### A — Diariser d'abord, transcrire par tour (comme le pipeline final)

Inverser l'ordre de la phase Résumé : diarisation (Nemotron : secondes sur CPU ; pyannote :
minutes, comme aujourd'hui) **puis** transcription rapide découpée par **tours exclusifs**
(`Transcriber._chunk_by_pyannote_turns`, déjà écrit et éprouvé par le pipeline final,
`max_chunk_s = 45`). Chaque segment naît avec son locuteur ; `quick_transcript.txt` et
`diarization_context.md` sont **entièrement** étiquetés.

- Exactitude : la meilleure possible sans horodatage mot à mot — l'attribution est
  acoustique, pas déduite.
- Coût : même quantité d'audio transcrite, mais plus d'appels STT (un par tour fusionné ;
  les voies batchées existent) ; sur une réunion d'une heure, de l'ordre de quelques
  centaines de tours. Le VAD devient inutile (les tours sont un VAD). Avec Nemotron, la
  diarisation ajoute des secondes ; avec pyannote (> 8 voix), rien ne change : elle tournait
  déjà, seulement plus tard.
- Effets secondaires positifs : la **synthèse** elle-même lit « qui dit quoi » ; les
  « Participants probables », l'animateur suggéré et les indices de prénoms (apostrophes)
  reposent sur des tours réels.
- Risques : un tour mal borné coupe une phrase (le pipeline final vit déjà avec, et le
  réalignement mot à mot le corrige quand les mots existent) ; un audio
  `audio_tres_faible` doit garder le repli 30 s (règle existante du pipeline).
- Travail : phase `summary` réordonnée (STT ← diarisation), `SummaryGenerator` branché sur le
  découpage par tours, reprise/cache du transcript rapide, tests, E2E réel.

### B — Garder l'ordre, étiqueter chaque bloc par son locuteur majoritaire

Après la diarisation, marquer chaque bloc de la transcription rapide avec le locuteur qui
couvre ≥ 70 % de sa durée, et `[SPEAKER_00 + SPEAKER_01]` sinon ; fournir cette version à
la LLM.

- Exactitude : partielle — les blocs mixtes (fréquents en réunion dense) restent ambigus,
  exactement ceux qui posent problème aujourd'hui.
- Coût : nul (calcul pur au moment d'écrire le contexte).
- Travail : une demi-journée, sans risque. C'est une **amélioration de l'évidence**, pas une
  solution.

### C — Scinder les blocs aux frontières de locuteurs grâce aux mots horodatés

Whisper rend des horodatages mot à mot (déjà activés) ; `SpeakerPunctuationRealigner`
sait scinder un segment aux changements de locuteur. L'appliquer à la transcription rapide
donne des segments mono-locuteur **sans re-transcrire**.

- Exactitude : excellente là où les mots existent ; la précision des mots de Whisper est
  de l'ordre de ±0,2 s, suffisante.
- Limite : **Whisper seulement** (ni Cohere, ni les moteurs servis en `json`, ni MOSS).
  Kroko a des horodatages par token — à vérifier.
- Coût : nul en STT ; quelques dizaines de ms de calcul.
- Travail : brancher le réaligneur sur la phase Résumé, pour les backends qui le permettent.

### D — Demander à la LLM de se relire

Fournir la table des tours (temps de parole, nombre de tours, premier tour) et lui demander
de vérifier la cohérence de ses attributions.

- Exactitude : faible — on demande à l'inférence de corriger l'inférence, sans nouvelle
  évidence. Écarté.

### E — Pour plus de 8 voix : Nemotron au résumé, pyannote au final, réconciliation

Garder Nemotron (rapide) pour le résumé même au-delà de 8 voix, laisser pyannote faire le
comptage exact au pipeline final, et **réconcilier** les deux numérotations par
recouvrement temporel pour transporter le mapping de l'étape 5.

- Exactitude : Nemotron fusionne les voix au-delà de 8 — le résumé décrirait des locuteurs
  composites, et la réconciliation serait ambiguë précisément sur ces voix.
- Risque : une seconde numérotation, contraire à la contrainte de stabilité.
- Écarté tant que le comptage > 8 reste du ressort de pyannote.

## 4. Comparaison

| | Exactitude | > 8 voix | Backends | Coût résumé | Numéros stables | Effort |
|---|---|---|---|---|---|---|
| **A** diariser puis transcrire par tour | ★★★★ | oui (pyannote) | tous | + secondes (Nemotron) / = (pyannote) | oui | moyen |
| **B** locuteur majoritaire par bloc | ★★ | oui | tous | nul | oui | faible |
| **C** scission par mots horodatés | ★★★★ | oui | Whisper (Kroko ?) | nul | oui | faible-moyen |
| D relecture LLM | ★ | — | — | + tokens | oui | faible |
| E Nemotron + pyannote réconciliés | ★★ | partiel | — | — | **non** | élevé |

## 5. Recommandation

**A est la solution de fond**, parce qu'elle corrige la cause (une évidence trop mince)
pour tous les backends et toutes les tailles de réunion, et qu'elle améliore la synthèse
elle-même. Le pipeline final le fait déjà ; la phase Résumé ne le faisait pas parce que la
diarisation coûtait des minutes — avec Nemotron, cet argument est tombé, et avec pyannote
(> 8 voix) le coût est inchangé, seulement déplacé avant la transcription.

**C est un complément bon marché** pour les backends à mots horodatés : il resserre
l'attribution à l'intérieur d'un tour quand la diarisation a bordé large.

**B peut se livrer tout de suite** si l'on veut une amélioration sans réorganiser la phase ;
elle ne résout pas les blocs mixtes et ne doit pas être prise pour la correction.

Avant de trancher, **une mesure** : sur trois réunions réelles (dont une de plus de 8 voix),
compter les attributions justes de « Participants probables » aujourd'hui et avec A, et
chronométrer la phase Résumé dans les deux cas. Le banc existe (`scripts/bench/`,
`banc_regles/`), il suffit d'y ajouter ce critère.

## 6. Ce qui ne change pas quoi qu'on choisisse

- Les numéros restent ceux de la diarisation, dans le même ordre, du résumé au DOCX.
- Le parseur des « Participants probables » reste tolérant (nom — fonction — rôle).
- L'humain valide à l'étape 5 : on améliore une **suggestion**, jamais une décision prise à
  sa place.
