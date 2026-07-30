# Temps réel & connecteurs de réunion — plan directeur

> **Statut** : plan directeur **largement implémenté côté code**. **✅ livré + gate réel** :
> Phase 0 (3 coutures), **Phase K** (façade STT `/v1/audio/transcriptions` + `/v1/audio/ingest`
> fichier + garde durée), **micro direct** (Phase 0-bis). **🧪 cœur implémenté + suite CI
> verte, avec transports INJECTÉS (I/O réel à valider au gate manuel)** : A0 (contrat par
> capacités + `MeetingImport` idempotent + pont), A1–A4 (post-réunion Visio/Zoom/Teams/Meet :
> OAuth, signatures/déchiffrement, adaptateurs, fetchers), L0 (passerelle live → segments à
> provenance + relais live→batch), L1 (Visio/LiveKit), L2 (Zoom RTMS), STT live (Kyutai
> msgpack + WhisperLiveKit lines/buffer), R1 (Meet Media WebRTC, démux CSRC 48 k) et C1
> (Teams RTM, dernier recours). Les formats de chaque transport sont **vérifiés par audit
> croisé contre les dépôts OSS de référence** (rtms-samples, moshi, WhisperLiveKit,
> livekit-agents, meet-media-api-samples, graph webhooks) — cf. commits `fix(connecteurs)`
> + `feat(connecteurs)`. Reste au gate manuel : brancher les sockets/WebRTC réels et les
> identifiants live (prévu). Le raisonnement d'architecture et les alternatives rejetées
> vivent dans [`docs/adr/ADR-001-frontiere-ingestion-reunions.md`](adr/ADR-001-frontiere-ingestion-reunions.md)
> (source de vérité des décisions) ; ce plan n'énonce que les décisions **actives**.

> **MàJ 2026-07-25 — câblage réel + bot.** Les transports OFFICIELS sont désormais câblés
> (glue réelle dep-gated derrière les points d'injection, cœurs testés CI) : Visio/LiveKit,
> Zoom RTMS (2-WS), STT Kyutai + WhisperLiveKit, Meet Media (WebRTC/aiortc). Un **pont PCM
> neutre** unifie l'intégration de tout acquéreur média externe. Un **bot navigateur** (fallback
> SORTANT-seul, marche derrière firewall/proxy ; sous-paquet isolé opt-in, banc d'essai Jitsi)
> couvre le live là où l'officiel manque. **Découpe v1/v2** : v1 = Visio (officiel), Zoom (RTMS+
> bot), Teams (bot), Meet (officiel) ; **v2** = bot Meet (anti-bot) + Teams-live officiel (sidecar
> .NET — la lib média `Microsoft.Skype.Bots.Media` est Windows-only, .NET lui-même tourne sous
> Linux). Tout en commits ; le gate réel (E2E par plateforme) reste à faire ensemble.

## 0. REPRISE — où on en est, et par quoi continuer (à jour du 2026-07-30)

> **Lisez cette section en premier.** Elle existe pour qu'on puisse reprendre le chantier depuis
> une autre machine sans relire les 1 300 lignes qui suivent. Les détails vivent plus bas ; ici,
> seulement l'état, les blocages, et l'ordre de travail.

### Ce qui est ÉPROUVÉ, et ce qui ne l'est pas

La distinction est la seule chose qui compte pour décider quoi faire ensuite. Elle est portée
par le champ `status` de `transcria/data/meeting_connectors.yaml`, que la page
**`/admin/connecteurs`** affiche telle quelle.

| Plateforme | Voie | État | Réseau exigé |
|---|---|---|---|
| **Zoom** | bot SDK natif | ✅ **éprouvé en réunion réelle** (compte GRATUIT) : 9 879 trames, locuteur nommé, 48 segments, 0 hallucination | sortant seul |
| **Jitsi** | bot navigateur | ✅ éprouvé | sortant seul |
| **Visio** (La Suite) | transport natif LiveKit | ✅ éprouvé | sortant seul |
| **Zoom RTMS** | webhook | 🧪 code + CI, **jamais exécuté** | ⚠ entrant HTTPS |
| **Teams** | Graph (post-réunion) | 🧪 code + CI, **jamais exécuté** | ⚠ entrant HTTPS ×2 |
| **Meet** | Pub/Sub **pull** | 🧪 code + CI, **jamais exécuté** | **aucun entrant** |

« 🧪 » ne veut pas dire « presque fini ». Il veut dire : les RÈGLES sont écrites et testées, le
transport ne l'est pas, et rien n'a jamais vu une vraie plateforme.

### Ce qui bloque — un ACHAT, pas du code

Teams et Meet attendent des abonnements payants, à prendre **sur la machine qui a accès au
pare-feu** :

- **Microsoft 365 Business** ~7 $/mois — l'enregistrement de réunion exige une édition payante.
- **Google Workspace Business Standard** ~14 $/mois, **essai gratuit de 14 jours**. Un seul
  utilisateur suffit (l'organisateur).

⚠ **Commencer par Meet, pas par Teams**, malgré son coût double : Meet n'exige **aucune
ouverture de pare-feu** (Google publie dans une file Pub/Sub qu'on interroge), là où Teams
impose deux points d'entrée HTTPS publics. Meet est donc validable même si l'ouverture de
pare-feu traîne, et c'est aussi le connecteur le plus facile à faire accepter par une DSI.

### À préparer sur la machine « pare-feu » AVANT de commencer

0. **Ce que `git clone` ne transporte PAS** — secrets, poids, `~/.transcria-bot.env`, données
   privées : liste complète et vérifiée dans **[docs/INSTALL.md § 14](INSTALL.md#14-reprendre-le-projet-sur-une-autre-machine--ce-que-git-ne-transporte-pas)**.
   ⚠ En particulier : ne pas recopier `config.yaml` tel quel (chemins absolus et calibration
   VRAM de l'ANCIENNE machine), le laisser régénérer par `install.sh`.
1. Le dépôt à jour (`git pull`) — tout le code est sur `main`.
2. Les deux comptes ci-dessus, **et les procédures pas à pas sont déjà écrites** : ouvrir
   `/admin/connecteurs` dans le portail, chaque plateforme y porte ses étapes exactes.
3. Pour **Teams uniquement** : deux URL HTTPS publiques (notifications + cycle de vie).
   Un tunnel (`cloudflared`) suffit pour éprouver.
4. Pour **Meet** : rien de réseau. Un projet Google Cloud, un compte de service, la délégation
   à l'échelle du domaine, et un abonnement Pub/Sub **de type pull** (surtout pas « push »).

### Les deux pannes MUETTES à ne pas se faire

Aucune des deux ne produit d'erreur : le code semble marcher et rien n'arrive jamais.

- **Teams** — sans politique d'accès applicatif (`New-CsApplicationAccessPolicy`), l'application
  s'authentifie mais ne voit les artefacts d'AUCUN organisateur.
- **Meet** — sans le rôle *Pub/Sub Publisher* accordé à
  `meet-api-event-push@system.gserviceaccount.com` sur le sujet, l'abonnement est créé, l'API
  répond 200, et la file reste vide à jamais.

### Ordre de travail recommandé

**Absorbé par les vagues 3-5 du chantier UI réunions** (`UI_REUNIONS_WORKFLOW.md` +
`VAGUE5_PISTES_SEPAREES.md`, livrées juillet 2026) — ces anciens items ne sont plus des
chantiers :

- **L1** (question à l'installation) → `install.sh --with-meeting-bots`, unité systemd du
  runner posée, activation par MENU + bouton « Activer » 1-clic sur `/admin/connecteurs`
  (auto-provisionnement complet, check-list vivante).
- **L3** (images de bot publiées) → job matrix GHCR des images de bot en CI (vague 4).
- **L4** (écran « Rejoindre une réunion ») → TRANCHÉ par D1 : jamais de droits Docker au
  portail — l'écran livré est « Planifier une réunion » (vague 3), exécuté par le
  **meeting-runner** séparé qui TIRE les intentions.
- Et au-delà des L : pistes séparées + STT par piste + sous-diarisation des pistes salle +
  **suivi en direct provisoire** sur la page du job (vague 5, lots A-C).

**Reste sans rien acheter** :

1. **L2 — parcours documenté de bout en bout** dans `README`/`INSTALL` (aujourd'hui :
   activer par le menu → planifier depuis la page d'accueil). Rend le chantier **testable
   par quelqu'un d'autre que son auteur**. Coût S.
2. **Sous-salles Zoom** : codées, jamais exécutées. Ne demande qu'une salle ouverte.
3. **Revue sécurité (Opus 5)** avant mise en service réelle : crypto meeting_ref/passcode,
   jetons `tia_`, endpoints `/v1` (ingest v2 pistes, `/captions`), runner.

**Une fois les comptes achetés** : brancher les appels réseau derrière les points d'injection
déjà spécifiés (cf. §7-quinquies) — `LiveConnectorSession` est leur contrat d'orchestration
(sort tranché D5.6 : le bot n'y passe pas) — puis le branchement sur l'ingestion, puis
l'E2E réel.

### Où trouver le reste

| Question | Fichier |
|---|---|
| Que contient `connector_service/`, module par module ? | `AGENTS.md` § Structure du projet |
| Quelle procédure pour activer une plateforme ? | `/admin/connecteurs`, ou `transcria/data/meeting_connectors.yaml` |
| Pourquoi telle décision d'architecture ? | `docs/adr/ADR-001-frontiere-ingestion-reunions.md` |
| Comment lancer un bot ? | `docs/BOT_REUNION.md` |
| Quelles briques cloud existent, et ce qui manque ? | §7-quinquies de ce document |

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

**Buts mesurables de ce chantier** (axes distincts, ADR-001 D8) : porter
l'**ingestion automatique post-réunion** à « **Très bon** » sur Visio/Zoom/Teams/Meet
(fait passer TranscrIA de « upload manuel » à « couvre l'essentiel des réunions
d'entreprise ») ; porter la **transcription/assistance live** à « **Bon** » sur
Visio/Zoom ; le **micro** (présentiel/dictée) est première classe. Le **bot universel
multi-plateforme** reste **hors périmètre assumé**.

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
| **Zoom** | Cloud Recording API | ✅ **RTMS** (officiel, par participant) — ou **Meeting SDK natif** (bot, par participant, locuteurs nommés) |
| **Teams** | **Graph** (VTT/MP4, webhook chiffré, API facturées à l'usage) | 🟠 bot RTM (MS déconseille) |
| **Meet** | **Meet REST API v2 + Drive** | 🔬 **Meet Media API** (officielle, Developer Preview) |
| **Micro direct** (présentiel/dictée) | fichier → façade/job | ✅ WhisperLiveKit (WS) |

→ On couvre les 4 plateformes **en post-réunion sans une seule ligne de
browser-automation** ; le live là où c'est propre (Visio natif, Zoom RTMS, Meet via
Media API officielle en recherche). **Vexa quitte la feuille de route principale** —
repli expérimental du Meet-live seulement (extrait de code, pas la plateforme).

**✅ VALIDÉ EN RÉUNION RÉELLE (2026-07-27), compte Zoom GRATUIT.** Le bot entre micro et
caméra coupés, capte l'audio par participant (9879 frames, 4226 sonores) et le transcrit avec
les **locuteurs nommés** — 29 segments attribués, code de sortie 0. Détail et défauts corrigés
en §6.5 de `docs/BOT_REUNION.md`.

**Correction apportée en cours de chantier (juillet 2026) — Zoom par bot.** Le pilote
navigateur écrit pour Zoom (`zoom_web`, **retiré du dépôt en vague 0 de consolidation —
l'historique git le conserve**) est **inexploitable** : le client Web
de Zoom oppose un reCAPTCHA à toute automatisation (vérifié au gate — le nom est saisi, le
bouton s'active, le clic aboutit, et Zoom refuse en silence). La documentation de Zoom
recommande d'ailleurs explicitement le **SDK natif** pour un bot headless Linux. Ce chemin est
implémenté (`live/zoom_sdk_transport.py`, `Dockerfile.zoom-sdk`) et vaut mieux que RTMS quand
l'hôte ne peut pas activer RTMS, puisqu'il n'exige **rien de l'hôte** pour une réunion du
compte propriétaire de l'app. Il **nomme les locuteurs**, ce dont le navigateur était
incapable. Réserve à connaître : une réunion d'un compte **externe** impose une revue de l'app
par Zoom — là, RTMS activé par l'hôte reste la seule voie sans démarche.

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
| Maintenable | interfaces provider **par capacités**, adaptateurs isolés, tests communs + par capacité |
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
  - **live/rapide** (faible latence) = backend choisi par config (candidats :
    Nemotron-streaming, Kyutai, Voxtral realtime ; défaut par bench streaming réel) ;
  - **référence/finale** (précision max) = cohere/qwen3/whisperx du pipeline.
- **Visio (La Suite numérique) = LiveKit** (dépôt `suitenumerique/meet`,
  Django + React, MIT). Enregistrement + transcription (bêta) déjà présents.
- **Zoom RTMS** (Realtime Media Streams) : audio PCM 16 k **par participant** +
  events + timestamps, sans bot visible — nécessite OAuth/scopes/crédits.
- **Teams** : Graph (post-réunion VTT/MP4) d'abord ; bot média temps réel
  déconseillé par Microsoft (dernier recours).

## 2. Architecture cible

```
   Plateforme (Visio/Zoom/Teams/Meet)
            │  (webhook / SDK / websocket)
            ▼
   ┌─────────────────────────────┐   SERVICE CONNECTEUR (async, isolé, opt-in)
   │  Adaptateur de plateforme    │   FastAPI/uvicorn OU worker asyncio
   │  ArtifactProvider            │   process séparé du web sync
   │  ParticipantProvider         │   déclare ProviderCapabilities(...)
   │  PlatformTranscriptProvider  │
   │  LiveMediaProvider           │
   └─────────────┬───────────────┘
                 │ contrôle: événements durables (enveloppe versionnée)
                 │ données live: AudioFrame / TranscriptPartial (session média)
                 ▼
   ┌─────────────────────────────┐
   │ Session temps réel TranscrIA │  affichage provisoire + audio horodaté
   │  (chaîne STT live, config.)  │  segments provenance = final_live
   └─────────────┬───────────────┘
                 │  fin de réunion
                 ▼
   ┌─────────────────────────────┐
   │  API de jobs TranscrIA       │  ← le cœur EXISTANT, inchangé
   │  pipeline complet (batch)    │  révision canonical = référence active
   └─────────────────────────────┘
```

**Adaptateurs par capacités** (ADR-001 D3) : chaque plateforme n'implémente que les
interfaces qu'elle supporte + déclare `ProviderCapabilities`. Les événements sont
normalisés une fois → **tests transversaux communs + suites par capacité** (§Mesure).

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

### DoD A0
Interfaces **par capacités** + manifeste `ProviderCapabilities`, enveloppe d'événements
de contrôle versionnée + contrat de session média + `AudioFrame` figés et testés ;
`MeetingImport` avec **`dedup_key` non-nulle** (ADR-001 D2) + création de job **idempotente
côté serveur** (`Idempotency-Key`) ; **providers factices** en plusieurs combinaisons de
capacités prouvant le pont vers l'API de jobs ; **tests de crash** entre import/upload/
création de job ; **`ProviderReconciler`** minimal (D2-bis) ; service async démarrable/
arrêtable ; import-linter confirme que le cœur sync n'importe pas le service async ; doctor
du service.

## 5. Les connecteurs par plateforme

> Descriptions détaillées ci-dessous ; le **séquencement de référence est le §7**
> (révisé : keystone façade d'abord, post-réunion officiel des 4).

### Visio post-réunion (A1, premier connecteur) — effort M
- Visio finit l'enregistrement → **contrat de tâche Visio** (`POST /api/v1/tasks/` +
  métadonnées : propriétaire, fichier, salle, date ; l'audio est dans MinIO/S3) → notre
  **adaptateur** `ArtifactProvider` crée un job TranscrIA → pipeline complet → restitution.
- **Développer contre un contrat RÉEL** (revue #3) : figer d'abord des fixtures — un vrai
  payload `/api/v1/tasks/`, ses métadonnées, une réf MinIO/S3, un cas retry, un objet
  absent, une tâche dupliquée, le format de retour attendu vers La Suite Docs.
- Zéro live. Prouve le partenariat + le chemin `fetch_artifacts`.
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
- **DoD A1** : webhook/tâche Visio signé·e + vérifié·e + **idempotent** (`dedup_key`) ;
  un enregistrement Visio réel (ou rejoué) crée un job et produit les livrables
  complets ; un événement volontairement supprimé est **rattrapé par la réconciliation**
  sans doublon ; échec plateforme = job en erreur explicite, jamais de perte silencieuse.

### Visio live (L1) — effort L
- Rejoindre la salle LiveKit côté serveur, s'abonner aux **pistes par
  participant** → AudioFrames → chaîne STT live (config.) →
  transcript `final_live` + identité LiveKit.
- **Diarisation en partie inutile** : quand une piste = un participant connu,
  on aligne les segments sur cette identité sans diariser globalement. MAIS
  garder la diarisation par piste (règle « piste ≠ personne », §6).
- **Fondation** : le **worker Visio** `src/agents/multi_user_transcriber.py`
  (dépôt `suitenumerique/meet`), **bâti sur LiveKit Agents** — bon point de départ
  car il crée déjà une session STT **par participant** et rend le backend STT
  configurable (Deepgram/Kyutai en démo) → on branche **notre moteur live**.
  (Ce n'est PAS un exemple générique LiveKit : c'est du code applicatif Visio sur
  LiveKit Agents.) L'identité participant est **connue avant** la transcription →
  alignement direct, pas de diarisation globale.
- Fin de réunion → publication de la révision `canonical` (devient l'active), la
  révision `live` reste consultable (ADR-001 D5) — pas d'écrasement.
- Utilise couture 1 (provenance) + 2 (source `meeting`) + 3 (chaîne live).
- **DoD L1** : rejoint une salle LiveKit de test, produit du texte provisoire par
  participant identifié en direct, puis un `canonical` complet en fin de réunion ;
  latence live mesurée ; cas durs (§6) couverts ; VRAM du moteur live gérée
  (reclaim, cf. machine serrée).

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
- **Moteur live** : sélecteur `live_stt_backend` (candidats Nemotron-streaming /
  Kyutai / Voxtral realtime) + URL du serveur, secret masqué (comme SSO/LDAP).
- **Visio post-réunion** : URL publique de l'adaptateur de tâches + secret de
  signature + config stockage MinIO/S3 (ou mode callback/URL présignée) + état du
  dernier import. **Visio live** : URL LiveKit + api key/secret + identité worker +
  backend STT live (URL/secret du runtime). *(La façade OpenAI Audio n'apparaît que
  si un composant Visio l'utilise comme client STT, pas comme contrat de post-traitement.)*
- **Zoom** : client id/secret + secret webhook + statut RTMS ; bouton « tester ».
- **Teams** : app registration (client id, certificat) + état d'abonnement (actif/expiré).
- **Meet** : OAuth Google Workspace (scopes Meet+Drive) + statut.
- Secrets masqués + **audit de toute connexion** (réutilise le chantier identité).

### Live — l'expérience temps réel (panneau « Réunion en direct »)
- N'apparaît que si un connecteur live tourne. **Captions par participant**
  (identité) ; `partial`/`provisional` en **gris**, `final_live` figé.
- Bandeau visible : « le direct est un suivi ; le compte-rendu de référence sera
  produit à la fin » (la règle d'or à l'écran).
- Fin de réunion → « le document de référence est maintenant disponible » : la
  révision `canonical` devient l'active/affichée par défaut ; « afficher la version du
  direct » reste possible (ADR-001 D5) — le document officiel « ne bouge pas tout seul ».

### Micro direct (source `mic`)
- Bouton **« Enregistrer au micro »** (record-then-transcribe) sur la création de job.
- (Optionnel) **live rolling** : captions défilantes (fork WhisperLiveKit).

### Job « réunion » vs job « upload »
- Un job de connecteur affiche un badge **source** (Visio/Zoom/Teams/Meet/Micro) +
  `external_occurrence_id` + `meeting_import_id` + participants. La clé technique de
  déduplication (`dedup_key`) reste dans `MeetingImport`, pas reconstruite depuis le
  job. Tout l'aval (livrables) = **UI existante**.

## 6. Règles transverses

- **Conserver les 2 chaînes STT** : ne jamais remplacer la finale par le live.
- **Piste ≠ personne** : une piste « participant » peut être un micro de salle,
  un téléphone, plusieurs personnes → garder la **diarisation par piste**
  activable ; l'identité de piste n'est qu'un indice.
- **Cas durs à tester** (tests communs + par capacité, §Mesure) : réordonnancement
  de paquets, doublons de webhooks, perte/reprise de flux, reconnexion, changement
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

Légende : **✅** livré + gate réel · **🧪** cœur implémenté + suite CI verte, transports
INJECTÉS (I/O réel à valider au gate manuel).

| ID | Quoi | Dépend | Effort |
|---|---|---|---|
| **0** ✅ | 3 coutures (provenance / source audio / 2 chaînes STT) | — | S |
| **K** ✅ | Façade STT `/v1/audio/transcriptions` + `/v1/audio/ingest` fichier + gardes taille/durée | couture 2/3 | M |
| **0-bis** ✅ | Micro direct (record-then-transcribe → upload) | K | S |
| **A0** 🧪 | Contrat providers **par capacités** + manifeste + événements contrôle/données + `MeetingImport` (idempotence composite) + service async isolé | K + coutures | M |
| **A1** 🧪 | Visio post-réunion — **adaptateur** au contrat Visio (tâche + métadonnées → réunion), PAS un swap d'URL (ADR-001 D8) | A0 | M→sem. |
| **A2** 🧪 | Zoom post-réunion — Cloud Recording API (OAuth) → `/ingest` | A0 | M→sem. |
| **A3** 🧪 | Teams post-réunion — Graph, webhook chiffré → fetch MP4/VTT (API facturées) | A0 | sem. |
| **A4** 🧪 | Meet post-réunion — REST API v2 + Drive → job (pagination + statut HTTP durcis) | A0 | M |
| **L0** 🧪 | Passerelle live générique (plan de données audio séparé, D4) + relais live→batch + STT live Kyutai/WhisperLiveKit | A0 | L |
| **L1** 🧪 | Visio live — mapping `rtc.AudioFrame`→`RawFrame` (LiveKit), séquence/horloge synthé | L0 | L |
| **L2** 🧪 | Zoom live (RTMS) — handshake signé, keepalive, parse audio niché, `data_opt=2` par participant | L0 | L→sem. |
| **R1** 🧪 | Meet live — Meet Media API WebRTC (preview), démux CSRC→participant 48 k, plafond 3 flux | L0 | rech. |
| **C1** 🧪 | Teams RTM — **dernier recours** (MS déconseille) | L0 | cond. |

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
| **Façade STT** | flag (endpoint dans l'app web existante) | `live.facade.enabled` + `live.facade.max_sync_audio_mb` (25) + `live.facade.max_sync_duration_s` (600, garde durée) | `GET /health` façade |
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

### ⚠️ Écart entre ce tableau et la réalité (constaté 2026-07-27)

Le tableau ci-dessus décrit l'INTENTION. À ce jour, **rien de ce chantier n'est atteignable
par quelqu'un qui installe TranscrIA** : tout suppose de cloner le dépôt et de lancer des
scripts à la main. Vérifié : `install.sh` ne mentionne ni les bots ni le connecteur (0
occurrence) ; `connector_service/` n'est embarqué dans AUCUNE des trois images applicatives
(slim, bundled, worker) ; aucun écran d'interface ; `README` et `docs/INSTALL.md` muets.

C'est cohérent avec l'état du chantier — mais le risque est d'accumuler de la capacité sans
chemin vers l'utilisateur, et donc **sans personne d'autre que l'auteur pour l'éprouver**.

Reste à faire, par coût croissant :

| # | Ce qui manque | Effet | Coût |
|---|---|---|---|
| L1 | Question à l'installation : « transcrire des réunions en direct ? » → pose `live.facade.enabled`, propose de déporter l'inférence | la façade devient découvrable au lieu d'être invisible | S |
| L2 | Parcours documenté de bout en bout dans `README`/`INSTALL` : activer la façade → créer un jeton → image → `scripts/bot.sh` | rend le chantier **testable par d'autres** | S |
| L3 | Publier les images de bot sur GHCR, comme les images applicatives | supprime Docker de l'expérience utilisateur (plus rien à construire) | M |
| L4 | Écran « Rejoindre une réunion » : coller un lien, choisir la langue, le portail lance le bot | premier usage sans ligne de commande | **L — décision d'architecture** |
| L5 | Déclenchement automatique depuis l'agenda | le produit visé | XL |

**L4 est le premier point qui engage l'architecture** : le portail n'a aujourd'hui aucun droit
sur Docker, et lui en donner n'est pas anodin (surface d'attaque, droits du démon, isolation).
À trancher explicitement avant d'écrire la moindre ligne — pas à décider en passant.

✅ Fait depuis ce constat : les cinq clés `live.facade.*`, lues par le code depuis la Phase K,
n'étaient documentées NULLE PART ; elles le sont désormais (`CONFIG_REFERENCE.md`,
`config.example.yaml`). La fonctionnalité était jusque-là inatteignable sans lire le code.

✅ Fait également : la page **`/admin/connecteurs`** (`transcria/data/meeting_connectors.yaml` +
`transcria/web/connector_catalog.py`). Elle porte, pour les six plateformes, la procédure exacte,
ce qu'il faut renseigner, l'exigence réseau — et surtout le STATUT RÉEL : `validated` (éprouvé en
conditions réelles, date à l'appui) ou `implemented` (le code passe la CI mais n'a jamais été
exécuté en vrai). Cette distinction est le cœur de son honnêteté : afficher Teams ou Meet comme
prêts tromperait l'exploitant.

C'est une page de LECTURE — elle ne modifie rien, précisément parce que L4 n'est pas tranché.
Elle ne dispense donc ni de L1 ni de L2, mais elle rend la marche à suivre trouvable sans lire le
code, ce qui était le vrai blocage.

## 7-bis. Zoom — comment un CLIENT met cela en service (étude, 2026-07-27)

Question posée : *« si le moteur fonctionne, comment font les utilisateurs ? Aller sur le
Marketplace créer une app est impossible. »* Étude des voies possibles, sur sources Zoom.

### La réponse courte : ce n'est PAS par utilisateur

Chez Zoom, « compte » désigne **l'organisation**, pas la personne. Un bot non publié
*« can only join meetings within their development account »*
([Zoom Technical Library](https://library.zoom.com/zoom-workplace/zoom-meetings/securing-zoom-meetings-explainer/using-automated-meeting-tools-in-a-secure-and-effective-manner)) —
mais **toutes** les réunions de cette organisation, quel qu'en soit l'hôte.

Donc : **l'admin Zoom du client crée UNE app, UNE fois.** Les salariés ne font rien. La revue
Zoom n'est requise que pour joindre les réunions d'**autres** organisations
([conditions de revue](https://developers.zoom.us/docs/distribute/sdk-feature-review-requirements/)).

### Les trois voies, comparées

| | **App interne / organisation** ⭐ | **App publiée au Marketplace** | **RTMS** |
|---|---|---|---|
| Qui agit | l'admin du client, une fois | l'éditeur, puis chaque client autorise | l'admin + l'hôte |
| Revue Zoom | **non** | **oui** (+ jeton OBF) | non |
| Portée | réunions de l'organisation | réunions **externes** aussi | réunions de l'organisation |
| **Réseau entrant** | **AUCUN** — 100 % sortant | aucun | **⚠ point HTTPS PUBLIC obligatoire** |
| Bot visible | oui | oui | pas de bot |
| Convient à l'auto-hébergé | **oui** | mal (un Client ID partagé par tous les déploiements) | seulement si l'entrant est possible |

### ⚠ RTMS impose une ouverture de pare-feu — vérifié

Zoom **pousse** l'évènement `meeting.rtms_started` vers un point d'entrée que l'on doit
*« expose publicly reachable HTTPS »*
([documentation officielle](https://developers.zoom.us/blog/realtime-mediastreams-websockets/)).
Les deux WebSockets (signalisation, média) sont ensuite **sortants**, mais le déclencheur,
lui, est **entrant**.

C'est structurant pour un produit auto-hébergé : RTMS exige une URL publique stable, un
certificat valide et une entrée à travers le pare-feu du client — précisément ce que le bot
évite. Le bot reste donc la voie la plus déployable en entreprise, et RTMS l'alternative
quand l'organisation dispose déjà d'une exposition maîtrisée.

*(Zoom recommande néanmoins RTMS pour la capture automatisée. Notre bot reste légitime dans
le régime « même compte », mais ce n'est pas leur voie préférée — à savoir, pas à cacher.)*

### Politique d'usage des bots — ce que Zoom exige

Le régime « même compte » **ne dispense pas** de ces obligations :

| Obligation Zoom | Où nous en sommes |
|---|---|
| Le bot apparaît dans la liste des participants | ✅ acquis |
| Une notification d'enregistrement informe les participants | ✅ acquis (observé en essai réel) |
| Une invite d'autorisation précise les données accédées | ✅ acquis (droit d'enregistrement) |
| **Le nom doit désigner l'initiateur ET la fonction** — Zoom cite « *Steve Miller's notetaking app* » | ⚠ **à corriger** : nous affichons « TranscrIA » |
| Revue Marketplace pour les réunions d'autres comptes | ✅ hors périmètre du modèle retenu |

Le point de nommage est le seul écart, et il est peu coûteux : un défaut de la forme
« Transcription — <organisateur> » plutôt que le seul nom du produit.

### Procédure pour l'admin du client (à reprendre dans la doc utilisateur)

1. [marketplace.zoom.us](https://marketplace.zoom.us) → se connecter **avec un compte admin de
   l'organisation** (c'est ce compte qui détermine la portée) ;
2. volet **en bas à gauche** → **Developer** → **Build an app** → **General app** → **Create** ;
3. **Basic Info** → relever **Client ID** et **Client Secret** (jeu **Development**) ;
4. **Features → Embed** → activer **Meeting SDK** ;
5. **ne pas publier** l'app — elle reste interne ;
6. portail Zoom → **Settings → Recording & Transcript** → activer **Record to computer files**,
   puis, sous *« Who can request host permission to record? »*, cocher **Internal meeting
   participants** et de préférence **Auto approve their permission requests** ;
7. coller les deux valeurs dans la configuration de TranscrIA.

### Points encore ouverts

- **Un compte gratuit suffit techniquement** (vérifié en réunion réelle), mais le régime
  « organisation » suppose un compte d'entreprise avec des utilisateurs rattachés : à
  confirmer chez le premier client réel.
- **Sans auto-approbation**, l'hôte doit accepter une fenêtre à chaque réunion — et à chaque
  entrée en sous-salle. Passer le bot co-hôte l'évite.
- **La politique d'usage des bots doit être relue avant commercialisation** : elle porte sur
  le consentement et l'information des participants, pas seulement sur la technique.

## 7-ter. Teams — les trois voies vérifiées une à une (étude, 2026-07-27)

Étude menée avec la méthode apprise sur Zoom : vérifier AVANT d'écrire, et regarder ce que
font les projets de référence plutôt que supposer. Point de départ : *« le SDK a des
composants Windows, on verra en v2 — mais Wine dans un Docker, c'est idiot ? »*

### Voie 1 — SDK média temps réel (`Microsoft.Skype.Bots.Media`) : hors de portée

Wine ne s'attaque pas au bon verrou. La contrainte n'est pas « quel OS » mais **où et
comment héberger** ([doc Microsoft](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/calls-and-meetings/requirements-considerations-application-hosted-media-bots)) :

> *« Production application-hosted media bots must be deployed on a Windows Server guest OS
> **in Azure**. »*

S'y ajoutent une **IP publique PAR INSTANCE** (instance-level public IP), un **port public
entrant** mappé par instance, et des certificats SSL valides. Même un Wine parfait laisserait
donc : hébergement Azure imposé, IP publique, ports entrants. Voie fermée pour un produit
auto-hébergé.

### Voie 2 — Bot navigateur : possible, mais 10 à 20 fois plus coûteux qu'on ne l'imagine

Les DEUX projets de référence procèdent ainsi. Leur volume de code est le premier
avertissement :

| | Pilote | Payload | Total |
|---|---|---|---|
| attendee (Teams) | 689 l | 3 995 l | **~4 700 l** |
| vexa (Teams) | — | — | **~2 800 l** |
| **notre pilote Jitsi** | 200 l | partagé | **~200 l** |

Et surtout, ce que leur code révèle **indépendamment l'un de l'autre** :

- `UiBlockedByCaptchaException` — Teams oppose un **« Verify you're a real person »**,
  exactement comme Zoom ;
- `UiTeamsBlockingUsException` — un état dédié à « Teams nous bloque » ;
- *« Due to org policy, you need to sign in or use Teams on the web to join this meeting »* —
  l'organisation peut EXIGER une connexion ;
- redirections « light experience » à contourner ;
- côté vexa : un état `blocked` documenté « Bot-detection block (reCAPTCHA / blank block
  page) », et un état **`needs_human_help`**.

Leur parade commune : **se connecter avec un VRAI compte Microsoft**. attendee le dit
explicitement — *« If a login is available, but we aren't using it, we should login and retry
and see if the captcha goes away »*. L'entrée anonyme n'est donc pas fiable.

### ⚠ Microsoft resserre activement — construire ici, c'est bâtir sur du sable

- l'admin peut **désactiver entièrement** l'entrée anonyme ;
- il peut restreindre le passage du hall à « People in my org » ;
- **à partir de mai 2026, les bots tiers externes seront ÉTIQUETÉS comme bots dans le hall**
  au lieu d'apparaître comme des participants ordinaires
  ([doc Microsoft](https://learn.microsoft.com/en-us/microsoftteams/anonymous-users-in-meetings)).

Autrement dit, Microsoft outille les administrateurs pour reconnaître et bloquer exactement
ce type de bot. Une intégration bâtie dessus se dégradera à mesure que les organisations
activeront ces réglages.

### Voie 3 — Graph post-réunion : la seule voie officielle et stable

Déjà codée (`connector_service/signatures.py` pour le déchiffrement, receveurs dans
`connector_service/app.py`), **jamais éprouvée**. Coût : un point d'entrée HTTPS public pour
les notifications Graph — entrant, donc, mais pour un flux POST-réunion, ce qu'une DSI
accepte plus volontiers qu'une exposition permanente.

### La voie 3 en détail — ce qu'elle exige RÉELLEMENT (vérifié 2026-07-27)

**Ce qui n'est plus un obstacle.** Les API Teams de Graph **ne sont plus facturées** depuis le
25 août 2025 : *« no longer metered, and no billing configuration is required »*
([doc Microsoft](https://learn.microsoft.com/en-us/graph/teams-licenses)). Plus d'abonnement
Azure, plus de coût à la minute. C'était l'inquiétude principale, elle tombe.

**Deux verrous qu'on aurait découverts trop tard.**

1. Un nouveau réglage de locataire, **« Transcript API access »** (centre d'administration
   Teams → politique de réunion) : **désactivé par défaut**, et **appliqué à partir du
   29 juillet 2026**. Désactivé, l'API répond `403 — « Graph API access to transcripts is
   disabled for this tenant »`.
2. Le réglage voisin **« Include speaker attribution » est AUSSI désactivé par défaut** : sans
   lui, les transcriptions reviennent **sans les noms des locuteurs**, c'est-à-dire sans ce
   qui fait l'intérêt du produit.

**Un point de conception à corriger dans notre cible.** L'API *transcriptions* rend le texte
**produit par Teams**, pas de l'audio. Or notre valeur est notre propre chaîne (STT,
diarisation, LLM). C'est donc l'API **enregistrements** qui nous intéresse —
`GET /users/{id}/onlineMeetings/{id}/recordings/{id}/content` rend le MP4, que nous
transcrivons nous-mêmes. Cela déplace la cible, et rend au passage le verrou nº 1 moins
critique (il porte sur les transcriptions).

**Ce que l'admin du client doit faire** — nettement plus que les deux champs de Zoom :

1. enregistrer une application Entra ID + consentement administrateur ;
2. créer une **politique d'accès applicatif** en PowerShell (`New-CsApplicationAccessPolicy`)
   et l'accorder aux utilisateurs concernés ;
3. activer « Transcript API access » et « Include speaker attribution » ;
4. exposer un **point d'entrée HTTPS public** pour les notifications de changement ;
5. et la réunion doit être **effectivement enregistrée**.

### Peut-on l'éprouver sans locataire d'entreprise ? (vérifié 2026-07-27)

| Piste | Verdict |
|---|---|
| **Sandbox E5 développeur (gratuit)** | **Fermé** depuis 2025 : réservé aux abonnés Visual Studio Pro/Enterprise ou aux membres du programme partenaire |
| **Teams Free / Teams Essentials** | **Insuffisant** — pas d'enregistrement ni de transcription de réunion |
| **Microsoft 365 Business Basic** ⭐ | **Suffisant** — enregistrement et transcription inclus dès ce palier. ~7 $/utilisateur/mois (hausse de juillet 2026), **premier mois gratuit** |

Un seul utilisateur suffit pour éprouver : l'organisateur. L'application, elle, travaille en
permissions applicatives et ne consomme pas de licence.

⚠ L'essai gratuit **bascule automatiquement en payant** à l'échéance — à résilier si l'on ne
poursuit pas.

### Décision proposée

| Voie | Verdict |
|---|---|
| SDK média temps réel | **Abandonnée** — Azure + IP publique par instance ; Wine n'y change rien |
| Bot navigateur | **Repoussée** — ~4 700 lignes, captcha, politiques d'organisation, et Microsoft resserre |
| **Graph post-réunion (enregistrements)** | **Éprouvable pour ~7 $** — ne rien écrire avant d'avoir le locataire |

La règle qui s'applique ici est celle apprise sur Teams RTM : **ne pas écrire un connecteur
qu'on ne peut pas exécuter**. Du code jamais éprouvé donne une fausse impression de
couverture, et c'est précisément ce que la page d'administration signale désormais.

Et si le temps réel Teams devient une exigence client : prévoir un **compte Microsoft dédié
au bot** (ce que font les deux références), en assumant la fragilité — pas en la découvrant.

## 7-quater. Google Meet — étude (2026-07-27), même méthode que Teams

### ⭐ La trouvaille : Meet n'exige AUCUN port entrant

Google ne pousse pas vers un webhook : l'**API Google Workspace Events** publie dans un
**sujet Pub/Sub**, et Pub/Sub accepte les abonnements en mode **PULL**. La documentation le
dit sans détour :

> *« Pull subscriptions are useful for scenarios where the subscriber **cannot be exposed to
> a public endpoint (e.g., behind a firewall)** »* — et plus loin : *« An application server
> that is either a cloud or on-premises system can subscribe to the Pub/Sub topic in order to
> receive the message **through the firewall** »*
> ([documentation Google](https://developers.google.com/workspace/chat/quickstart/pub-sub))

C'est structurant pour un produit auto-hébergé, et cela renverse le classement :

| | Zoom (bot SDK) | Zoom RTMS | **Teams (Graph)** | **Meet (Pub/Sub)** |
|---|---|---|---|---|
| **Port entrant** | aucun | ⚠ webhook public | ⚠ webhook public | **aucun (pull)** |
| Bot dans la réunion | oui | non | non | non |
| Coût du test | **gratuit** | — | ~7 $/mois | ~14 $/mois (essai 14 j) |
| État | ✅ **validé en réel** | jamais exécuté | jamais exécuté | jamais exécuté |

Meet est donc, sur l'axe déploiement, **le connecteur post-réunion le plus facile à faire
accepter par une DSI** — plus encore que Teams, qui impose une URL publique.

### Prérequis vérifiés

- **Édition** : la transcription et l'enregistrement Meet exigent **Business Standard** ou
  au-dessus (Business Plus, Enterprise Standard/Plus, Education Plus). Le plan gratuit ne
  suffit pas, comme chez Teams.
- **Coût d'essai** : ~14 $/utilisateur/mois, **essai gratuit de 14 jours**. Un seul
  utilisateur suffit (l'organisateur).
- **Évènements disponibles** (API Workspace Events) :
  `google.workspace.meet.recording.v2.fileGenerated`,
  `google.workspace.meet.transcript.v2.fileGenerated`, plus conférence démarrée/terminée et
  participant entré/sorti.
- **Artefacts** : Meet dépose l'enregistrement dans le **Drive de l'organisateur** ; la
  ressource `conferenceRecords/{cr}/recordings/{rec}` porte `driveDestination.file` et
  `exportUri`.

### Ce que nous avons déjà, et ce qui manque

| Brique | État |
|---|---|
| `providers/meet.py` — `conferenceRecords` → Drive | **écrit** (164 l), jamais exécuté |
| `live/meet_media*.py` — Media API temps réel (WebRTC) | **écrit**, jamais exécuté ; ⚠ plafonné aux **3 locuteurs les plus forts** et admission HUMAINE |
| **Abonnement Workspace Events + Pub/Sub (pull)** | partie PURE **écrite et testée** (`meet_events.py`, `pubsub_pull.py`, `subscription_renewal.py`, `subscription_keeper.py`) ; il reste les APPELS réseau — cf. §7-quinquies |
| Téléchargement Drive | manquant |

Autrement dit, nous savons déjà lire un enregistrement une fois qu'on en connaît la
référence ; nous ne savons pas encore **apprendre qu'il existe**. C'est exactement l'inverse
de ce que l'on croirait en regardant le nombre de lignes écrites.

### Projets de référence trouvés (recherche du 2026-07-27)

| Projet | Langage | Licence | Apport |
|---|---|---|---|
| **Tutoriel officiel Google** « Observe meeting events with Python » | Python | doc | **La référence** : confirme le mode PULL et donne le code |
| `googleworkspace/python-samples` | Python | **Apache-2.0** | démarrage rapide Meet seulement, pas d'évènements |
| `gm-space-api` (cloné) | Python | **MIT** | utilise exactement le motif : `pubsub_v1.SubscriberClient`, `subscriber.subscribe`, `workspaceevents.googleapis.com/v1/subscriptions` — **réutilisable** |
| `jido_connect/docs/…spike.md` (cloné) | Elixir | MIT | note d'étude, 178 l — deux pièges ci-dessous |

Il n'existe PAS d'équivalent d'attendee pour cette voie : aucun projet mûr et éprouvé. La
**documentation officielle est ici la source solide**, et le tutoriel Python en donne le
squelette exact.

### Deux pièges relevés dans la note d'étude, absents de la doc principale

1. **Les évènements Meet ne portent PAS de données de ressource.** `payloadOptions` n'est
   documenté que pour les évènements Chat : un évènement Meet est une simple RÉFÉRENCE. Il
   faut donc toujours aller chercher l'enregistrement par l'API REST — ce que fait déjà
   `providers/meet.py`, qui se trouve ainsi confirmé dans sa conception.
2. **Les abonnements expirent, au maximum sept jours** sans données de ressource. Une boucle
   de renouvellement est indispensable, exactement comme chez Teams — même besoin, deux
   plateformes, donc une brique à concevoir en commun plutôt qu'en double.
3. **Le sujet Pub/Sub doit autoriser un publicateur nommé**, sinon il ne reçoit jamais rien.
   La documentation « Create a Google Workspace subscription » impose d'accorder le rôle
   *Pub/Sub Publisher* à `meet-api-event-push@system.gserviceaccount.com` (chaque application
   Workspace a le sien : `chat-api-push@…`, `drive-api-event-push@…`). **Sans ce droit,
   l'abonnement est créé, l'API répond 200, et aucun message n'arrive jamais** — panne
   silencieuse la plus coûteuse de cette voie, car rien ne la signale. Le même document
   confirme par ailleurs que l'abonnement Pub/Sub créé est **`pull`-based** par défaut, ce qui
   valide notre choix réseau.

### Décision proposée

Meet post-réunion est **la prochaine cible la plus rentable** après Zoom : pas de bot, pas de
port entrant, et la moitié du chemin déjà parcourue. Sa seule vraie contrainte est le coût
d'essai (14 $ contre 7 $ pour Teams), et le plafond à 3 locuteurs ne concerne QUE la voie
temps réel, que nous ne visons pas ici.

⚠ Réserve de méthode, tirée de Teams RTM : ne rien ajouter à `providers/meet.py` avant de
pouvoir l'exécuter. La partie à écrire maintenant est celle qui se vérifie SANS abonnement —
construction de l'abonnement Events, lecture des évènements, correspondance
`conferenceRecord` → occurrence.

## 7-quinquies. Briques codées SANS compte (2026-07-27) — état exact

Teams et Meet exigent des abonnements payants que nous n'avons pas encore. Plutôt qu'attendre,
tout ce qui se vérifie sans compte a été écrit et testé. Le tableau dit ce qui EXISTE et,
surtout, ce qui reste — un module écrit n'est pas un connecteur qui marche.

| Module | Rôle | Vérifié par |
|---|---|---|
| `teams_graph.py` | abonnements Graph : construction, durées, lecture des notifications, cycle de vie | tests, formes relevées sur la doc |
| `graph_validation.py` | identité de l'émetteur des notifications (`appid` v1 / `azp` v2) | tests |
| `meet_events.py` | abonnements Workspace Events, lecture des messages Pub/Sub (CloudEvents binaire) | tests |
| `subscription_renewal.py` | **brique COMMUNE** de renouvellement — Graph et Workspace partagent la même règle, deux politiques | tests |
| `subscription_keeper.py` | l'ordonnanceur qui l'APPLIQUE : plusieurs abonnements, échecs encaissés, délais respectés | tests, **sans asyncio ni horloge réelle** |
| `oauth.py` (existant) | jetons Zoom / Graph / Google — **déjà là** : la marge de renouvellement y est passée à 5 min, un jeton qui expire pendant un téléchargement faisant échouer l'ingestion | tests |
| `jwt_crypto.py` | **vérification** RS256 des `validationTokens` Graph (liste blanche d'algorithmes) | tests, **avec paire de clés engendrée sur place** |
| `pubsub_pull.py` | interrogation et acquittement Pub/Sub en REST, et **quoi acquitter** | tests, formes relevées sur la référence REST |

#### ⚠ Une erreur commise et corrigée : un module OAuth écrit en double

Un `oauth_tokens.py` a été écrit — demandes de jeton Graph et Google, échéances,
rafraîchissement — avant qu'un contrôle de la structure du paquet ne révèle que **`oauth.py`
faisait déjà tout cela**, était utilisé par les providers réels et couvert par des tests. La
moitié « signature de l'assertion Google » de `jwt_crypto.py` réinventait de surcroît ce que
`google-auth`, dépendance déjà déclarée, fait pour nous.

Le module en double a été supprimé et `jwt_crypto.py` réduit à ce que rien ne couvrait : la
VÉRIFICATION des jetons Microsoft. Seule amélioration retenue au passage — la marge de
renouvellement d'`oauth.py`, portée d'une à cinq minutes : elle datait d'un temps où les jetons
ne servaient qu'à de petits appels, alors qu'ils servent maintenant à télécharger des
enregistrements.

Leçon de méthode : **inventorier le paquet AVANT d'écrire**, pas après. Le trou d'AGENTS.md —
`connector_service/` n'y figurait pas du tout — n'excuse rien, mais il l'a rendu facile ; il est
désormais comblé, et la structure y est vérifiée par un contrôle exécutable dans les deux sens.

#### Trois pièges que l'ordonnanceur existe pour éviter

Ils n'ont rien de théorique — ce sont les trois façons dont une boucle de renouvellement échoue
en production, et aucune ne se voit avant que les évènements ne cessent d'arriver :

1. **L'échec d'un abonnement arrête les autres.** Le plus banal des bugs de boucle et le plus
   coûteux : un locataire en panne fait expirer les abonnements de tous les autres.
2. **La temporisation n'est pas respectée**, et la boucle martèle un service déjà en difficulté
   jusqu'à se faire limiter — au moment précis où l'échéance approche.
3. **Deux opérations trop rapprochées sur le même abonnement.** Graph interdit explicitement
   d'enchaîner `/reauthorize` et `PATCH` en moins de dix minutes.

#### Deux choix explicites dans la couche Pub/Sub

1. **API REST plutôt que `google-cloud-pubsub`.** La bibliothèque officielle apporte gRPC et
   son arbre de dépendances pour un « streaming pull » dimensionné pour des milliers de
   messages par seconde. Nous en attendons quelques-uns par réunion : une interrogation
   périodique en REST suffit, se teste sans mock de gRPC, et n'ajoute **aucune dépendance**.
2. **Un message ILLISIBLE est acquitté, un traitement ÉCHOUÉ ne l'est pas.** Les deux cas se
   ressemblent et appellent l'inverse l'un de l'autre : un message illisible ne le deviendra
   jamais et bloquerait la file par redélivrances sans fin, alors qu'un téléchargement raté
   peut réussir au prochain essai — l'acquitter perdrait l'enregistrement pour de bon.

Ce qui manque encore, et qui exige un compte :

- **les appels réseau eux-mêmes** : créer l'abonnement, exécuter l'interrogation Pub/Sub,
  télécharger le média ;
- **le branchement sur l'ingestion** : de l'évènement à un job TranscrIA ;
- **toute validation réelle.** Rien de la liste ci-dessus n'a jamais vu une vraie plateforme.

Le découpage n'est pas gratuit : il place la totalité des RÈGLES (durées, revendications,
formes, algorithmes acceptés) du côté testable, et ne laisse au réseau que le transport. C'est
ce qui a permis de corriger trois erreurs — durée maximale d'abonnement, réponse 202 sur
`clientState` invalide, dépassement du calcul de temporisation — avant qu'elles ne coûtent une
session de débogage contre un service distant.

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
| Export/enregistrement (L1 + contribution Track-Egress) | [`livekit/egress`](https://github.com/livekit/egress) (Apache-2.0) | Adapter (egress par piste → connecteur) |
| Réf. concrète LiveKit+STT | [`atyenoria/livekit-whisper-transcribe`](https://github.com/atyenoria/livekit-whisper-transcribe) | Étudier |
| **Partiels stables (`partial`→`provisional`, couture 1)** | [`ufal/whisper_streaming`](https://github.com/ufal/whisper_streaming) (MIT) — politique **local-agreement** (ne fige un mot que si deux passes successives s'accordent) | **Adapter local-agreement UNIQUEMENT** aux backends à fenêtres glissantes sans marqueurs natifs partial/final |
| **Micro navigateur + WS live (0-bis livré ; live rolling futur, dépend de L0)** | [`WhisperLiveKit`](https://github.com/QuentinFuxa/WhisperLiveKit) (Q. Fuxa) — backend **FastAPI WebSocket** + front **HTML/JS capture micro** + diarisation live | **Forker le front micro** + le patron serveur WS async |
| **Meet live** (R1, recherche) | **Meet Media API officielle** (Developer Preview) ; [`Vexa`](https://github.com/Vexa-ai/vexa) (Apache-2.0) ou Attendee = **repli expérimental** seulement (Vexa ~80 % redondant avec TranscrIA) | Évaluer la Media API ; à défaut, **extraire SEULEMENT** la capture Meet (`capture-bridge.ts`), **pas** la plateforme |
| Alt. Recall.ai open-source | [`Attendee`](https://github.com/attendee-labs/attendee) (MIT), Meet/Teams/Zoom browser-auto | Étudier (même classe que Vexa) |
| Capture **bot-free** (audio système, **client-side**) | [`Meetily`](https://github.com/Zackriya-Solutions/meetily) (MIT, Tauri/Rust, whisper.cpp/Parakeet) | Patron pour la capture onglet/système côté poste |
| **Zoom RTMS (L2)** | [`zoom/rtms-samples`](https://github.com/zoom/rtms-samples) (JS/Py/Go/Java/.NET, exemples transcription) + [`zoom/rtms`](https://github.com/zoom/rtms) (bindings Py/Node/Go). RTMS = **WebSocket standard**, PCM par participant | **Forker un sample** |
| Réf. Zoom RTMS produit complet | Arlo (Zoom Apps RTMS : résumés, actions) | Étudier |
| **Teams post-réunion (Graph)** | Microsoft Graph : `getAllTranscripts`/`getAllRecordings`, change notifications chiffrées + [`microsoftgraph/nodejs-webhooks-sample`](https://github.com/microsoftgraph/nodejs-webhooks-sample) (MIT, ~80 % du webhook+crypto RSA→AES) | **Forker le sample** → fetch MP4/VTT → job. ⚠ consentement admin, réunions calendaires |
| **Meet post-réunion (officiel)** | [Google Meet REST API v2](https://developers.google.com/workspace/meet/api) — `conferenceRecords.transcripts/recordings` + Drive API (artefacts du Drive de l'organisateur) | Utiliser (poll `FILE_GENERATED` → job) |
| **Serveur STT live Kyutai** | [`suitenumerique/meet-kyutai-moshi-stt`](https://github.com/suitenumerique/meet-kyutai-moshi-stt) (MIT, Docker `moshi-server`) — WS `/api/asr-streaming`, `stt-1b-en_fr`, 0,5 s, batché 64. Client = `livekit-plugins-kyutai-lasuite` | **Réutiliser l'image** (⚠ build candle sm_120 à valider) |
| Visio (source) | [`suitenumerique/meet`](https://github.com/suitenumerique/meet) (LiveKit + Django + React, MIT) | Lire pour brancher |

### Décision : QUOI forker exactement (cibles vérifiées dans le code, 2026)

Axe déterminant : **API officielle / natif** (stable, souverain) **vs automation
navigateur** (fragile, CGU). On privilégie l'officiel — y compris Meet-live via la
**Meet Media API officielle** (preview) ; le bot navigateur n'est qu'un **repli
expérimental**. Cibles **lues dans le code source** :

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

**Confirmé par le code** (nuance importante — ADR-001 D1) : **Vexa**
(`TRANSCRIPTION_SERVICE_URL`) peut utiliser directement la **façade OpenAI Audio** comme
service STT externe. **Visio post-réunion**, lui, utilise un **contrat de tâche**
(`/api/v1/tasks/` → WhisperX) et exige un **adaptateur dédié** (pas un swap d'URL) ; le
**worker live** de Visio, en revanche, peut être raccordé à un backend STT live compatible
avec son interface. Par ailleurs **~80 % de Vexa** (transcription, stockage, gateway, UI)
est **redondant** avec TranscrIA → on n'en prend que le bout capture-Meet, et **seulement
si besoin** (Meet post-réunion étant officiel).

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
| Latence live insuffisante (RTF du backend live retenu) | Mesurer tôt (bench streaming réel) ; le live est un *suivi*, pas le verbatim ; fenêtrer/paralléliser si besoin |
| VRAM (moteur live + 35B) sur machine serrée | Reclaim STT↔LLM existant ; le connecteur peut être sur une autre machine |
| API plateforme non documentée/mouvante (Visio bêta) | Construire sur le socle LiveKit + interfaces explicites, pas sur une API de prod instable |
| Dépendance propriétaire (Zoom/Teams) | Visio (souverain, open) d'abord ; Zoom/Teams additifs |
| Qualité live surestimée par les métriques | Règle « lire, pas scorer » (§6) |

## 11. Stratégie de test

- **Tests transversaux communs + suites par capacité** (ADR-001 D3, §Mesure) : enveloppe
  commune + cas durs partagés, puis fonctionnels par capacité déclarée — des providers
  factices en plusieurs combinaisons de capacités servent de référence.
- **Provenance** : golden batch inchangés (couture 1 additive) ; sur une session simulée,
  **publication de la révision `canonical` → activation comme révision par défaut →
  conservation de la révision `live`** (pas d'écrasement).
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

- **Chaîne STT live/rapide** : backend faible latence choisi par config (candidats
  Nemotron-streaming / Kyutai / Voxtral realtime) pour le suivi en direct.
- **Chaîne STT référence/finale** : moteur du pipeline (cohere/qwen3) pour le
  verbatim de référence.
- **Provenance** : état de production et d'autorité d'un segment (`partial` →
  `provisional` → `final_live` → `canonical`).
- **Adaptateur de plateforme** : implémente les interfaces de capacités
  (`ArtifactProvider`/`LiveMediaProvider`/…) qu'une plateforme supporte + déclare
  `ProviderCapabilities`. (Remplace l'ancien `MeetingProvider` monolithique.)
- **AudioFrame** : unité audio normalisée par participant (voir §4).
- **Egress (LiveKit)** : export d'un enregistrement (composite ou par piste).
- **RTMS (Zoom)** : Realtime Media Streams — audio/events par participant.

## 13. Checklist « rien oublié »

- [ ] **Stratégie** : forker la capture, ne RIEN réécrire ; effort sur le feeder.
- [ ] **API officielle / SFU natif** privilégié (Meet-live = Meet Media API officielle) ;
      bot-navigateur = **repli expérimental** seulement, isolé.
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
- [ ] **Post-réunion officiel des 4** (Visio **adaptateur de tâche**, Zoom Recording, Teams Graph, Meet REST API).
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
