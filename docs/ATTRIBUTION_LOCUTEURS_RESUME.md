# Attribution des locuteurs à l'étape Résumé — étude des solutions

> Statut : **mesurée** (2026-10-10, §7) — l'option A est implémentée derrière
> `workflow.summary_by_turns` (défaut `false`) ; son activation par défaut est une décision de
> version. Déclenchée par des retours
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

## 7. Mesure (2026-10-10) — option A implémentée et jouée sur trois réunions réelles

### Protocole

Option A est implémentée derrière `workflow.summary_by_turns` (défaut `false`) : la phase
Résumé lance la diarisation **avant** la transcription rapide
(`phases/summary.run_speaker_detection_before_transcription`, best-effort : VRAM indisponible
ou diarisation absente ⇒ ordre historique), et `SummaryGenerator` découpe l'audio par tours
exclusifs avec **la même fonction que le pipeline final** (`transcription.chunk_by_turns`).
Chaque segment de `quick_transcript.txt` porte alors son locuteur ; `diarization_context.md`
est étiqueté en entier.

Le banc (`scripts/bench/attribution_bench.py`) joue la seule phase Résumé sur un audio réel
dans un mode donné et écrit, par SPEAKER_XX, ce que la LLM a attribué et les phrases
réellement prononcées par cette voix (`scripts/bench/attribution_report.py`). Le verdict est
une **lecture humaine** : une description est *juste* si ce qu'elle dit de la voix correspond à
ce que cette voix dit ; *fausse* si le contenu attribué est prononcé par une autre voix (vérifié
par mots-clés dans le transcript étiqueté) ou inventé à partir d'une mention faite par un
tiers ; *générique* si elle ne dit rien (« intervention courte »). Les prénoms ne sont pas
comptés : sans vérité terrain sur les voix, ils restent une inférence de la LLM dans les deux
modes. Même LLM d'arbitrage, même STT rapide, même machine (8× RTX 3090), runs séquentiels.

| Réunion | Durée | Voix | Diarisation | Particularité |
|---|---|---|---|---|
| M1 | 1 h 52 | 7 | `auto` → Nemotron 3 | revue à sept voix, alternance rapide (2 068 tours) |
| M2 | 35 min (fenêtre) | 8 détectées / 19 en référence | `pyannote` | séance plénière > 8 voix, nombreuses voix brèves |
| M3 | 1 h 07 | 4 | `auto` → Nemotron 3 | réunion technique, longs monologues |

### Exactitude des « Participants probables »

| | Historique (STT par blocs, diarisation ensuite) | Option A (`summary_by_turns`) |
|---|---|---|
| M1 (7 voix) | **4 justes**, 2 fausses, 1 voix **non décrite** | **7 justes** |
| M2 (8 voix) | **3 justes**, 1 fausse, 1 douteuse, 3 génériques | **7 justes**, 1 fausse |
| M3 (4 voix) | **4 justes** | **4 justes** |
| Total | **11 / 19** | **18 / 19** |
| Couverture étiquetée du transcript lu par la LLM | 0 % (+ lignes « certaines » seules) | 100 % |

Lecture des erreurs, parce qu'elles sont exactement le symptôme rapporté :

- M1, historique : la description de la **troisième voix** (arbitrage budgétaire, nouvelle
  infrastructure) reprend un contenu que le transcript étiqueté montre **prononcé par la
  deuxième voix** ; la **sixième voix** n'est décrite que par la phrase d'accueil où l'animateur
  la présente (rien de ce qu'elle dit) ; la **quatrième voix** (7 minutes de parole) n'est pas
  décrite du tout. En option A, les sept descriptions correspondent aux propos de chaque voix.
- M2, historique : la description de la **deuxième voix la plus bavarde** (sujets techniques
  longuement développés) est celle des propos de la **troisième** ; trois voix reçoivent
  « membre, intervention courte ». En option A, six voix reçoivent une description conforme ;
  la seule erreur est un **prénom** pris dans une phrase où la voix parle d'une tierce personne
  (« c'est plutôt X qui… ») — une inférence de la LLM, pas un défaut d'attribution de numéro.
- M3 : quatre voix aux rôles très contrastés et aux longs monologues — l'ordre historique
  suffit (136 lignes « certaines ») ; l'option A ne change rien à l'exactitude.

L'option A transcrit aussi **11 à 14 % de mots en plus** (M1 : 20 392 → 23 214 ; M2 : 5 312 →
5 983 ; M3 : 12 256 → 14 274) : les tours de diarisation retiennent de la parole que le VAD
coupait (débuts et fins de prise de parole, interjections).

### Coût

Durées reconstruites à partir des horodatages des artefacts (STT = transcription rapide,
LLM = résumé opencode) :

| | Diarisation | STT rapide | LLM | Phase Résumé |
|---|---|---|---|---|
| M1 historique | 127 s | 250 s | 310 s | 695 s |
| M1 option A | 109 s | **415 s** | 270 s | 812 s (**+17 %**) |
| M2 historique | 37 s | 81 s | 151 s | 272 s |
| M2 option A | 33 s | **108 s** | 185 s | 335 s (**+23 %**) |
| M3 historique | 87 s | 161 s | 169 s | 432 s |
| M3 option A | 70 s | **233 s** | 170 s | 491 s (**+14 %**) |

Le surcoût est entièrement dans la STT par tours (1,3 à 1,7× la STT par blocs : 2 068
segments de 3,2 s en moyenne sur M1 contre 338 de 19 s), pas dans la diarisation (elle
tournait déjà) ni dans la LLM (un transcript plus long mais étiqueté ne lui coûte pas plus).
Pistes si ce coût gêne : regrouper les tours consécutifs d'un même locuteur jusqu'à
`max_chunk_s` (déjà le cas — l'alternance de M1 est réelle), ou servir la STT rapide par un
moteur batché.

### Conclusion

L'option A corrige le symptôme rapporté : **18 descriptions justes sur 19 contre 11**, et
aucune voix laissée sans description, pour **+15 à +25 %** sur l'étape Résumé. Elle est valable
avec pyannote au-delà de huit voix (M2) et n'introduit aucune numérotation nouvelle (mêmes tours,
même checkpoint). Recommandation : **activer `workflow.summary_by_turns` par défaut à la
prochaine version**, en gardant `false` comme repli documenté pour qui préfère la vitesse. Les
compléments B/C restent sans objet une fois A en place.

Deux défauts attrapés en lisant le banc, corrigés le même jour : le parseur coupait
« Pharmacien(ne) » en nom + fonction « ne » (la parenthèse doit être précédée d'un espace pour
séparer une fonction) ; et le bench a montré que les prénoms restent une inférence fragile
(mention d'un tiers) — hors du périmètre de cette mesure.

