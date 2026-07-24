# ADR-001 — Frontière d'ingestion des réunions (temps réel & connecteurs)

- **Statut** : accepté
- **Date** : 2026-07-24
- **Portée** : chantier `docs/TEMPS_REEL_REUNIONS.md` (temps réel & connecteurs de réunion)
- **Sources** : analyse de code multi-dépôts (Visio/Vexa/Zoom RTMS/Teams Graph) +
  deux revues externes du plan directeur, triées point par point.

Cet ADR consigne les **décisions actives et les alternatives rejetées**. Le plan
directeur ne contient plus que les décisions ; le raisonnement et les arbitrages
vivent ici (une seule source de vérité, cf. revue #2 point 20).

## Contexte

TranscrIA est 100 % batch/sync (Flask + gunicorn sync, polling, pas de push). Le
seul trou concurrentiel est la capture live & l'ingestion automatique des réunions.
Objectif calibré : **viser « Bon », pas « Excellent »** (pas de bot universel type
Otter). Thèse : la capture est une **commodité** → on forke/adapte la capture, on
garde le cœur (le document de référence).

## Décisions

### D1 — Frontière d'ingestion à TROIS voies (pas une façade STT unique)

La couture commune n'est PAS un unique endpoint STT. Trois familles d'intégration,
avec chacune sa voie :

1. **Artefacts post-réunion** (Zoom Recording / Teams Graph / Meet REST / Visio
   Egress) → webhook + OAuth + téléchargement (MP4/VTT/WAV) → stockage → **job async**.
2. **Média live** (LiveKit / Zoom RTMS / Meet Media API) → **passerelle live async**
   → STT live + enregistrement horodaté → job final.
3. **Client STT compatible OpenAI** (micro / agent / Vexa) → `POST /v1/audio/transcriptions`
   → réponse STT **synchrone bornée**.

La façade OpenAI Audio (Phase K, livrée) est **un adaptateur de la voie 3**, pas la
frontière universelle. Correction vs formulation initiale « les plateformes convergent
TOUTES vers un endpoint STT ».

### D2 — Enregistrement d'import minimal + idempotence composite

Pas d'entité `MeetingOccurrence` à 6 tables maintenant (rejeté, cf. R2). Au 1er
connecteur : un enregistrement **`MeetingImport`** minimal reliant artefact distant → job,
avec **contrainte UNIQUE en base** (pas un check applicatif : deux webhooks simultanés).

Clé d'idempotence **composite** (jamais `external_meeting_id` seul — réutilisé par les
réunions récurrentes Zoom / séries Teams / espaces Meet) :
`provider + provider_account/tenant + external_occurrence_id + external_artifact_id`.
À défaut d'`external_artifact_id` fourni : `… + artifact_type + artifact_variant + checksum`.

### D3 — Contrat provider segmenté par capacités

Le `MeetingProvider` monolithique est remplacé par de petites interfaces
(`ArtifactProvider`, `LiveMediaProvider`, `ParticipantProvider`,
`PlatformTranscriptProvider`) + un manifeste `ProviderCapabilities(...)`. Un provider
Teams-post ne doit jamais porter un `stream_audio()` factice. **À figer AVANT le 1er
connecteur.** Le code du Protocol est un livrable de la Phase du contrat (A0), pas du plan.

### D4 — Séparation plan de contrôle / plan de données

Les événements (`MeetingStarted`, `RecordingAvailable`, `ParticipantJoined`…) sont de
petits messages **durables** (enveloppe : `event_id`, `schema_version`, `provider`,
`provider_account_id`, `external_occurrence_id`, `correlation_id`, `occurred_at`,
`received_at`, `deduplication_key`, `payload`). Les `AudioFrame` circulent dans le
**plan de données** (session média WS/WebRTC/SDK), jamais dans le bus durable.
Timestamps désambiguïsés : `media_timestamp_ms` (position réunion) vs
`wall_clock_timestamp` (UTC) — jamais un `start_timestamp` ambigu.

### D5 — Révisions live et canonical distinctes (pas d'écrasement)

Le pipeline batch ne « remplace » pas le live en place. À la fin du batch, la révision
`canonical` devient la révision **active/affichée par défaut** ; la révision `live` est
**conservée** (audit, diagnostic moteur, comparaison qualité). Artefacts séparés
(`live_revision` / `canonical_revision`). Pas de table `SegmentAlignment` (rejeté R2 :
les frontières de segments diffèrent, alignement coûteux et faible valeur). Champ
`provenance` conservé + identifiant de révision.

### D6 — Provenance : stabilité selon le moteur

`local-agreement` **seulement** pour les backends à fenêtre glissante. Un moteur au
streaming natif (Voxtral SSE partial/final) garde sa sémantique — pas de double passe
artificielle. `partial` (instable) → `provisional` (stabilisé par marqueur natif OU
local-agreement selon le backend) → `final_live` (fin de tour) → `canonical` (référence).

### D7 — Transcription plateforme = artefact AUXILIAIRE

L'audio brut reste la source préférée du canonical, MAIS la transcription fournie par
la plateforme est **conservée** comme artefact auxiliaire (aperçu immédiat, identité
locuteurs, alignement, repli si pas d'enregistrement, cross-check qualité, détection de
noms propres). Jamais promue canonique automatiquement. Priorité des sources :
pistes séparées à couverture **vérifiée** > enregistrement composite > pistes partielles +
composite > transcription plateforme seule.

### D8 — Post-réunion officiel des 4, live seulement où c'est propre

Post-réunion sans browser-automation : Visio (Egress→URL, **via adaptateur du contrat
Visio, pas un swap d'URL**), Zoom (Cloud Recording API), Teams (Graph, API facturées à
l'usage), Meet (REST API v2 + Drive). Live : Visio LiveKit (adapter le worker Visio
`multi_user_transcriber.py`, qui EST un worker Visio bâti sur LiveKit Agents), Zoom RTMS.
**Meet a une API live officielle** — Meet Media API (Developer Preview : inscription
projet GCP + OAuth + tous les participants, plafond de flux virtuels) → **recherche
avant production**, avant tout bot navigateur. Teams RTM = dernier recours (MS déconseille).

### D9 — Façade synchrone : double garde (taille ET durée)

La façade `/v1/audio/transcriptions` occupe un worker gunicorn sync. La **taille** (25 Mo)
est un garde grossier insuffisant (un opus de 25 Mo décode des heures) ; le **vrai** garde
est la **durée** (`max_sync_duration_s`, défaut 600 s, sonde ffprobe avant inférence) →
au-delà, 413 + renvoi `/v1/audio/ingest` (async). Deadline backend à ajouter au besoin.

### D10 — Gouvernance de la capture (transversale)

Un chapitre gouvernance transversal (pas répété par connecteur) : qui active la capture,
information des participants, propriété/lieu de stockage des artefacts, rétention
(original/live/canonical), propagation de suppression, comportement si enregistrement
désactivé ou consentement refusé, audit consultable, données envoyées à un STT distant.

## Alternatives rejetées

- **R1 — Entité `MeetingOccurrence` à 6 sous-tables maintenant** : cathédrale avant le 1er
  connecteur, contredit « mesuré / ossature intacte ». → enregistrement d'import minimal (D2),
  à faire grossir quand un 2e connecteur prouve la forme.
- **R2 — Provenance en 3 axes (origin/stability/authority) + table `SegmentAlignment`** :
  churn d'un champ déjà livré pour une feature pas encore live. → champ `provenance` + révisions
  distinctes (D5), l'alignement segment-à-segment n'est pas nécessaire.
- **R3 — Front-load du modèle de domaine complet (« Phase 0A »)** : → ordre valeur-d'abord
  (micro → Visio-post → …).
- **R4 — Coller le Protocol/DDL/matrices de tests dans le plan directeur** : ce sont des
  livrables d'implémentation (Phase du contrat + 1er connecteur), pas du plan.

## Conséquences

- Le plan directeur ne garde que les décisions actives ; ce document porte le raisonnement.
- Le contrat par capacités (D3) et l'enregistrement d'import (D2) sont les **prérequis du
  1er connecteur** (Visio post-réunion).
- Les seuils GO deviennent mesurables (points de mesure dans le plan, §Mesure).
- Phase K (façade + `/ingest` fichier + garde durée) est **déclarée livrée** ; cet ADR ne
  réaudite pas son code.
