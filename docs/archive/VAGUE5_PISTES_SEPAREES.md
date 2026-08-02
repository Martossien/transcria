# Vague 5 — Pistes séparées, suivi en direct, et le sort de `LiveConnectorSession`

> **Statut : VALIDÉ par l'utilisateur le 2026-07-30 (y compris D5.6). Lot A LIVRÉ**
> (56a085d) **et GATE RÉEL RÉUSSI le soir même** : job `1ea75400`, 2 participants, 2
> pistes nommées dans `input/tracks/`, manifeste v2 sans dégradation, et **7,91 s de
> chevauchement réel** entre les deux voix — le corpus de validation du lot B. **Lot B1
> LIVRÉ le soir même** (STT par piste + fusion) et **prouvé sur ce corpus avec Cohere
> réel** : l'interruption croisée à 30 s (« C'était où la ferme ? » PENDANT le récit de
> l'autre, réponse comprise) est captée DES DEUX CÔTÉS, chacun sous son nom — 7 segments
> de chevauchement intacts, protégés de l'arbitrage multi-STT. **Lot B2 LIVRÉ** :
> sous-diarisation pyannote PAR PISTE dans `SpeakerDetector` (pistes non `solo`,
> ≥ 10 s de parole ; 2 voix et plus → `PISTE_<pid>_S1`… dans `speaker_turns.json`,
> audit `speakers/track_diarization.json`), transcription découpée par les tours
> exclusifs de la sous-voix, garde de phase (les tours `source: manifest` ne sont
> JAMAIS écrasés par une re-diarisation du mix en profil qualité — trou préexistant
> bouché), rôles LLM étendus aux ids `PISTE_…`. Une seule voix trouvée → rien ne
> change (nom proposé, cas fluide D5.3). Gate réel « salle » (2 personnes derrière
> UN micro) reporté (décision utilisateur : on avance). **Lot C LIVRÉ** : le bot émet
> `{"bot_caption": …}` par tour final (BOT_EVENTS=json), le runner les regroupe
> (25 tours ou 2 s) et les POSTe à `/v1/meetings/<sid>/captions` (gardes de
> `/events` : claimant + session active), le portail les APPEND dans
> `live/captions.jsonl` plafonné (`connectors.meetings.max_caption_lines`, défaut
> 2000, troncature de tête ANNONCÉE, numérotation monotone), la page du job
> (état `in_meeting`) affiche « Suivi en direct — provisoire » via
> `GET /api/meetings/<sid>/captions?after=<n>` (visibilité du job porteur) et le
> panneau s'efface à l'ingestion (rechargement d'état existant). **Lot D LIVRÉ** :
> révision ADR-001 D5 écrite (niveau 2 réalisé, captions.jsonl = trace non-référence),
> sort de `LiveConnectorSession` documenté à la classe (contrat des connecteurs, pas du
> bot), REPRISE de `TEMPS_REEL_REUNIONS.md` toilettée (L1/L3/L4 absorbés par les vagues
> 3-5), CHANGELOG. **LA VAGUE 5 EST CLOSE côté code** — restent les gates réels
> « salle » et « suivi live » avec l'utilisateur, et la revue sécurité Opus 5.
> Spécification dédiée exigée par le plan directeur
> ([`UI_REUNIONS_WORKFLOW.md`](UI_REUNIONS_WORKFLOW.md), vague 5) : elle plie dans la
> conception les leçons des gates réels de juillet — pistes sur DISQUE (jamais en RAM),
> continuité d'échantillons par flux, fenêtres d'énergie du registre, placement de la
> façade par l'allocateur / `live.facade.inference_url`, contrat multipart validé
> strictement. Réalise le niveau 2 de la décision D5 du plan et amène la révision
> correspondante d'[`ADR-001`](../adr/ADR-001-frontiere-ingestion-reunions.md).

---

## 0. Lecture rapide — pourquoi cette vague, et ce qu'elle change

**Le problème réel** (décision utilisateur, 2026-07-30 : « perdre l'avantage des locuteurs
fait perdre beaucoup trop ») : aujourd'hui le bot capte chaque participant séparément…
puis **mélange tout** dans un seul WAV. Le manifeste (fenêtres de parole par piste) permet
de retrouver *qui* parlait *quand*, mais dès que deux personnes parlent EN MÊME TEMPS,
leurs voix sont sommées : le STT n'entend qu'une bouillie et **les mots du chevauchement
sont perdus**. Et une piste « salle » (plusieurs personnes derrière un micro) reste UN
locuteur, faute de pouvoir la diariser seule.

**Ce que livre la vague 5** :

1. **Pistes séparées** (priorité n°1) — le bot conserve l'audio de CHAQUE piste sur
   disque, l'ingestion les stocke à côté du mix, et le pipeline transcrit **piste par
   piste** puis fusionne par timestamps : les mots du chevauchement existent chacun sur
   leur piste, donc chacun dans le SRT. Les pistes « salle » gagnent leur propre
   diarisation — la limite « une salle = un locuteur » saute.
2. **Suivi en direct** — pendant la réunion, la page du job affiche les tours de parole
   au fil de l'eau (marqués « provisoires » : le batch reste la référence, ADR-001 D5).
3. **Sort de `LiveConnectorSession` tranché** — voir §5 : le contrat reste celui des
   futurs connecteurs plateforme ; le bot suit son chemin éprouvé. C'est une RÉDUCTION
   de périmètre assumée, à valider explicitement.

**Ce que la vague ne change PAS** : le mix `original.wav` reste produit et stocké (repli,
préflight qualité, compatibilité totale avec les bots anciens et les connecteurs
post-réunion qui n'auront JAMAIS de pistes) ; le workflow humain reste souverain (étape 5
valide toujours les noms) ; la frontière d'ingestion reste `/v1/audio/ingest`.

---

## 1. Décisions d'architecture

### D5.1 — Enregistrement PAR PISTE, sur DISQUE, timeline commune

Le bot écrit un **WAV par participant** (`tracks/<pid>.wav`, s16le mono 48 kHz),
incrémentalement, **sur disque dans le conteneur** — jamais en RAM : le mix actuel
accumule ~330 Mo/heure en mémoire ; à N pistes ce serait des Go (leçon consignée).
Le mix passe lui aussi sur disque par la même occasion.

Chaque piste vit sur la **timeline commune** de la réunion (silence comblé par des zéros),
avec la **continuité d'échantillons par flux** éprouvée au gate du 2026-07-30 (la gigue
réseau ne déplace pas l'audio ; l'horloge n'ancre que le début et resynchronise après une
vraie coupure). Le cœur de placement (curseur de continuité) est EXTRAIT de `MeetingMixer`
en un module partagé — une seule implémentation pour le mix ET les pistes, testée une fois.

Conséquence précieuse : une piste étant alignée sur la timeline commune, **les timestamps
du STT sur cette piste SONT les timestamps de la réunion** — la fusion est un tri, pas un
recalage.

### D5.2 — Contrat d'ingestion v2 (multipart), compatibilité TOTALE

`POST /v1/audio/ingest` accepte, EN PLUS des parts actuelles (`file` = mix,
`participants_manifest`) : une part **par piste**, nommée `track_<pid>`. Le manifeste
passe en **version 2** : chaque participant peut porter `"track": "track_<pid>"`
(référence de part) ; tout le reste (kinds, speech_windows) est inchangé. Validation
STRICTE côté serveur (comme la v1) : part référencée absente, part orpheline, ou pid
inconnu → manifeste REJETÉ en bloc, ingestion en mode mix (dégradé journalisé, jamais
un état à moitié).

Stockage job : `input/original.wav` (inchangé) + `input/tracks/<pid>.wav`.

**Règle de compatibilité (non négociable)** : sans parts de piste, le comportement est
octet pour octet celui d'aujourd'hui. Les bots anciens, les autres plateformes et les
connecteurs post-réunion (Zoom Cloud Recording…) n'ont pas de pistes et doivent
continuer de fonctionner sans une ligne de changement.

Gardes de taille : `connectors.meetings.max_track_mb` (défaut 512) par part et
`max_tracks` (défaut 16). Au-delà de `max_tracks` participants, les pistes excédentaires
ne sont PAS envoyées (le mix les couvre) et le manifeste le dit
(`"track_overflow": true`) — dégradation annoncée, jamais silencieuse.

### D5.3 — Pipeline : STT par piste, découpé par FENÊTRES, puis fusion par tri

Le pipeline bascule en mode « par piste » quand le job possède des pistes ET un manifeste
v2 — sinon chemin historique (mix + pyannote), intact.

- **Découpe par fenêtres d'abord** : on ne transcrit PAS des heures de silence. Les
  `speech_windows` du manifeste (déjà seuillées à l'énergie — leçon du gate) découpent
  chaque piste en segments voisés (+ marge configurable, défaut 0,4 s, fenêtres
  fusionnées sous 2 s d'écart) ; le STT tourne sur ces extraits et ses timestamps sont
  ré-offsetés. C'est LE levier de coût : une réunion de 2 h où quelqu'un a parlé 10 min
  coûte 10 min de STT sur sa piste, pas 2 h.
- **Piste NOMMÉE solo** : STT direct, locuteur = nom du participant. Aucune diarisation.
- **Piste salle / inconnue** (règle de prudence établie : `unknown` → salle) : pyannote
  SUR LA PISTE → voix `PISTE_<pid>_S1`, `_S2`… ; attribution des segments STT par
  recouvrement (mécanique d'attribution existante, appliquée par piste). Si pyannote ne
  trouve qu'UNE voix et que la piste est nommée → le nom est proposé directement (le cas
  « salle d'une seule personne » redevient fluide) ; la validation humaine tranche
  toujours (étape 5, « Une seule personne ? » conservé par piste salle).
- **Le modèle STT est chargé UNE fois** et itère les pistes (même backend, même carte,
  réservation d'allocateur unique pour la phase) — jamais un chargement par piste.
- **Fusion** : module PUR `workflow/track_fusion.py` — concatène les segments de toutes
  les pistes (déjà en temps global), trie par début, produit
  `transcription_segments.json` + SRT. Les chevauchements deviennent des sous-titres aux
  timecodes qui se recouvrent (SRT le permet) — **les mots des deux locuteurs existent**.
- **Qualité** : le préflight (DNSMOS/SQUIM/scène) reste calculé sur le MIX — c'est le
  paysage sonore global. Le chemin qualité/multi-STT s'applique par piste avec la
  mécanique existante par fichier. Correction, résumé, relecture : INCHANGÉS (ils
  consomment segments + SRT, peu importe d'où ils viennent).

### D5.4 — Étape 5 : mêmes écrans, meilleure matière

Pistes solo : noms pré-remplis (comme aujourd'hui). Pistes salle : l'encadré « Micro
partagé » existant liste `PISTE_<pid>_S1`… à nommer — mêmes composants, même validation
souveraine, y compris le cas « participant sans nom côté plateforme » (encadré explicatif
livré le 2026-07-30). Aucune nouvelle notion d'interface.

### D5.5 — Suivi en direct : POLL simple, marqué provisoire (ADR-001 D5 niveau 2)

Le bot produit DÉJÀ des tours finaux en direct (façade STT). Nouveau chemin, volontairement
sans websocket ni SSE (le poll de 5 s existe et suffit à un suivi de réunion) :

- le bot POSTe ses tours par lots à **`/v1/meetings/<sid>/captions`** (jeton runner,
  session claimée par CE runner — mêmes gardes que `/events`) ;
- le portail les APPEND dans `live/captions.jsonl` du job (plafond
  `connectors.meetings.max_caption_lines`, défaut 2 000 — au-delà, rotation par
  troncature de tête, annoncée dans le flux) ;
- la page du job (état `in_meeting`) affiche un panneau « Suivi en direct — provisoire »
  alimenté par `GET /api/meetings/<sid>/captions?after=<n>` (droits du propriétaire du
  job, delta incrémental).

À l'ingestion, le panneau laisse place au pipeline : le direct PRÉCÈDE le canonical, ne le
remplace jamais — c'est la révision D5 à écrire dans l'ADR (le fichier `captions.jsonl`
est conservé comme trace, marqué non-référence).

La façade profite de `live.facade.inference_url` (déjà câblé) : sur une machine chargée,
le live STT peut partir sur un nœud — le placement local par l'allocateur reste le défaut.

### D5.6 — `LiveConnectorSession` : contrat des CONNECTEURS, pas du bot (réduction assumée)

L'audit la disait « câblée nulle part ». Lecture faite : `LiveConnectorSession` modélise
« suivi live via un provider, puis récupération de l'ARTEFACT d'enregistrement de la
plateforme, puis ingestion » — exactement le monde des connecteurs §5 de
`TEMPS_REEL_REUNIONS.md` (Zoom RTMS + Cloud Recording…), PAS celui du bot, qui EST sa
propre source d'enregistrement et suit un chemin plus riche (mixage, pistes, manifeste),
éprouvé par quatre gates réels. La câbler dans le bot serait de l'abstraction pour
l'abstraction.

**Décision proposée** : elle reste le contrat d'orchestration des connecteurs plateforme
(documentée comme telle, docstring mise à jour, tests conservés) ; le bot n'y passe pas.
Le plan directeur est amendé (« câbler LiveConnectorSession » → « sort tranché »).
**Point à valider explicitement** — c'est la seule coupe de périmètre de cette vague.

---

## 2. Catalogue des cas limites (et leur réponse)

| Cas | Réponse |
|---|---|
| Participant qui REJOINT (nouvel endpoint Jitsi → nouveau pid) | deux pistes, souvent le même nom → l'étape 5 les présente séparément avec le même nom pré-rempli ; les nommer pareil suffit (la fusion par nom est un raffinement ultérieur, pas un prérequis) |
| Participant sans nom | règle existante : piste salle prudente + encadré explicatif |
| Plus de `max_tracks` participants | pistes excédentaires non envoyées, couvertes par le mix, `track_overflow` annoncé |
| Disque du conteneur plein en cours de réunion | l'écriture de pistes s'arrête, le mix (prioritaire) continue ; manifeste marqué `tracks_degraded`, ingestion en mode mix — la réunion n'est JAMAIS perdue |
| Échec d'upload d'UNE part de piste | tout-ou-rien côté manifeste v2 : sans la part, validation stricte → mode mix journalisé (pas d'état à moitié) |
| Piste 100 % silencieuse (micro coupé toute la réunion) | zéro fenêtre → zéro STT → absente du SRT ; listée à l'étape 5 comme « présente, silencieuse » |
| Réunion très longue | pistes sur disque (RAM constante) ; garde `max_track_mb` |
| Bot ancien / autre plateforme sans pistes | chemin historique intact (règle D5.2) |
| Chevauchement dans une même piste SALLE | limite résiduelle documentée : pyannote sur la piste fait au mieux, comme aujourd'hui sur le mix — mais le chevauchement INTER-pistes (le cas majoritaire en visio) est, lui, résolu |

## 3. Réutilisé, pas réinventé

`capture.js`/pont PCM (framing par participant inchangé) · curseur de continuité du
`MeetingMixer` (extrait, partagé) · `ParticipantLedger` + seuil d'énergie ·
`parse_participants_manifest` (étendu v2, validation stricte) · `/v1/audio/ingest` +
`MeetingImport` (idempotence) · attribution segments↔diarisation existante · projection
étape 5 + encadré salle + « Une seule personne ? » · façade STT + `inference_url` +
placement allocateur · machine d'états sessions + `/events` (patron des `/captions`) ·
poll 5 s de la page job.

## 4. Lots de livraison

| Lot | Contenu | Effort | DoD |
|---|---|---|---|
| **A — Capter et livrer les pistes** | curseur partagé extrait ; `TrackRecorder` disque (+ mix sur disque) ; manifeste v2 ; parts `track_<pid>` ; stockage `input/tracks/` ; gardes taille/overflow/dégradé | M | gate Jitsi réel : le job contient mix + pistes alignées ; bot ancien simulé → comportement d'aujourd'hui à l'octet près ; suite + E2E verts |
| **B — Transcrire par piste et fusionner** | découpe par fenêtres ; STT par piste (modèle chargé 1×) ; sous-diarisation des pistes salle (`PISTE_<pid>_S1`) ; `track_fusion` pur ; SRT à chevauchements ; étape 5 branchée | L | gate réel à 2 participants parlant EN MÊME TEMPS : les mots des DEUX sont dans le SRT, chacun sous son nom ; réunion mix-only → résultat identique à avant |
| **C — Suivi en direct** | `/v1/meetings/<sid>/captions` (POST runner, GET propriétaire) ; `live/captions.jsonl` plafonné ; panneau « provisoire » sur la page job ; bot POSTe par lots | M | pendant un gate réel, la page affiche les tours < 10 s après la parole ; à l'ingestion le panneau s'efface au profit du pipeline |
| **D — Dette de décision** | révision ADR-001 D5 (niveau 2 réalisé, live=provisoire/batch=canonical) ; sort de `LiveConnectorSession` documenté ; REPRISE de `TEMPS_REEL_REUNIONS.md` toilettée (L1/L3/L4 absorbés par vagues 3-4) ; CHANGELOG | S | doctor/docs cohérents ; plans à jour |

Ordre : A → B → C → D. Discipline inchangée : gates CI complets + suite + E2E réel avant
CHAQUE push ; gates Jitsi réels avec l'utilisateur aux DoD de A, B et C.

## 5. Hors périmètre (dit explicitement)

Fusion automatique de deux pistes au même nom (raffinement post-v1) · multi-STT arbitré
par piste au-delà de la mécanique existante · websockets/SSE pour le direct · pistes pour
les connecteurs post-réunion (leurs artefacts n'en ont pas) · JWT d'instances Jitsi
privées (capacité env livrée le 2026-07-30, hors vague) · la revue **sécurité** des
nouveaux endpoints (`/captions`) et du contrat v2 — passage obligé, HORS de ce document
(Opus 5), avant mise en service.
