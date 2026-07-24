# Temps réel & connecteurs de réunion — plan directeur

> **Statut** : plan directeur **partiellement implémenté**. Livré : Phase 0
> (3 coutures), **Phase K** (façade STT `/v1/audio/transcriptions` + `/v1/audio/ingest`
> fichier + garde durée), **micro direct** (Phase 0-bis). Aucun connecteur de
> plateforme ni passerelle live n'est encore livré. Chaque phase suivante est
> « go »-ée séparément. Le raisonnement d'architecture et les alternatives rejetées
> vivent dans [`docs/adr/ADR-001-frontiere-ingestion-reunions.md`](adr/ADR-001-frontiere-ingestion-reunions.md)
> (source de vérité des décisions) ; ce plan n'énonce que les décisions **actives**.

## Positionnement stratégique — pourquoi ce chantier

TranscrIA est **fort sur son cœur** : donner le son d'une réunion + sa
transcription à l'utilisateur, avec une **qualité documentaire de référence**.
Ce chantier ne vise **qu'à combler le seul trou central**, sans toucher au cœur.

### Le moat vs les concurrents *(appréciation qualitative — PAS un benchmark commun)*

| Axe | TranscrIA | Meetily | Scriberr | Otter/Fireflies | Teams Recap |
|---|---|---|---|---|---|
| Souveraineté / auto-hébergement | **Excellent** | Excellent | Excellent | Faible-moyen | Moyen |
| **Ingestion auto post-réunion** (cible **Très bon**) | **Faible → Très bon** ⚠ | Moyen | Faible | Excellent | Excellent (Teams) |
| **Transcription / assistance live** (cible **Bon** sur Visio+Zoom) | **Faible → Bon** ⚠ | Excellent | Moyen | Excellent | Excellent (Teams) |
| *Bot universel multi-plateforme* | *hors périmètre assumé* | Excellent | Moyen | Excellent | Excellent (Teams) |
| Réunions longues | **Excellent** | Bon | Bon | Bon | Bon |
| Validation humaine du verbatim | **Excellent** | Moyen | Bon | Moyen | Moyen |
| Comptes rendus Word formels | **Excellent** | Limité-bon | Limité | Moyen | Moyen |
| File GPU / reprise / multi-worker | **Excellent** | Limité | Limité | SaaS | Microsoft |
| Identité / RBAC / audit sur site | **Très bon** | Limité | Limité | Bon (SaaS) | Excellent (M365) |
| CRM / calendrier / collaboration | Faible | En dev | Limité | Excellent | Excellent (M365) |
| Facilité de déploiement | Moyenne-**faible** ⚠ | Bonne | Très bonne | Excellente | Excellente |
| Liberté de choix des modèles | **Excellente** | Très bonne | Bonne | Faible | Faible |

**Lecture** : 7 axes « Excellent » forment le moat (le « boring enterprise » en
aval + souveraineté + liberté des modèles). Le trou central se scinde en **deux
axes** (ADR-001 D8) : l'**ingestion auto post-réunion** — qu'on peut porter à
**Très bon** sur les 4 via les API officielles — et la **transcription live** —
cible **Bon** sur Visio/Zoom seulement. Le **bot universel** reste hors périmètre
assumé. (Le CRM/calendrier est faible aussi, mais périphérique — hors périmètre.)

### 🎯 Objectif calibré : viser « Bon », PAS « Excellent »

**But mesurable de ce chantier : faire passer « Capture live & bot de réunion »
de Faible → BON** (pas Excellent). Levier principal = l'**ingestion post-réunion
officielle des 4 plateformes** (fait passer TranscrIA de « upload manuel » à
« couvre l'essentiel des réunions d'entreprise »), complétée par le **live natif**
(Visio, Zoom) et le **micro** (présentiel/dictée).

**Pourquoi PAS « Excellent »** : « Excellent » (Otter/Meetily) = un **bot live sur
TOUTES les plateformes** — qu'on évite délibérément (fragile, CGU, anti-souveraineté).
Teams-live et Meet-live restent des **trous assumés** (couverts en post-réunion).
On s'aligne sur les concurrents par l'**outcome** (couvrir les réunions) et la
**robustesse**, pas par l'UX du bot universel.

**Portée utilisateurs** : de « seulement ceux qui uploadent un fichier » à « tout
utilisateur dont les réunions sont sur Visio/Zoom/Teams/Meet » — *gated* côté
org/admin (OAuth, activation enregistrement), ce qui **colle au positionnement
entreprise/souverain**.

### Thèse : la capture est une COMMODITÉ, pas le moat

La capture live/bot est un marché mûr et open-source (Vexa, Attendee,
livekit-agents, Meetily…). La réécrire = réinventer ce que d'autres font déjà
« Excellent », sur un axe qui **n'est PAS notre valeur**. Donc :

> **On FORKE/ADAPTE une couche de capture ; on met notre effort sur la COUTURE
> qui la déverse dans notre pipeline** — là où vivent les 7 « Excellent ». Le
> live est un *feeder* bon marché ; la valeur reste le **document validé**.

### Garde-fou révélé par le tableau

« Déploiement : moyenne-**faible** ». Ajouter un service de capture = plus de
pièces mobiles → **risque d'empirer ce déjà-faible**. Règle absolue : la capture
est **opt-in, isolée, jamais requise** — un TranscrIA « upload + pipeline »
classique ne voit **rien** de neuf à installer.

### 🔑 La clé de voûte : une frontière d'ingestion commune (ADR-001 D1)

La couture commune n'est PAS un unique endpoint STT — l'analyse du code réel
montre **trois voies** d'intégration distinctes (cf. ADR-001 D1) :

1. **Artefacts post-réunion** (Zoom/Teams/Meet/Visio) → webhook + OAuth + fetch
   MP4/VTT/WAV → stockage → **job async** (ingestion + API de jobs).
2. **Média live** (LiveKit / Zoom RTMS / Meet Media API) → **passerelle live async**
   → STT live + enregistrement horodaté → job final.
3. **Client STT OpenAI** (micro / agent / Vexa) → `POST /v1/audio/transcriptions`
   → réponse STT **synchrone bornée**.

> **La façade OpenAI Audio (Phase K, livrée) est l'adaptateur de la voie 3**, pas
> la frontière universelle. Les clients STT s'y branchent ; les plateformes
> post-réunion se branchent sur l'ingestion d'artefacts + l'API de jobs ; le live
> passe par la passerelle async. `POST /v1/audio/ingest` (Phase K) est l'embryon
> de la voie 1 (dépôt **fichier**, le fetch URL contraint arrive au 1er connecteur).

### Découverte : le post-réunion est OFFICIEL sur les 4 plateformes

Contrairement à une première intuition, **aucune plateforme n'impose de bot pour
le post-réunion** — toutes ont une API officielle d'artefacts. Le browser-bot ne
sert QUE pour le live de Meet/Teams (fragile, optionnel) :

| Plateforme | Post-réunion (officiel, ZÉRO bot) | Live temps réel |
|---|---|---|
| **Visio** (LiveKit) | Egress → URL POST | ✅ livekit-agent (natif) |
| **Zoom** | Cloud Recording API | ✅ **RTMS** (officiel, par participant) |
| **Teams** | **Graph** (VTT/MP4, webhook chiffré, API facturées à l'usage) | 🟠 bot RTM (MS déconseille) |
| **Meet** | **Meet REST API v2 + Drive** | 🔬 **Meet Media API** (officielle, Developer Preview) |
| **Micro direct** (présentiel/dictée) | fichier → façade/job | ✅ WhisperLiveKit (WS) |

→ On couvre les 4 plateformes **en post-réunion sans une seule ligne de
browser-automation** ; le live seulement là où c'est propre (Visio natif, Zoom
RTMS). **Vexa quitte la feuille de route principale** — réservé au Meet-LIVE
optionnel (extrait de code, pas la plateforme).

## Décisions d'architecture → ADR-001

Le tri des revues externes (retenu / différé / rejeté) et le raisonnement complet
vivent dans [`docs/adr/ADR-001-frontiere-ingestion-reunions.md`](adr/ADR-001-frontiere-ingestion-reunions.md).
Décisions actives à retenir ici : frontière d'ingestion à 3 voies (D1), enregistrement
d'import minimal + idempotence composite (D2), contrat provider par capacités (D3),
séparation contrôle/données (D4), révisions live/canonical distinctes (D5), provenance
selon le moteur (D6), transcription plateforme = auxiliaire (D7), post-réunion officiel
des 4 + Meet Media API avant bot (D8), façade sync bornée taille+durée (D9), gouvernance
transversale (D10). Les sections ci-dessous reflètent ces décisions.

## 0. Principe directeur (non négociable)

**Le direct sert à SUIVRE la réunion ; le pipeline TranscrIA produit le
DOCUMENT DE RÉFÉRENCE.** Le temps réel ne remplace jamais le pipeline : il le
*précède*. C'est ce qui protège la force de TranscrIA (la qualité documentaire
finale) tout en comblant son seul manque concurrentiel (le suivi en direct).

Corollaires :
- **Ossature intacte.** Le cœur (Flask app-factory `create_app`, gunicorn
  **sync**, pipeline batch phase-par-phase) ne change pas. Le temps réel vit
  dans un **service async isolé** qui parle à TranscrIA par son **API de jobs**,
  jamais bolté dans les workers web sync.
- **Les 6 principes** (docs/PISTES_AMELIORATION.md) s'appliquent : paramétrable
  défaut inchangé, ossature intacte, mesuré, couvert par l'installeur,
  maintenable, UI/config FR-EN.
- **Additif, opt-in.** Aucune de ces briques n'est active par défaut.

### Les 6 principes appliqués à ce chantier

| Principe | Comment il est respecté ici |
|---|---|
| Paramétrable, défaut inchangé | Tout opt-in ; batch reste cohere/qwen3 ; `live_stt_backend` défaut `null` |
| Ossature intacte | Cœur sync/batch non modifié ; le temps réel = service async **séparé** |
| Mesuré | Latence live, taux de partiels, WER final vs référence, cas durs testés |
| Couvert par l'installeur | Service connecteur provisionné (systemd, deps) comme les runtimes STT |
| Maintenable | 1 contrat `MeetingProvider`, adaptateurs par plateforme, tests de contrat communs |
| UI/config FR-EN | Onglet réunion + config connecteurs, i18n systématique, pas d'UI morte |

## 1. Contexte technique établi (acquis de cette campagne)

- **TranscrIA est 100 % batch/sync** : Flask + gunicorn workers **sync**
  (`wsgi:app`), progression par **polling HTTP** (pas de push), **zéro**
  websocket/SSE/getUserMedia dans le code. → toute connexion temps réel exige
  un process async séparé.
- **Voxtral = moteur de STREAMING**, confirmé par la fiche officielle
  (`Voxtral-Mini-4B-Realtime-2602` : sert via **WebSocket `/v1/realtime`**,
  vLLM **nightly**, temp 0.0). Le chemin **audio.cpp streaming (SSE)** est
  prouvé chez nous : réunion 46 min couverte de bout en bout. Les chemins
  offline (per-chunk vide, whole-file OOM) et vLLM-HTTP (crash) sont le
  **mauvais usage** du modèle. → Voxtral est **la chaîne STT rapide/live**, pas
  un backend batch.
- **Deux chaînes STT** à distinguer explicitement :
  - **live/rapide** (faible latence) = Voxtral streaming (audio.cpp SSE) ;
  - **référence/finale** (précision max) = cohere/qwen3/whisperx du pipeline.
- **Visio (La Suite numérique) = LiveKit** (dépôt `suitenumerique/meet`,
  Django + React, MIT). Enregistrement + transcription (bêta) déjà présents.
- **Zoom RTMS** (Realtime Media Streams) : audio PCM 16 k **par participant** +
  events + timestamps, sans bot visible — nécessite OAuth/scopes/crédits.
- **Teams** : Graph (post-réunion VTT/MP4) d'abord ; bot média temps réel
  déconseillé par Microsoft (dernier recours).

## 2. Architecture cible

```
   Plateforme (Visio/Zoom/Teams)
            │  (webhook / SDK / websocket)
            ▼
   ┌─────────────────────────────┐   SERVICE CONNECTEUR (async, isolé, opt-in)
   │  MeetingProvider (adaptateur)│   FastAPI/uvicorn OU worker asyncio
   │  → événements normalisés     │   process séparé du web sync
   └─────────────┬───────────────┘
                 │ AudioFrame / TranscriptPartial / RecordingAvailable
                 ▼
   ┌─────────────────────────────┐
   │ Session temps réel TranscrIA │  affichage provisoire + audio horodaté
   │  (chaîne STT live = Voxtral) │  segments provenance = final_live
   └─────────────┬───────────────┘
                 │  fin de réunion
                 ▼
   ┌─────────────────────────────┐
   │  API de jobs TranscrIA       │  ← le cœur EXISTANT, inchangé
   │  pipeline complet (batch)    │  segments provenance = canonical
   └─────────────────────────────┘
```

**Contrat unique** : chaque plateforme n'est qu'un adaptateur `MeetingProvider`.
Les événements sont normalisés une fois → tests de contrat identiques pour tous.

## 3. Phase 0 — Les 3 coutures (dans le cœur, cheap, EN PREMIER)

Additives, défaut inchangé, releasables en un petit lot. Elles ne font rien de
visible mais rendent tout le reste « appelable ».

### Couture 1 — Provenance du segment
- **Quoi** : un champ `provenance` sur le segment, enum
  `canonical | partial | provisional | final_live`. Aujourd'hui seul
  `canonical` est produit ; les autres sont réservés au live.
- **Où** : structure Segment (`transcria/stt/`), sérialisation des segments
  (artefacts `metadata/` du job), modèle de job si persistance.
- **Pourquoi tôt** : cher à rétro-installer ; poser dès maintenant que « le
  texte a une provenance » fait que le live *remplira* les autres états sans
  toucher au modèle.
- **Machine à états** (qui pose quoi) :
  - `partial` — texte instable du STT live (peut changer au prochain paquet) →
    posé par la chaîne live, jamais persisté comme livrable.
  - `provisional` — segment stabilisé par le STT live (ne bougera plus en
    direct). **Mécanisme selon le backend** (ADR-001 D6) : le marqueur natif du
    moteur quand il expose partial/final (ex. Voxtral SSE) ; **local-agreement**
    (cf. `ufal/whisper_streaming`, §8) SEULEMENT pour un backend à fenêtres
    glissantes — jamais une double passe artificielle sur un moteur au streaming natif.
  - `final_live` — segment final du **moteur temps réel** (fin de tour).
  - `canonical` — segment de la **révision documentaire de référence** (pipeline TranscrIA).
  - Transition clé (ADR-001 D5) : à la fin du batch, la révision `canonical` devient
    la révision **active/affichée par défaut** ; la révision `live` est **conservée**
    (audit, diagnostic, comparaison), PAS écrasée en place. Le direct était un suivi,
    le canonical est la référence — « le document de référence est maintenant disponible ».
- **Affichage** : `partial`/`provisional` en gris (suivi), `final_live` figé,
  `canonical` = le document officiel (avec « afficher la version du direct »). Un
  lecteur voit toujours quel niveau de confiance il lit.
- **DoD** : champ additif, défaut `canonical`, sérialisé (artefacts + éventuelle
  colonne), testé ; **zéro** changement de sortie sur les jobs batch existants
  (golden inchangés).

### Couture 2 — Abstraction source audio
- **Quoi** : interface « une source produit un WAV 16 k canonique (+
  éventuellement des pistes par participant + une identité) et le remet au
  pipeline ». Aujourd'hui : `file`. Demain : `mic`, `meeting`.
- **Où** : chemin d'ingestion (`web/processing_api.py` → création de job).
- **Pourquoi tôt** : le connecteur meeting et le micro s'y brancheront sans
  toucher le pipeline.
- **Esquisse d'interface** (synchrone, côté cœur — un connecteur async peut la
  piloter depuis l'extérieur) :
  ```
  class AudioSource(Protocol):
      def materialize(self, job) -> Path: ...          # WAV 16 k mono canonique
      def participant_tracks(self, job) -> list | None: ...  # pistes + identité (opt)
      def kind(self) -> str: ...                        # "file" | "mic" | "meeting"
  ```
  L'implémentation `FileSource` encapsule le `_materialize_wav` existant ; le
  pipeline consomme `AudioSource`, pas le chemin de fichier en dur.
- **DoD** : `file` refactoré derrière l'interface, **comportement identique**
  (E2E 16/16 inchangé) ; interface synchrone simple ; test de contrat
  `AudioSource` (au moins `FileSource`).

### Couture 3 — Nommer les 2 chaînes STT
- **Quoi** : `models.stt_backend` = référence/finale (existe) ; ajouter
  `models.live_stt_backend` = rapide/live (= `voxtralrt` streaming).
- **Où** : `config/loader.py` défauts + `config/config_schema.py` +
  `config_form.py` (UI) + i18n.
- **Pourquoi tôt** : nommer la chaîne rapide lui donne une place ; pas de
  câblage live encore.
- **Résolution** : `live_stt_backend` suit la MÊME règle de validation que
  `stt_backend`/`summary_stt_backend` (natif du registre, ou servi routé avec
  url — `config_schema._check_*`). Défaut `null` = pas de chaîne live. Piège
  i18n récurrent : forcer les msgstr FR/EN explicitement, jamais de défuzzage
  aveugle (cf. [[refactoring-qualite-avancement]]).
- **DoD** : clé opt-in, défaut null (= pas de chaîne live), classée dans
  `config_classification.yaml`, validée, UI (`config_form.py`) + i18n FR/EN,
  doctor si un moteur live servi est déclaré sans runtime.

## 4. Phase A0 — Contrat providers (par capacités) + service async isolé

> **Nommage (ADR-001, revue #2 point 4)** : cette phase-CONTRAT est **A0**, distincte
> de **A1** (Visio post-réunion). Le tableau §7 fait foi. « Phase 1 » n'est plus utilisé
> (collision de numérotation). `A` = artefact/ingestion, `L` = live, `R` = recherche.

### Le contrat — interfaces par capacités (ADR-001 D3)

Pas de `MeetingProvider` monolithique (rejeté : imposait `stream_audio` à Teams-post).
De **petites interfaces** + un manifeste `ProviderCapabilities` ; un provider ne déclare
que ce qu'il sait faire. Le **code du Protocol est un livrable de A0**, pas du plan :

- `ArtifactProvider` — `fetch_artifacts(occurrence) -> [RemoteArtifact]`
- `ParticipantProvider` — `fetch_participants(occurrence) -> [ExternalParticipant]`
- `PlatformTranscriptProvider` — `fetch_platform_transcripts(occurrence) -> [RemoteTranscript]`
- `LiveMediaProvider` — `open_session(occurrence) -> LiveMediaSession`
- `ProviderCapabilities(post_meeting_recording, post_meeting_transcript, live_audio,
  live_transcript, participant_identity, separate_tracks)`

### Événements — plan de contrôle vs plan de données (ADR-001 D4)

- **Plan de contrôle** (petits messages DURABLES) : `MeetingStarted, MeetingEnded,
  ParticipantJoined, ParticipantLeft, ParticipantRenamed, RecordingAvailable,
  PlatformTranscriptAvailable, LiveStreamInterrupted, LiveStreamRecovered`. Enveloppe :
  `event_id, schema_version, provider, provider_account_id, external_occurrence_id,
  correlation_id, occurred_at, received_at, deduplication_key, payload`.
- **Plan de données** (flux LIVE, jamais dans le bus durable) : `AudioFrame,
  TranscriptPartial, TranscriptFinal` — circulent dans la session média (WS/WebRTC/SDK).

### AudioFrame (champs minimum)
`provider, provider_account_id, external_occurrence_id, participant_id,
participant_display_name, track_id, sequence_number, media_timestamp_ms,
wall_clock_timestamp, duration_ms, encoding, sample_rate_hz, channels, sample_count,
payload`. (Plus de `start_timestamp` ambigu : position réunion vs UTC explicitées.)

### Le pont vers TranscrIA
Le service connecteur crée un job et pousse des artefacts via l'**API de jobs
existante** (jamais d'accès direct au pipeline). Mécanisme concret :
- **Auth** : jeton `tia_` (Bearer) suffit pour le prototype ; un **service account
  scopé** (rotation/révocation/périmètre org) est le bon état pour un connecteur
  permanent (ADR-001, différé).
- Il appelle les **routes ⭐ stables** (upload/process/status/download-*).
- **Idempotence composite** (ADR-001 D2) : enregistrement `MeetingImport` +
  **contrainte UNIQUE en base** sur
  `provider + provider_account/tenant + external_occurrence_id + external_artifact_id`
  (à défaut d'artifact_id : `… + artifact_type + artifact_variant + checksum`). Un webhook
  rejoué — ou deux webhooks simultanés — ne crée pas un second job.
- Le service connecteur peut vivre sur une **autre machine** que le nœud GPU
  (il ne fait que capter + router ; le calcul reste côté TranscrIA).

### Déploiement
Process séparé (systemd unit dédiée), opt-in. Ne partage pas le worker gunicorn
sync. Le cœur `sync` n'importe jamais ce service. Deux patrons matures à
reprendre (§8) : le **worker `livekit/agents`** (pour les connecteurs LiveKit)
et le **serveur FastAPI WebSocket de `WhisperLiveKit`** (pour le micro
navigateur) — plutôt que d'inventer le squelette async.

### DoD Phase 1
Contrat `MeetingProvider` + événements + `AudioFrame` figés et testés (test de
contrat abstrait) ; un **provider factice** (stub) prouve le pont vers l'API de
jobs (créer un job, pousser un artefact) sans plateforme réelle ; service
async démarrable/arrêtable ; import-linter confirme que le cœur sync n'importe
pas le service async.

## 5. Les connecteurs par plateforme

> Descriptions détaillées ci-dessous ; le **séquencement de référence est le §7**
> (révisé : keystone façade d'abord, post-réunion officiel des 4).

### Visio post-réunion (premier jalon) — effort M
- Visio finit l'enregistrement → **webhook « recording ready »** → le service
  crée un job TranscrIA → pipeline complet → résultat renvoyé (La Suite Docs).
- Zéro live. Prouve le partenariat + le chemin `fetch_final_artifacts`.
- Chaîne actuelle de Visio (d'après l'analyse du dépôt, **à revérifier au
  moment de l'implémentation**) : LiveKit **Egress** → enregistrement audio →
  stockage MinIO/S3 → Celery → WhisperX → doc La Suite. Verrou connu :
  enregistrement et transcription **pas simultanés**, seul
  `RoomCompositeEgress` officiellement supporté (pistes mélangées) — alors que
  LiveKit sait faire de l'egress **par piste/participant**.
- **Contribution amont possible** (souveraineté, contribuer plutôt que
  contourner) : ajouter à Visio un mode `Track/Participant Egress` →
  WebSocket/objet → connecteur TranscrIA, pour garder audio + identité +
  timestamps **par participant**. C'est là que TranscrIA apporte le plus :
  relier l'identité connue en réunion à la transcription finale de qualité.
- **DoD Phase 2** : webhook signé/vérifié + idempotent (doublons tolérés) ; un
  enregistrement Visio réel (ou rejoué) crée un job et produit les livrables
  complets ; échec plateforme = job en erreur explicite, jamais de perte
  silencieuse.

### Visio live (LiveKit realtime) — effort L
- Rejoindre la salle LiveKit côté serveur, s'abonner aux **pistes par
  participant** → AudioFrames → chaîne STT live (Voxtral streaming) →
  transcript `final_live` + identité LiveKit.
- **Diarisation en partie inutile** : quand une piste = un participant connu,
  on aligne les segments sur cette identité sans diariser globalement. MAIS
  garder la diarisation par piste (règle « piste ≠ personne », §6).
- **Fondation = `livekit/agents`** (§8) : un worker agent rejoint la salle. Son
  exemple **`multi-user-transcriber.py`** fait déjà une session STT **par
  participant** (STT pluggable — Deepgram/Kyutai en démo) → on **remplace le
  plugin STT par notre Voxtral streaming**. Correction : ce fichier n'est PAS
  propre à Visio, c'est l'exemple officiel de LiveKit ; Visio (LiveKit) s'appuie
  sur la même mécanique. L'identité participant est **connue avant** la
  transcription → alignement direct, pas de diarisation globale.
- Fin de réunion → pipeline offline remplace en `canonical` (couture 1).
- Utilise couture 1 (provenance) + 2 (source `meeting`) + 3 (chaîne live).
- **DoD Phase 3** : rejoint une salle LiveKit de test, produit du texte
  provisoire par participant identifié en direct, puis un `canonical` complet en
  fin de réunion ; latence live mesurée ; cas durs (§6) couverts par des tests
  de contrat ; VRAM du moteur live gérée (reclaim, cf. machine serrée).

### Zoom post-réunion (A2, Cloud Recording API) — effort M
- Événement « enregistrement disponible » → OAuth → récupération des fichiers →
  artefacts TranscrIA → job batch. Périmètre plus simple (pas de flux live), bon
  premier connecteur OAuth propriétaire. **Distinct** du live RTMS ci-dessous — le
  « ou » historique est supprimé (revue #2 point 12).

### Zoom live (L2, RTMS) — effort L
- RTMS fournit PCM L16 mono 16 k **par participant** (et/ou fusionné) + events +
  transcription attribuée, **sans bot visible** ; format par défaut avec id, nom,
  timestamp. L'essentiel n'est pas l'audio (réutilisé) mais : OAuth/scopes Zoom,
  webhooks `rtms_started`/`rtms_stopped`, autorisations admin/hôte, **crédits Zoom
  Developer Pack** (à porter dans une matrice de coûts, pas seulement les risques),
  information visible des participants.
- **Fork de départ** : `zoom/rtms-samples` (§8) → `AudioFrame` normalisé sur la
  passerelle live (L0). RTMS = WebSocket standard, pas besoin du SDK C++.

### Teams post-réunion (Graph) — effort M
- Notification Graph → récup VTT/MP4 + métadonnées (+ attribution des locuteurs
  si le tenant l'active) → job TranscrIA. Comme Visio post-réunion, côté MS.
  Accès tenant-wide ou par réunion via **Resource-Specific Consent**. Supprime
  déjà l'upload manuel → gros de la valeur sans temps réel.

### Meet post-réunion (Google Meet REST API v2) — effort M
- **API officielle** (découverte tardive) : après la réunion, les artefacts
  (enregistrement + transcript) sont déposés dans le **Google Drive de
  l'organisateur**. `conferenceRecords.transcripts` / `.recordings` donnent les
  refs ; download via **Drive API** (poller jusqu'à `STATE=FILE_GENERATED`).
- Même forme que Teams/Visio post-réunion → job TranscrIA. **Zéro bot.** Requiert
  Google Workspace + enregistrement activé + OAuth (scopes Meet + Drive).

### Meet-live (R1, recherche) & Teams-RTM (C1, dernier recours) — effort L, conditionnel
- **Meet-live** : Google fournit une **API live officielle — Meet Media API**
  (audio/vidéo/participants temps réel), en **Developer Preview** : inscription du
  projet GCP + du principal OAuth + de **tous les participants** au programme, et
  **plafond de flux virtuels** (au-delà, Meet ne transmet que les pistes jugées les
  plus pertinentes — ne pas présumer recevoir toutes les pistes en grande réunion).
  → **phase de RECHERCHE R1, sans engagement de prod**, AVANT tout bot navigateur.
  Critères : inscription/consentement praticables, couverture audio mesurée selon le
  nombre de participants, pas de trou au changement de locuteur, repli post-réunion
  toujours dispo. Le bot Vexa (`capture-bridge.ts`) reste une référence expérimentale.
- **Teams-RTM** : bot média temps réel .NET/Windows, gestion médias complexe.
  Microsoft **déconseille** (préfère Graph/Copilot). À ne lancer qu'avec un
  besoin contractuel que Graph ne couvre pas.

## UI — interface utilisateur (FR/EN, opt-in, pas d'UI morte)

Trois surfaces, toutes i18n FR/EN, toutes **n'apparaissant que si la brique est
active** (aucune UI morte). Le reste de l'UI (résultat, validation, Word, éditeur
SRT) **ne change pas** — c'est le moat.

### Admin — configuration des connecteurs (page `/admin/config`, patron existant)
- **Façade STT** : un flag d'activation (rien à saisir).
- **Moteur live** : sélecteur `live_stt_backend` (Nemotron-streaming / Kyutai /
  Voxtral) + URL du serveur, secret masqué (comme SSO/LDAP).
- **Visio** : URL de notre façade à coller côté Visio (doc) ; (live) URL/clé du worker.
- **Zoom** : client id/secret + secret webhook + statut RTMS ; bouton « tester ».
- **Teams** : app registration (client id, certificat) + état d'abonnement (actif/expiré).
- **Meet** : OAuth Google Workspace (scopes Meet+Drive) + statut.
- Secrets masqués + **audit de toute connexion** (réutilise le chantier identité).

### Live — l'expérience temps réel (panneau « Réunion en direct »)
- N'apparaît que si un connecteur live tourne. **Captions par participant**
  (identité) ; `partial`/`provisional` en **gris**, `final_live` figé.
- Bandeau visible : « le direct est un suivi ; le compte-rendu de référence sera
  produit à la fin » (la règle d'or à l'écran).
- Fin de réunion → bascule pipeline → `canonical` remplace le direct (le document
  officiel « ne bouge pas tout seul »).

### Micro direct (source `mic`)
- Bouton **« Enregistrer au micro »** (record-then-transcribe) sur la création de job.
- (Optionnel) **live rolling** : captions défilantes (fork WhisperLiveKit).

### Job « réunion » vs job « upload »
- Un job de connecteur porte un badge **source** (Visio/Zoom/Teams/Meet/Micro) +
  `external_meeting_id` + participants. Tout l'aval (livrables) = **UI existante**.

## 6. Règles transverses

- **Conserver les 2 chaînes STT** : ne jamais remplacer la finale par le live.
- **Piste ≠ personne** : une piste « participant » peut être un micro de salle,
  un téléphone, plusieurs personnes → garder la **diarisation par piste**
  activable ; l'identité de piste n'est qu'un indice.
- **Cas durs à tester** (contrat, identiques pour tous) : réordonnancement de
  paquets, doublons de webhooks, perte/reprise de flux, reconnexion, changement
  de nom, parole simultanée, mute/unmute, micro partagé, révision d'un segment
  partiel, arrêt brutal, reprise idempotente.
- **Pas d'UI morte** : un onglet « réunion » n'apparaît que quand un connecteur
  existe. i18n FR/EN systématique.
- **Lire, pas seulement scorer** (leçon du bench Voxtral) : les métriques
  automatiques mentent (le compteur non-latin a raté une dérive russe ; le
  nombre de segments a caché des sauts silencieux). Toute qualité de
  transcription live/finale se **valide à la lecture humaine**, pas au compteur.
- **Signaux d'honnêteté** systématiques sur le live : fenêtres quasi-vides,
  sauts de couverture (écart entre segments consécutifs), boucles de répétition,
  dérive non-latine — exposés, jamais avalés silencieusement.

## 7. Séquencement & jalons

Nomenclature par flux (ADR-001, revue #2 point 4) : `A` = artefact/ingestion,
`L` = live, `R` = recherche, `C` = contractuel. Fini les « Phase 1 » ambigus.

| ID | Quoi | Dépend | Effort |
|---|---|---|---|
| **0** ✅ | 3 coutures (provenance / source audio / 2 chaînes STT) | — | S |
| **K** ✅ | Façade STT `/v1/audio/transcriptions` + `/v1/audio/ingest` fichier + gardes taille/durée | couture 2/3 | M |
| **0-bis** ✅ | Micro direct (record-then-transcribe → upload) | K | S |
| **A0** | Contrat providers **par capacités** + manifeste + événements contrôle/données + `MeetingImport` (idempotence composite) + service async isolé | K + coutures | M |
| **A1** | Visio post-réunion — **adaptateur** au contrat Visio (tâche + métadonnées → réunion), PAS un swap d'URL (ADR-001 D8) | A0 | M→sem. |
| **A2** | Zoom post-réunion — Cloud Recording API (OAuth) → `/ingest` | A0 | M→sem. |
| **A3** | Teams post-réunion — Graph, webhook chiffré → fetch MP4/VTT (API facturées) | A0 | sem. |
| **A4** | Meet post-réunion — REST API v2 + Drive → job | A0 | M |
| **L0** | Passerelle live générique (plan de données audio séparé, D4) | A0 | L |
| **L1** | Visio live — adapter le worker `multi_user_transcriber.py` sur la passerelle | L0 | L |
| **L2** | Zoom live (RTMS) sur la passerelle | L0 | L→sem. |
| **R1** | Meet live — **recherche Meet Media API** (preview, plafond de flux) ; bot = repli | L0 | rech. |
| **C1** | Teams RTM — **dernier recours** (MS déconseille) | L0 | cond. |

- **Ordre** : 0 → K → 0-bis (✅ livrés) → **A0 contrat** → A1 Visio-post → A2 Zoom-post /
  A3 Teams / A4 Meet-post → L0 passerelle → L1 Visio-live → L2 Zoom-RTMS → (R1 Meet /
  C1 Teams-RTM si besoin). **Post-réunion d'abord sur les 4, live ensuite.**
- **A0 est le prérequis dur des connecteurs** : le contrat par capacités et
  l'idempotence composite doivent être figés AVANT A1 (sinon on recode à chaque plateforme).
- **Efforts = ordre de grandeur POC, pas prod.** Un connecteur SaaS durci (OAuth,
  webhooks, reconnexion, renouvellement d'abonnement, crédits, revue éditeur) = plusieurs
  semaines (ADR-001 D8).
- **Micro direct = première classe** (source `mic`) : présentiel / dictée / solo,
  indépendant des plateformes.
- **Règle de push inchangée** : chaque phase = suite verte + E2E réel 16/16 avant
  `main` (cf. [[refactoring-qualite-avancement]]).

## Installation, déploiement & doctor (opt-in)

Le cœur « upload + pipeline » **n'installe rien de neuf**. Chaque brique est
provisionnée à part, opt-in, idempotente — patron des runtimes STT existants
(`installer.cli`, phases, doctor).

| Brique | Installation | Config | Doctor |
|---|---|---|---|
| **Façade STT** | flag (endpoint dans l'app web existante) | `live.facade.enabled` + `live.facade.max_sync_audio_mb` (plafond sync, défaut 25) | `GET /health` façade |
| **Moteur live Nemotron-streaming** | **déjà là** (phase audio.cpp, famille `nemotron_asr`) | `live_stt_backend` + URL | runtime + GGUF présents |
| **Moteur live Kyutai** | image Docker `meet-kyutai-moshi-stt` (ou build `moshi-server`) — ⚠ **sm_120 à valider** | URL WS + clé | `/health` WS |
| **Service connecteur async** | systemd unit dédiée (ou conteneur), opt-in | env par connecteur | process vivant + creds valides |
| **Deps par plateforme** | Visio : `livekit-agents` + plugin Kyutai · Zoom : `pip install rtms` · Teams : `msal`+`cryptography` · Meet : `google-api-python-client` | — | import-check |

- **Garde-fou** : un déploiement qui ne veut PAS de temps réel ne voit **rien**
  (respecte l'axe « déploiement faible » du tableau moat).
- **Doctor** : un check par brique active (façade, moteur live, connecteur, creds
  plateforme) — sur le modèle des checks `qwen3asr`/`nemotron` existants.
- **Config** : nouvelles clés classées dans `config_classification.yaml`, validées
  au schéma, exposées à l'UI + i18n (patron habituel).

## 8. Briques à réutiliser

### Interne (TranscrIA)

- **audio.cpp streaming (SSE)** — chaîne STT live prouvée (46 min couverts) ;
  léger, GGUF, déjà intégré (`scripts/launch_stt_voxtral.sh`, backend
  `voxtralrt`, `models.summary_stt_backend`/futur `live_stt_backend`).
- **exp-STT** (`/root/Voxtral-WebUI`) — patrons de référence. ⚠ record-then-
  transcribe + chunké, **PAS** de live rolling (le WebSocket Voxtral y avait été
  jugé « trop complexe » et abandonné). Réutilisable : `_pcm16_to_wav_bytes`,
  segmentation VAD, `gr.Microphone` (record-then-transcribe), le client HTTP.
- **API de jobs TranscrIA** — création de job + push d'artefacts = le pont du
  service connecteur (jamais d'accès direct au pipeline).
- **Diarisation pyannote/sortformer** — pour diariser une piste individuelle
  non mono-locuteur (règle piste ≠ personne).
- **Contrat STT servi** (`inference.stt.backends`, `resource_node.engines`,
  `RemoteTranscriber`, `AsrClient`, superviseur de moteurs) — la chaîne live
  s'y branche.

### Externe — projets à reprendre / adapter / forker (recherche 2026)

> Licences à **revérifier** au moment d'intégrer (peuvent évoluer). Le principe :
> **forker/adapter** plutôt que réécrire, **étudier** les archis matures.

| Besoin / phase | Projet | Usage |
|---|---|---|
| **Connecteur Visio live + patron async** | [`livekit/agents`](https://github.com/livekit/agents) (Python, Apache-2.0) + **`suitenumerique/meet` → `src/agents/multi_user_transcriber.py`** (MIT, ~225 l., STT pluggable par env, déjà en prod chez Visio) | **Forker le worker Visio** ; STT pluggable → **notre moteur live** (Kyutai/Nemotron-streaming) |
| Export/enregistrement (ph. 2 + contribution Track-Egress) | [`livekit/egress`](https://github.com/livekit/egress) (Apache-2.0) | Adapter (egress par piste → connecteur) |
| Réf. concrète LiveKit+STT | [`atyenoria/livekit-whisper-transcribe`](https://github.com/atyenoria/livekit-whisper-transcribe) | Étudier |
| **Partiels stables (`partial`→`provisional`, couture 1)** | [`ufal/whisper_streaming`](https://github.com/ufal/whisper_streaming) (MIT) — politique **local-agreement** (ne fige un mot que si deux passes successives s'accordent) | **Adapter l'algorithme** au flux Voxtral |
| **Micro navigateur + WS live (ph. 0-bis + affichage)** | [`WhisperLiveKit`](https://github.com/QuentinFuxa/WhisperLiveKit) (Q. Fuxa) — backend **FastAPI WebSocket** + front **HTML/JS capture micro** + diarisation live | **Forker le front micro** + le patron serveur WS async |
| **Meet-LIVE optionnel** (pas d'API temps réel off.) | [`Vexa`](https://github.com/Vexa-ai/vexa) (Apache-2.0) — ⚠ **plateforme complète, ~80 % redondant** avec TranscrIA. Feeder confirmé (`TRANSCRIPTION_SERVICE_URL` → notre façade, WAV 16k) mais bot 0.12 « prouvé sur VM » (fragile) | **Extraire SEULEMENT** la capture Meet (`capture-bridge.ts`, par locuteur), **pas** la plateforme |
| Alt. Recall.ai open-source | [`Attendee`](https://github.com/attendee-labs/attendee) (MIT), Meet/Teams/Zoom browser-auto | Étudier (même classe que Vexa) |
| Capture **bot-free** (audio système, **client-side**) | [`Meetily`](https://github.com/Zackriya-Solutions/meetily) (MIT, Tauri/Rust, whisper.cpp/Parakeet) | Patron pour la capture onglet/système côté poste |
| **Zoom RTMS (ph. 4)** | [`zoom/rtms-samples`](https://github.com/zoom/rtms-samples) (JS/Py/Go/Java/.NET, exemples transcription) + [`zoom/rtms`](https://github.com/zoom/rtms) (bindings Py/Node/Go). RTMS = **WebSocket standard**, PCM par participant | **Forker un sample** |
| Réf. Zoom RTMS produit complet | Arlo (Zoom Apps RTMS : résumés, actions) | Étudier |
| **Teams post-réunion (Graph)** | Microsoft Graph : `getAllTranscripts`/`getAllRecordings`, change notifications chiffrées + [`microsoftgraph/nodejs-webhooks-sample`](https://github.com/microsoftgraph/nodejs-webhooks-sample) (MIT, ~80 % du webhook+crypto RSA→AES) | **Forker le sample** → fetch MP4/VTT → job. ⚠ consentement admin, réunions calendaires |
| **Meet post-réunion (officiel)** | [Google Meet REST API v2](https://developers.google.com/workspace/meet/api) — `conferenceRecords.transcripts/recordings` + Drive API (artefacts du Drive de l'organisateur) | Utiliser (poll `FILE_GENERATED` → job) |
| **Serveur STT live Kyutai** | [`suitenumerique/meet-kyutai-moshi-stt`](https://github.com/suitenumerique/meet-kyutai-moshi-stt) (MIT, Docker `moshi-server`) — WS `/api/asr-streaming`, `stt-1b-en_fr`, 0,5 s, batché 64. Client = `livekit-plugins-kyutai-lasuite` | **Réutiliser l'image** (⚠ build candle sm_120 à valider) |
| Visio (source) | [`suitenumerique/meet`](https://github.com/suitenumerique/meet) (LiveKit + Django + React, MIT) | Lire pour brancher |

### Décision : QUOI forker exactement (cibles vérifiées dans le code, 2026)

Axe déterminant : **API officielle / natif** (stable, souverain) **vs automation
navigateur** (fragile, CGU). On privilégie l'officiel ; le bot ne sert qu'au
**Meet-live optionnel**. Cibles **lues dans le code source** :

| Voie | Ce qu'on forke / config (précis) | Effort | Robustesse |
|---|---|---|---|
| **Façade STT (keystone)** | endpoint TranscrIA `POST /v1/audio/transcriptions` (WAV→verbose_json) + `/v1/audio/ingest` fichier | fondation ✅ livré | — |
| **Visio post-réunion** | **adaptateur** au contrat Visio (tâche `/api/v1/tasks/` + métadonnées → réunion TranscrIA ; accès MinIO OU callback/URL présignée amont) — **PAS** un swap d'`SUMMARY_SERVICE_ENDPOINT` (Visio → WhisperX, contrat ≠ OpenAI) | M (POC), semaines (prod) | 🟢 natif |
| **Visio live** | adapter le **worker Visio** `suitenumerique/meet` → `src/agents/multi_user_transcriber.py` (bâti sur LiveKit Agents, STT pluggable par env → nous) ; ⚠ egress = `RoomCompositeEgress` seul aujourd'hui, Track Egress = contribution amont | faible-moyen | 🟢 natif |
| **Zoom post-réunion** | Cloud Recording API (OAuth) → fetch → job | M | 🟢 officiel |
| **Zoom live** | binding `zoom/rtms` (`on_audio_data`, `data_opt=2` par participant, PCM L16 16k) → passerelle ; crédits Developer Pack | M (POC), semaines (prod) | 🟢 officiel |
| **Teams post-réunion** | Graph (`microsoftgraph/nodejs-webhooks-sample` ~80 % webhook+crypto) → fetch MP4/VTT → job ; API facturées à l'usage | ~1-1,5 sem+ | 🟢 officiel |
| **Meet post-réunion** | **Google Meet REST API v2** (`conferenceRecords.transcripts/recordings`) + Drive API → job | moyen | 🟢 officiel |
| **Meet live** *(R1 recherche)* | **Meet Media API officielle** (Developer Preview, plafond de flux) ; Vexa `capture-bridge.ts` = repli expérimental | recherche | 🔬 preview |
| **Micro direct** | ✅ **livré** (record-then-transcribe → upload) ; live rolling = fork `WhisperLiveKit` (à venir) | faible | 🟢 |
| **Moteur STT live** | Nemotron-streaming 0.6B via audio.cpp (zéro stack) **ou** Kyutai `stt-1b-en_fr` via moshi-server (image `meet-kyutai-moshi-stt`, MIT) | moyen | 🟢 |

**Confirmé par le code** : Vexa (`TRANSCRIPTION_SERVICE_URL`) ET Visio
(`SUMMARY_SERVICE_ENDPOINT`) attendent tous deux un **STT externe par URL** → la
façade les branche par config. Mais **~80 % de Vexa** (transcription, stockage,
gateway, UI) est **redondant** avec TranscrIA → on n'en prend que le bout
capture-Meet, et **seulement si besoin** (Meet post-réunion étant officiel).

## 9. Décisions actées & points encore ouverts

### Actées (validées par l'analyse de code des 4 plateformes)

- **Frontière d'ingestion à 3 voies** (ADR-001 D1) : la façade OpenAI Audio
  (`/v1/audio/transcriptions`, ✅ livrée) est l'adaptateur des clients STT, PAS la
  frontière universelle ; les plateformes post-réunion passent par l'ingestion
  d'artefacts + l'API de jobs, le live par la passerelle async.
- **Post-réunion officiel sur les 4** (Visio via adaptateur, Zoom Recording API,
  Teams Graph, Meet REST API) → **zéro browser-bot** dans le cœur de la feuille de route.
- **Live officiel** : Visio (worker LiveKit natif) + Zoom (RTMS) — par participant,
  souverain, qualité supérieure. **Meet-live** = Meet Media API officielle (preview,
  recherche R1) avant tout bot.
- **Micro direct ✅ LIVRÉ, PREMIÈRE CLASSE** (source `mic`, couture 2) :
  record-then-transcribe → upload. Live rolling (fork WhisperLiveKit) à venir.
- **Vexa rétrogradé** : référence + **extrait capture-Meet** pour le Meet-live
  *optionnel* seulement — on n'adopte PAS la plateforme (~80 % redondant).
- **Feeder = audio brut préféré** ; la **transcription de la plateforme est un
  artefact AUXILIAIRE** (aperçu/identité/repli/cross-check), jamais promue canonique
  automatiquement (ADR-001 D7). On ne réécrit aucun bot.
- **Capture opt-in/isolée** — déploiement du cœur inchangé.

### Encore ouverts (à trancher au moment)

- **Moteur STT live** : Nemotron-streaming 0.6B (audio.cpp, zéro stack) vs Kyutai
  `stt-1b-en_fr` (moshi-server) — **bencher EN MODE STREAMING** sur réunions
  réelles avant de figer (on n'a testé Nemotron qu'en batch). Voxtral = repli.
- **Kyutai sur RTX 5090 (sm_120)** : build candle/flash-attn à vérifier.
- Framework async du service connecteur (FastAPI — patron WhisperLiveKit — vs
  worker `livekit-agents` selon le connecteur).
- **`local-agreement`** (whisper_streaming) porté sur le moteur live vs partiels bruts.
- Priorité plateformes : Visio d'abord (souverain, partenariat La Suite).
- Config plateforme (**le vrai coût, hors code, côté client**) : Zoom (activation
  RTMS, Developer Pack, scopes) ; Teams (consentement admin, application access
  policy, réglages transcript/speaker attribution) ; Meet (Workspace, Drive).
- Modèle de restitution (La Suite Docs vs TranscrIA vs les deux).

## 10. Risques & mitigations

| Risque | Mitigation |
|---|---|
| L'async contamine le cœur sync | Service **séparé** ; contrat import-linter « le cœur n'importe pas le connecteur » |
| Le live devient le livrable officiel | Machine à états de provenance ; seul `canonical` est la référence |
| Latence live insuffisante (RTF ~1,0 sur Voxtral streaming) | Mesurer tôt ; le live est un *suivi*, pas le verbatim ; fenêtrer/paralléliser si besoin |
| VRAM (moteur live + 35B) sur machine serrée | Reclaim STT↔LLM existant ; le connecteur peut être sur une autre machine |
| API plateforme non documentée/mouvante (Visio bêta) | Construire sur le socle LiveKit + interfaces explicites, pas sur une API de prod instable |
| Dépendance propriétaire (Zoom/Teams) | Visio (souverain, open) d'abord ; Zoom/Teams additifs |
| Qualité live surestimée par les métriques | Règle « lire, pas scorer » (§6) |

## 11. Stratégie de test

- **Tests de contrat identiques** pour tous les providers : mêmes événements
  normalisés produits, mêmes cas durs (§6) — un provider factice sert de
  référence.
- **Provenance** : golden batch inchangés (couture 1 additive) ; transitions
  `final_live → canonical` testées sur une session simulée.
- **Source audio** : contrat `AudioSource`, `FileSource` prouve l'iso-comportement
  (E2E 16/16 inchangé).
- **Live** : session LiveKit de test rejouable (pas de vraie réunion privée dans
  la CI) ; lecture humaine des sorties (jamais que le compteur).
- **Sécurité** : webhooks signés/vérifiés, jetons d'API scannés, jamais de
  secret plateforme dans le dépôt (cf. fichiers interdits, [[jobs-reels-bench-prive]]).

### Automatisable en CI vs validation réelle (« pas toujours possible »)
- **✅ CI** : la **façade STT** (WAV → verbose_json), les 3 coutures (provenance
  goldens, contrat `AudioSource`), chaque connecteur avec un **provider factice**
  (événements normalisés simulés), le **déchiffrement Teams** (vecteurs de test
  RSA→AES), le parsing **RTMS/VTT**, la conversion PCM→WAV.
- **🖐 Manuel / réel (PAS en CI)** : rejoindre une **vraie** réunion
  Visio/Zoom/Teams/Meet (comptes, OAuth, activation plateforme côté tenant), la
  **capture navigateur Meet** (fragile), la **latence live réelle**, le build
  **Kyutai sm_120**. → checklist de validation manuelle par plateforme, sur une
  autre machine/tenant (comme convenu pour les IdP réels). **On l'assume : ces
  tests-là ne sont pas toujours possibles en automatique** — on les documente et
  on les rejoue à la main avant chaque « go » plateforme.

### Tests par capacité (une fois les interfaces segrégées — ADR-001 D3)
Tests **communs** à tout adaptateur : enveloppe fournisseur, `correlation_id`, gestion
des secrets, erreurs explicites, retry idempotent, absence de perte silencieuse, audit,
isolation tenant. Puis par contrat : `ArtifactProvider` (artefact retardé, doublon,
nouvel ETag/version, checksum incorrect, téléchargement interrompu, reprise) ;
`LiveMediaProvider` (désordre/trous de séquence, reconnexion, mute, fin brutale,
changement de piste, backpressure, timestamps) ; `ParticipantProvider` (reconnexion,
anonyme, renommage, multi-appareils, micro de salle). Le provider factice existe en
**plusieurs variantes de capacités**.

## Mesure — seuils GO & points de mesure

Rendent « Bon » testable (valeurs **provisoires**, à recaler après prototypes). Chaque
métrique DOIT définir son point de mesure, sinon un connecteur passe à 99 % sur 10
réunions et est déclaré prêt trop tôt.

**Post-réunion** : ≥ 99 % des réunions éligibles importées sans intervention · 0 doublon
visible · 0 perte silencieuse · p95 « artefact dispo → job créé » < 5 min · reprise sans perte.
**Live** : p95 premier partiel < 2,5 s · p95 final live < 6 s · couverture audio > 99,5 % ·
reconnexion < 10 s · aucune perte silencieuse > 2 s.
**Exploitation** : 100 % des webhooks traçables (`correlation_id`) · 100 % des artefacts
avec checksum · 100 % des sessions à état final explicite · alertes abonnement OAuth/Graph expirant.

**Points de mesure** (formules) :
- *latence premier partiel* = heure de rendu UI − `wall_clock_timestamp` du 1ᵉʳ échantillon couvert ;
- *couverture audio* = durée non-muette couverte par ≥ 1 segment / durée non-muette attendue ;
- *artefact → job* = création effective du job − émission de l'événement fournisseur ;
- *0 doublon* = une occurrence externe + un artefact externe ⇒ un seul import logique + un seul job actif.
- **Périmètre** : p95 **par plateforme**, **par taille de réunion**, **avec et sans reconnexion** ;
  fenêtre d'observation minimale + nombre minimal de réunions **avant** de déclarer un GO.

## Gouvernance de la capture (transversale — ADR-001 D10)

Chapitre **transversal** (pas répété par connecteur). Chaque connecteur DOIT déclarer :
- qui peut **activer** la capture (org/admin) ; comment les participants sont **informés** ;
- quelle organisation est **propriétaire** des artefacts ; **où** sont stockées les données ;
- **rétention** de l'original / du live / du canonical ; **suppression** propagée ou non au fournisseur ;
- comportement si l'enregistrement est **désactivé** côté plateforme ;
- comportement si un participant **refuse/interrompt** la capture ;
- **journal d'audit** consultable ; **données envoyées** à un éventuel STT distant.

Cohérent avec les signaux RGPD/PSSI déjà présents (voix enregistrées, lexiques,
audits sans termes en clair). Un connecteur sans cette déclaration ne passe pas le « go ».

## 12. Glossaire

- **Chaîne STT live/rapide** : moteur faible latence (Voxtral streaming) pour le
  suivi en direct.
- **Chaîne STT référence/finale** : moteur du pipeline (cohere/qwen3) pour le
  verbatim de référence.
- **Provenance** : niveau de confiance d'un segment (`partial` → `provisional`
  → `final_live` → `canonical`).
- **MeetingProvider** : adaptateur d'une plateforme vers les événements
  normalisés.
- **AudioFrame** : unité audio normalisée par participant (voir §4).
- **Egress (LiveKit)** : export d'un enregistrement (composite ou par piste).
- **RTMS (Zoom)** : Realtime Media Streams — audio/events par participant.

## 13. Checklist « rien oublié »

- [ ] **Stratégie** : forker la capture, ne RIEN réécrire ; effort sur le feeder.
- [ ] **API officielle / SFU natif** privilégié ; bot-navigateur (CGU/fragile)
      seulement pour Meet, isolé.
- [ ] **Feeder = audio brut préféré** ; transcription plateforme = artefact
      auxiliaire, jamais canonique automatique (ADR-001 D7).
- [ ] **Déploiement du cœur inchangé** (capture opt-in, garde-fou du moat).
- [x] Provenance : les 4 états (couture 1) — révisions live/canonical distinctes (D5).
- [x] Source audio : `file`/`mic` derrière une interface (couture 2 ; `meeting` à venir).
- [x] 2 chaînes STT nommées + `live_stt_backend` (couture 3).
- [ ] Contrat providers **par capacités** + manifeste + événements contrôle/données (A0, D3/D4).
- [ ] Service async **isolé** + garde import-linter (A0).
- [ ] Pont via jeton `tia_` (prototype) → service account scopé (permanent) ;
      **idempotence composite + contrainte UNIQUE** (ADR-001 D2, jamais `external_meeting_id` seul).
- [ ] Piste ≠ personne (diarisation par piste conservée).
- [ ] Cas durs (réordonnancement, doublons, reconnexion, rename, overlap, arrêt).
- [ ] **Objectif calibré** : ingestion post-réunion « Très bon », live « Bon », bot universel hors périmètre.
- [ ] **Gouvernance de la capture** (ADR-001 D10, §Gouvernance) : consentement, rétention, suppression, audit.
- [x] **Keystone façade STT** (`/v1/audio/transcriptions` + `/v1/audio/ingest` fichier) —
      **LIVRÉ Phase K** (opt-in `live.facade.enabled`, jeton `tia_`, provenance
      `final_live`, formats OpenAI, gardes taille+durée). Reste A0 : fetch URL contraint
      (SSRF) + idempotence composite (contrat providers).
- [x] **Micro direct** (Phase 0-bis) — record-then-transcribe, source `mic`, E2E walkthrough.
- [ ] **Post-réunion officiel des 4** (Visio URL, Zoom Recording, Teams Graph, Meet REST API).
- [ ] **UI** : config connecteurs (admin) + panneau live (provenance grise→canonical)
      + bouton micro ; pas d'UI morte ; i18n FR/EN.
- [ ] **Installation** : phases opt-in par brique + doctor ; cœur inchangé.
- [ ] **Tests** : CI (façade/coutures/providers factices/crypto) **+** checklist
      manuelle réelle par plateforme (« pas toujours possible » assumé).
- [ ] Règle « lire, pas scorer » + signaux d'honnêteté.
- [ ] Défaut inchangé, opt-in, installeur, doctor.
- [ ] Chaque phase : suite verte + E2E 16/16 avant `main`.
- [ ] Aucun secret plateforme / contenu de réunion privé sur GitHub.

## 14. Sources / veille (recherche 2026)

- LiveKit Agents (framework worker, STT pluggable) —
  <https://docs.livekit.io/agents/> · <https://github.com/livekit/agents>
- LiveKit Egress (export/enregistrement par piste) —
  <https://github.com/livekit/egress>
- Visio (source, LiveKit+Django+React) — <https://github.com/suitenumerique/meet>
- whisper_streaming (politique local-agreement) —
  <https://github.com/ufal/whisper_streaming>
- WhisperLiveKit (micro navigateur + serveur WebSocket + diarisation live) —
  <https://github.com/QuentinFuxa/WhisperLiveKit>
- Vexa (bot navigateur self-hosté Meet/Teams, mode GPU-free/STT externe,
  REST+WS+MCP) — <https://vexa.ai> · <https://docs.vexa.ai> ·
  <https://github.com/Vexa-ai/vexa>
- Attendee (alt. Recall.ai open-source MIT, Meet/Teams/Zoom + Whisper) —
  <https://attendee.dev> · <https://github.com/attendee-labs/attendee>
- Meetily (transcription self-hostée **bot-free client-side**, MIT, Tauri/Rust) —
  <https://github.com/Zackriya-Solutions/meetily>
- Catégorie « meeting bot API » (référence commerciale Recall.ai + comparatifs
  open-source) — <https://screenapp.io/blog/recall-ai-alternative-open-source-meeting-bot>
- Zoom RTMS (WebSocket officiel, PCM par participant, binding Python MIT) —
  <https://developers.zoom.us/docs/rtms/> · <https://github.com/zoom/rtms-samples> ·
  <https://github.com/zoom/rtms>
- Teams Graph (transcripts/recordings + change notifications chiffrées) —
  <https://learn.microsoft.com/en-us/graph/teams-changenotifications-callrecording-and-calltranscript> ·
  <https://github.com/microsoftgraph/nodejs-webhooks-sample>
- **Google Meet REST API v2** (post-réunion officiel : conferenceRecords +
  Drive) — <https://developers.google.com/workspace/meet/api/guides/overview>
- **Kyutai STT** (moteur live FR+EN, moshi-server WS) —
  <https://kyutai.org/stt/> · <https://github.com/kyutai-labs/delayed-streams-modeling> ·
  <https://github.com/suitenumerique/meet-kyutai-moshi-stt> (packaging Docker MIT)
- **Nemotron 3.5 ASR Streaming 0.6B** (moteur live via audio.cpp, zéro stack) —
  <https://huggingface.co/nvidia/nemotron-3.5-asr-streaming-0.6b>
- Petits bots Meet open-source (fragiles, réf.) —
  <https://github.com/mmal3k/google-meet-bot>

*(Fiches modèle Voxtral déjà consignées : `Voxtral-Mini-4B-Realtime-2602`
= WebSocket `/v1/realtime`, vLLM nightly ; `Voxtral-4B-TTS-2603` = TTS, hors
sujet STT.)*

---

*Historique de rédaction : base + 6 passes + passe stratégique + **passe analyse
de code** (Visio/Vexa/Zoom/Teams clonés & lus) : keystone façade STT, post-réunion
officiel sur les 4, Meet REST API découverte, Vexa rétrogradé, moteurs live
Nemotron-streaming/Kyutai, micro direct première classe. À réviser à chaque « go ».*
