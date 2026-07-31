# Google Meet et Microsoft Teams avec TranscrIA — guide de l'administrateur

> Vérifié contre la documentation officielle **le 2026-07-31** :
> [Meet — S'abonner aux événements](https://developers.google.com/workspace/events/guides/events-meet)
> (page à jour du 2026-04-20), [Meet REST API v2](https://developers.google.com/workspace/meet/api/guides/overview),
> [Graph — notifications d'enregistrements et transcriptions](https://learn.microsoft.com/en-us/graph/teams-changenotifications-callrecording-and-calltranscript)
> (page à jour du 2026-06-29).
>
> ⚠️ **Ces deux connecteurs sont `implemented`, pas `validated`** : le code existe et passe
> les tests, mais n'a jamais vu la vraie plateforme — il exige un abonnement payant. Ce
> guide prépare le terrain ; l'épreuve réelle reste à faire.

Contrairement à Jitsi/Visio/Zoom (un bot rejoint la réunion), Meet et Teams sont des
connecteurs **post-réunion** : la plateforme prévient qu'un enregistrement existe,
TranscrIA le télécharge et le passe dans son propre pipeline. Aucun participant
supplémentaire dans la réunion.

## 0. Lequel commencer ? — **Meet**, même s'il coûte plus cher

| | Meet | Teams |
|---|---|---|
| Abonnement | Google Workspace Business Standard (~14 $/mois, **essai 14 jours**) | Microsoft 365 Business (~7 $/mois) |
| Ouverture de pare-feu | **AUCUNE** (on interroge une file Pub/Sub) | **DEUX URL HTTPS publiques** (notifications + cycle de vie) |
| Acceptabilité DSI | élevée (rien d'entrant) | à négocier |

Meet est donc validable même si l'ouverture réseau traîne — et c'est le seul point qui
justifie l'ordre.

## 1. Google Meet

### Ce que l'admin Google fait (une fois)

1. **Console Google Cloud** → créer (ou choisir) un projet → **activer** les API
   « Google Meet API » et « Google Workspace Events API », plus « Cloud Pub/Sub ».
2. **Créer un compte de service** et générer une clé JSON (elle restera sur le serveur
   TranscrIA, jamais dans le dépôt).
3. **Délégation à l'échelle du domaine** (console Admin Workspace → Sécurité → Contrôle
   des API → Délégation) : autoriser le *client ID* du compte de service sur les
   portées Meet nécessaires (lecture des espaces et des artefacts) et Drive en lecture.
4. **Créer un sujet Pub/Sub** et un abonnement de type **pull** (surtout pas « push » :
   c'est ce qui évite toute ouverture de pare-feu).
5. ⚠️ **La panne muette n°1** : accorder le rôle **Pub/Sub Publisher** au compte
   `meet-api-event-push@system.gserviceaccount.com` **sur le sujet**. Sans lui,
   l'abonnement se crée, l'API répond 200… et la file reste vide **à jamais**.

### Ce que TranscrIA écoute

Les événements officiels (vérifiés) : `google.workspace.meet.recording.v2.fileGenerated`
(le signal utile — l'enregistrement est prêt), `…recording.v2.started/ended`,
`…conference.v2.started/ended`, `…participant.v2.joined/left`. L'artefact est ensuite lu
par l'API Meet REST **v2** (`conferenceRecords/…/recordings/…`) et récupéré sur le Drive
de l'organisateur.

### Côté TranscrIA

Administration → Connecteurs → fiche **Meet** : renseigner les identités demandées
(chemin de la clé JSON du compte de service, utilisateur à impersonner, sujet et
abonnement Pub/Sub), puis **Tester la connexion** — le portail vérifie l'authentification
et l'accès à l'abonnement **sans réunion**.

## 2. Microsoft Teams

### Ce que l'admin Microsoft fait (une fois)

1. **Entra ID (Azure AD)** → *Inscriptions d'applications* → nouvelle application →
   créer un **secret client**.
2. **Permissions d'API** : ajouter la permission **applicative**
   **`OnlineMeetingRecording.Read.All`** (c'est celle des enregistrements de réunions
   planifiées ; `OnlineMeetingTranscript.Read.All` n'est utile que si l'on veut aussi la
   transcription faite par Teams — ce n'est pas notre cas), puis **consentement
   administrateur**.
3. ⚠️ **La panne muette n°2** : créer une **politique d'accès applicatif** en PowerShell
   (`New-CsApplicationAccessPolicy` + `Grant-CsApplicationAccessPolicy`). Sans elle,
   l'application s'authentifie parfaitement… et ne voit les artefacts d'**aucun**
   organisateur.
4. **Deux URL HTTPS publiques** à fournir : celle des notifications et celle du **cycle
   de vie**. Un tunnel (`cloudflared`, `ngrok`) suffit pour éprouver.

### Règles Graph que TranscrIA applique déjà (vérifiées à la source)

- Au-delà d'**1 heure** d'expiration, `lifecycleNotificationUrl` est **obligatoire** —
  sans lui, la création d'abonnement échoue. D'où la seconde URL.
- Durée de vie maximale d'un abonnement : **4320 minutes** (3 jours) → renouvellement
  automatique par le service.
- Si l'admin du tenant coupe l'accès Graph aux **transcriptions**, la création/le
  renouvellement échoue en `403` avec le code interne `GraphAccessToTranscriptsDisabled`
  (les **enregistrements** ne sont pas affectés) — TranscrIA branche sur ce code, pas sur
  le message.
- Notifications **avec données chiffrées** : un certificat est requis ; sans données de
  ressource, aucun certificat n'est nécessaire.
- Limite tenant : 10 000 abonnements Teams cumulés.

### Côté TranscrIA

Administration → Connecteurs → fiche **Teams** : renseigner locataire, client, secret,
secret partagé des notifications et l'URL publique, puis **Tester la connexion** — le
portail obtient un jeton applicatif auprès d'Entra ID et le dit en clair (sans réunion,
sans abonnement).

## 3. Ce qu'il restera à faire, une fois les comptes en place

Brancher les appels réseau derrière les points d'injection déjà spécifiés (§7-quinquies de
`TEMPS_REEL_REUNIONS.md`), puis l'épreuve réelle : une réunion enregistrée de bout en bout
→ notification → téléchargement → job TranscrIA. Tant que ce n'est pas fait, la fiche
reste `implemented` : **ne pas s'appuyer dessus en production**.
