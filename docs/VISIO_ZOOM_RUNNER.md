# Visio puis Zoom sur le parcours complet — cadrage (lots V1/V2)

> **Statut : validé par l'utilisateur le 2026-07-31 — LOTS V0, V1 ET V2 LIVRÉS le jour
> même** (V0+V1 : 598ca74 ; V2 : commit suivant). Restent les GATES RÉELS avec
> l'utilisateur : Visio (accès exploitant LiveKit — URL + clé/secret), Zoom compte
> gratuit (app Meeting SDK, réunions 40 min). DoD des gates au tableau des lots.
> Les TRANSPORTS des deux plateformes sont ÉPROUVÉS en réel (catalogue `status: validated` :
> Visio/LiveKit natif 2026-07-26 ; Zoom Meeting SDK compte gratuit — 9 879 trames, locuteur
> nommé). Ce chantier ne refait pas les transports : il les branche sur le parcours COMPLET
> livré par les vagues 3-5 — planification UI → claim runner → bot → **pistes séparées v2**
> → suivi en direct provisoire → étape 5. Référence de vérification : `~/reference/attendee`
> (bots de production Zoom/Meet/Teams — vérifier nos choix, ne pas inventer, comme Jibri
> pour le fragment d'URL Jitsi).

## Le constat (lu dans le code, pas supposé)

Le bot NAVIGATEUR (`bot/cli.py`, Jitsi) est le seul à porter le parcours complet. Les trois
manques, identiques pour Visio (bot inexistant) et Zoom (`bot/zoom_sdk.py`) :

1. **Capture par piste** : pas de `RecordingTee` → pas de mixage disque, pas de
   `tracks/<pid>.wav`, pas de manifeste v2, pas d'ingestion vers le job planifié
   (`TRANSCRIA_JOB_ID` ignoré).
2. **Événements et direct** : pas de `BOT_EVENTS=json` (états sur la carte du job) ni de
   `{"bot_caption": …}` (panneau « Suivi en direct — provisoire »).
3. **Runner** : `commands.docker_argv` ne relaie pas les credentials MACHINE
   (`LIVEKIT_*`, `ZOOM_*`) comme il relaie `JITSI_XMPP_*` ; pas d'image `visio`.

L'atout structurel : LiveKit et le SDK Zoom livrent l'audio **PAR PARTICIPANT avec
identité** (`AudioFanIn` / `zoom_sdk_demux_source` existants) — les pistes v2 y sont plus
propres que sur Jitsi (aucun pont JS).

## Décisions

- **D-V1 — un socle commun AVANT les deux lots** : la plomberie « frames démultiplexées →
  tee → manifeste → ingest → events/captions » est LA MÊME pour tout bot sans navigateur.
  Elle est extraite en `connector_service/bot/_workflow.py` (fonctions réutilisant
  l'existant de `cli.py` : `_ingest_recording`, `_json_event_emitter`,
  `_json_caption_emitter` — déplacés, pas dupliqués ; `cli.py` les réimporte). Toute
  correction future profite aux trois bots.
- **D-V2 — Visio d'abord** (`bot/visio.py`, image `transcria-visio:latest`,
  `Dockerfile.visio` SANS navigateur : python + `livekit`) : meeting_ref = URL de salle
  (`https://…/<room>`) ou nom brut — parse PUR testé ; jeton bot forgé par
  `livekit_access_token` (participant caché, `can_subscribe`) avec `LIVEKIT_URL/API_KEY/
  API_SECRET` de l'ENVIRONNEMENT DU RUNNER (propriété machine, patron `JITSI_XMPP_*` —
  la voie validée est celle de l'EXPLOITANT de l'instance).
- **D-V3 — Zoom ensuite** (`bot/zoom_sdk.py` complété, image déjà déclarée au runner) :
  tee 32 kHz (le SDK n'offre pas 16 kHz — le tee accepte déjà `sample_rate_hz`),
  `ZOOM_CLIENT_ID/SECRET/PASSCODE` relayés par le runner ; contrainte compte gratuit =
  40 min/réunion (suffisant pour les gates, dit dans la doc).
- **D-V4 — mêmes codes de sortie, même machine d'états** : rien à changer côté serveur ni
  démon — c'était le contrat des vagues 3-4, il tient.
- **Hors périmètre** : revue sécurité (Opus 5 — s'ajoute au périmètre existant), achats
  Teams/Meet, sous-salles Zoom (salle réelle requise).

## Lots

| Lot | Contenu | DoD |
|---|---|---|
| **V0 — socle** | `bot/_workflow.py` extrait de `cli.py` (ingest/events/captions), tests déplacés/verts, bot Jitsi inchangé à l'octet près (mêmes lignes stdout) | suite + E2E verts, gate Jitsi non requis (aucun comportement changé) |
| **V1 — Visio** | `bot/visio.py` + parse PUR + `Dockerfile.visio` + `DEFAULT_IMAGES`/`_DOCKERFILES`/matrix GHCR + relais `LIVEKIT_*` + catalogue (steps runner) | gate réel : planifier une salle Visio depuis l'accueil → bot caché entre → pistes par participant → étape 5 nommée → SRT chevauchements |
| **V2 — Zoom** | tee+ingest+events/captions dans `bot/zoom_sdk.py`, relais `ZOOM_*`, doc 40 min | gate réel compte gratuit : même parcours ; sortie propre AVANT 40 min |

Discipline inchangée : gates statiques + suite + E2E réel avant CHAQUE push ; images bot
rebuildées ; gates réels avec l'utilisateur aux DoD V1/V2.
