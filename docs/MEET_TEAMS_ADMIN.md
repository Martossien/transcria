# Google Meet et Microsoft Teams avec TranscrIA — guide de l'administrateur

> Vérifié contre la documentation officielle ET **éprouvé sur un vrai Workspace le
> 2026-08-01**, réunion à deux participants comprise : agenda → salle pré-réglée →
> enregistrement automatique → évènement → Drive → job attribué à l'organisateur → arrêt au
> workflow humain (résumé de contrôle) → compte rendu. Le connecteur est `validated`.
>
> Les encadrés ⚠ signalent des pièges **réellement rencontrés** ce jour-là. Aucun ne produit
> de message clair : ils se traduisent par un compte rendu qui n'arrive jamais.
>
> ⚠️ **Teams n'a jamais vu la vraie plateforme** — il exige un abonnement M365 et deux URL
> HTTPS publiques. Sa section prépare le terrain ; l'épreuve réelle reste à faire.

## Comment ça marche

```text
Google Meet
   │  début/fin de conférence, participants, enregistrement prêt
   ▼
Google Workspace Events API
   ▼
Sujet Pub/Sub  ──►  Abonnement PULL  ──►  TranscrIA
                                            ├─ API Meet   : conférence, participants
                                            ├─ API Drive  : le média
                                            ├─ Admin SDK  : utilisateurs (facultatif)
                                            └─ Agenda     : réunions à venir (facultatif)
```

Aucun bot n'entre dans la réunion, et **aucun port entrant n'est ouvert** : tout est sortant.
C'est ce qui rend cette voie acceptable là où un webhook est refusé.

## 1. Google Meet

### 1.1 Prérequis

- une organisation **Google Workspace** (un compte Gmail personnel ne suffit pas) ;
- un compte **administrateur** Workspace ;
- un projet **Google Cloud** ;
- l'enregistrement Meet inclus dans l'édition (Business **Standard** ou supérieure).

> Une clé JSON de compte de service est un **secret**. Jamais dans Git, jamais dans une
> capture d'écran. TranscrIA la dépose en 0600 hors configuration ; seul son chemin est
> stocké.

### 1.2 Activer les API (console Cloud)

**API et services → Bibliothèque**, puis activer :

| API | Service | Nécessaire à |
|---|---|---|
| Google Meet REST API | `meet.googleapis.com` | conférences, participants, réglages |
| Google Workspace Events API | `workspaceevents.googleapis.com` | les abonnements |
| Cloud Pub/Sub API | `pubsub.googleapis.com` | la file |
| Google Drive API | `drive.googleapis.com` | télécharger l'enregistrement |
| Admin SDK API | `admin.googleapis.com` | *facultatif* — résolution efficace des utilisateurs |
| Google Calendar API | `calendar-json.googleapis.com` | *facultatif* — pré-réglage des réunions à venir |

> ⚠ **Activer une API et déléguer sa portée sont DEUX gestes, dans deux consoles.**
> Vécu : la portée `admin.directory.user.readonly` était correctement déléguée, et l'appel
> répondait `403 — Admin SDK API has not been used in project … before or it is disabled`.
> Le message oriente vers les droits ; la cause était l'API jamais activée. TranscrIA
> distingue désormais les deux cas dans ses messages.

### 1.3 Compte de service et clé

**IAM et administration → Comptes de service** → créer (ex. `meet-connector`) → clé **JSON**.

Relevez deux choses, qui ne servent PAS au même endroit :

| Valeur | Où elle va |
|---|---|
| l'**adresse** `meet-connector@projet.iam.gserviceaccount.com` | droits IAM (Pub/Sub) |
| l'**ID client NUMÉRIQUE** (21 chiffres) | délégation dans la console Admin |

> ⚠ **Ne confondez pas ces deux identifiants, ni avec l'utilisateur à impersonner.**
> Vécu trois fois dans la même journée : l'ID numérique saisi dans le champ « utilisateur à
> impersonner » de TranscrIA a produit `invalid_request — Invalid principal`, un message qui
> ne désigne rien. Le portail refuse maintenant toute valeur sans `@` en expliquant à quoi
> sert chaque identifiant.

### 1.4 Sujet et abonnement Pub/Sub

1. **Pub/Sub → Sujets** → créer (ex. `meet-events`) ;
2. **Pub/Sub → Abonnements** → créer, type **Pull** (surtout pas « push » : c'est ce qui
   évite toute ouverture de pare-feu).

Conservez le **chemin complet** de l'abonnement :

```text
projects/mon-projet/subscriptions/meet-events-pull
```

> ⚠ **Le nom court ne marche pas.** La console affiche `meet-events-pull` ; l'API exige la
> forme entière `projects/…/subscriptions/…` et répond `404` sans dire ce qui manque.
> TranscrIA refuse le nom court avant d'appeler.

### 1.5 Les deux droits IAM — les pannes muettes n°1 et n°2

| Rôle | Sur quelle ressource | Pour qui |
|---|---|---|
| **Pub/Sub Publisher** (`roles/pubsub.publisher`) | le **SUJET** | `meet-api-event-push@system.gserviceaccount.com` |
| **Pub/Sub Subscriber** (`roles/pubsub.subscriber`) | l'**ABONNEMENT** | votre compte de service |

> ⚠ Sans le premier, l'abonnement se crée, l'API répond 200… et **la file reste vide à
> jamais**. Sans le second, l'interrogation est refusée. Aucun test d'authentification ne
> peut les voir — le verdict de TranscrIA le rappelle explicitement.

### 1.6 Délégation à l'échelle du domaine

**console Admin → Sécurité → Contrôle des API → Délégation au niveau du domaine**, avec
l'**ID client numérique**, et les portées séparées par des **virgules sans espace ni retour
à la ligne** :

```text
https://www.googleapis.com/auth/meetings.space.readonly,https://www.googleapis.com/auth/drive.readonly,https://www.googleapis.com/auth/meetings.space.settings,https://www.googleapis.com/auth/admin.directory.user.readonly,openid,https://www.googleapis.com/auth/calendar.events.readonly
```

| Portée | Sert à | Sans elle |
|---|---|---|
| `meetings.space.readonly` | espaces, conférences, **abonnements** | rien ne fonctionne |
| `drive.readonly` | télécharger l'enregistrement | on sait qu'il existe, on ne l'a pas |
| `meetings.space.settings` | régler une salle en **auto-enregistrement** | un humain doit cliquer « Enregistrer » |
| `admin.directory.user.readonly` | adresse → identifiant, **une authentification pour tous** | repli OpenID, une par personne |
| `openid` | même chose, par personne | rien, si l'annuaire est accordé |
| `calendar.events.readonly` | pré-régler les réunions à venir | pas d'auto-enregistrement sans Business Plus |

> ⚠ **N'ajoutez JAMAIS `pubsub` à la délégation.** La file appartient au projet Cloud, ce
> n'est pas une donnée d'utilisateur — son droit vient de Cloud IAM (§1.5). Et comme Google
> **refuse en bloc** toute demande dont une seule portée n'est pas déléguée, l'exiger ici
> fait échouer une configuration pourtant correcte.

> ⚠ **Comptez ~3 minutes de propagation** (Google annonce « jusqu'à 24 h »). Mesuré deux
> fois : 180 secondes. Un refus juste après l'enregistrement ne signifie pas une erreur de
> saisie — rouvrez la ligne, comptez les pastilles, et retentez.

### 1.7 Côté TranscrIA

**Administration → Connecteurs → fiche Meet** : téléverser la clé JSON, renseigner
l'utilisateur à impersonner (une **adresse** du domaine) et l'abonnement **pleinement
qualifié**, puis **Tester la connexion**.

En cas de refus, le test désigne la cause au lieu de la laisser deviner : il rejoue la
demande **portée par portée** (`unauthorized_client` → laquelle manque) et **sans
impersonation** (`Invalid principal` → est-ce l'utilisateur représenté ou le compte de
service ?).

Ensuite, dans le panneau **Couverture Meet** :

- **les utilisateurs sont couverts automatiquement** — un abonnement par personne, déduit des
  comptes du portail portant une adresse du domaine. Rien à saisir, et **toutes** leurs
  réunions sont ingérées ;
- les **salles particulières** (une salle physique, un canal permanent) se déclarent à part.

> ⚠ **L'adresse du compte TranscrIA doit être celle du domaine Meet.** C'est elle qui
> déclenche l'abonnement ET l'attribution du compte rendu. Sans elle, le job existe mais
> appartient au compte de service : l'utilisateur ne le voit nulle part.

### 1.8 Faire enregistrer les réunions — quatre voies

| Voie | Effort par réunion | Effort admin | Édition |
|---|---|---|---|
| **A.** réglage d'organisation « enregistrées par défaut » | aucun | un clic, une fois | Business **Plus**+ |
| **B.** pré-réglage par l'**Agenda** (TranscrIA) | aucun | aucun | Business Standard |
| **C.** enregistrement de conformité | aucun, non désactivable | par groupe | module Assured Controls |
| **D.** l'hôte coche « enregistrer » | un clic | aucun | Business Standard |

La voie **A** (*Apps → Google Workspace → Google Meet → Paramètres vidéo → Enregistrement
automatique*) est la plus simple quand l'édition la permet. Sinon la voie **B** : TranscrIA
lit les réunions à venir de chaque utilisateur couvert et règle leurs salles en
`autoRecordingGeneration = ON` **avant** qu'elles commencent — même résultat, sans changer
d'édition.

> Vérifiez que l'enregistrement automatique est compatible avec vos règles internes et que
> les participants en sont informés. Meet affiche un bandeau, mais la conformité ne s'arrête
> pas là.

### 1.9 Deux délais mesurés, à ne pas confondre avec des pannes

- l'enregistrement passe par `ENDED` **dès l'arrêt**, puis `FILE_GENERATED` **5 à 6 minutes
  plus tard** ; l'évènement ne part qu'au second. Conclure « pas d'enregistrement » à la fin
  de la conférence est **systématiquement faux** ;
- un message Pub/Sub **non acquitté est redélivré** et peut **masquer le suivant** dans une
  interrogation : on croit l'évènement perdu alors qu'il attend derrière. Vécu.

### 1.10 Durée de vie des abonnements

Un abonnement Workspace Events vit **sept jours au maximum**, et Google **supprime
définitivement** celui qui expire : *« you can't renew or reactivate it »*. Le service Meet
de TranscrIA les renouvelle tout seul (marge d'un jour).

> ⚠ Le silence qui suit une expiration ressemble trait pour trait à « aucune réunion n'a été
> enregistrée » — une semaine après que tout fonctionnait. C'est la panne la plus coûteuse de
> ce connecteur, parce qu'elle survient longtemps après la validation.

### 1.11 Ce que TranscrIA ne peut PAS faire, et pourquoi

- **Pas de bot dans la réunion.** L'API média de Meet est en *Developer Preview* et exige que
  **tous les participants** soient inscrits au programme : inutilisable. Meet est donc
  post-réunion, contrairement à Jitsi, Visio et Zoom.
- **Pas de piste par participant.** Google ne livre qu'un fichier **mixé**. Les voix sont
  donc séparées par notre diarisation, pas par la plateforme. TranscrIA compense en
  transmettant le **nombre exact de participants** (ce qui évite qu'une voix unique soit
  coupée en deux) et leurs **noms**, proposés à la validation.
- **Pas de surveillance à l'échelle du domaine.** Un abonnement vise un utilisateur ou un
  espace, jamais une organisation — d'où un abonnement par personne, posé automatiquement.

### 1.12 Vérifications avant d'appeler à l'aide

- le bon projet Cloud, et les API **activées** (§1.2) ;
- l'abonnement **pull** existe, chemin **complet** relevé ;
- `meet-api-event-push@system.gserviceaccount.com` → **Publisher** sur le **sujet** ;
- votre compte de service → **Subscriber** sur l'**abonnement** ;
- la délégation porte l'**ID numérique** et les portées exactes, **sans `pubsub`** ;
- l'utilisateur à impersonner est une **adresse** valide du domaine ;
- le compte TranscrIA porte cette même adresse ;
- l'heure du serveur est juste (une horloge décalée invalide les assertions signées).

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
