# Bot de réunion — installation, lancement, exploitation

Le bot rejoint une visioconférence comme un participant, capte l'audio **par locuteur** et
le transcrit via TranscrIA. C'est la **voie de secours** du chantier temps réel : contrairement
aux connecteurs officiels (qui reçoivent des webhooks et exigent donc un port ouvert), le bot
n'établit que des connexions **sortantes** — il traverse un pare-feu ou un proxy d'entreprise
sans ouverture entrante.

Deux bots coexistent, parce que les plateformes n'ouvrent pas la même porte :

| Bot | Plateformes | Comment il entre | Image |
|---|---|---|---|
| **Navigateur** | Jitsi (publique ou auto-hébergée) | Chromium headless, capture WebRTC dans la page | `Dockerfile.bot` |
| **SDK natif Zoom** | Zoom | Meeting SDK officiel pour Linux, sans navigateur | `Dockerfile.zoom-sdk` |

Pourquoi deux : **Zoom refuse l'automatisation de son client Web** (reCAPTCHA, constaté au
gate) et recommande explicitement le SDK natif pour un bot headless Linux. Ce n'était donc pas
un défaut à corriger dans le pilote navigateur, mais la mauvaise porte d'entrée. Le SDK
apporte en prime ce que le navigateur ne pouvait pas donner : **les locuteurs sont nommés**.

Les deux partagent l'aval (façade STT, session live, provenance) et **les mêmes codes de
retour** : l'orchestration n'a pas à les distinguer.

## 0. Au quotidien : une seule commande

`scripts/bot.sh` masque entièrement Docker — il choisit l'image, construit ce qui manque,
transmet les identifiants et décide du mode réseau :

```bash
./scripts/bot.sh zoom  "123 456 7890"
./scripts/bot.sh zoom  "https://us05web.zoom.us/j/1234567890?pwd=…"   # le lien suffit
./scripts/bot.sh jitsi https://jitsi.exemple/ma-salle
```

Les réglages vivent dans `~/.transcria-bot.env` (hors du dépôt) :

```ini
TRANSCRIA_URL=http://127.0.0.1:7870
TRANSCRIA_TOKEN_FILE=/chemin/vers/jeton.txt
BOT_LANGUAGE=fr
ZOOM_CLIENT_ID=…
ZOOM_CLIENT_SECRET=…
```

Deux comportements qui évitent des pièges classiques :

- si `TRANSCRIA_URL` désigne **cette machine**, le script partage le réseau de l'hôte — sans
  quoi le conteneur ne joindrait pas la façade et la transcription resterait vide ;
- le code secret **porté par un lien prime** sur celui de la configuration : un code de
  configuration vaut pour toutes les réunions, un lien n'en désigne qu'une.

Le reste de ce document décrit ce qui se passe dessous, et n'est utile qu'en cas de problème.

---

## 1. Ce qu'il faut savoir avant

| Point | Réalité |
|---|---|
| Ressources | Aucun GPU, aucune base de données. Une machine banale suffit. |
| Réseau | Sortant uniquement (HTTPS + média WebRTC). Aucun port à ouvrir. |
| Modèle d'exécution | **Un conteneur par réunion**, éphémère : il meurt à la fin. |
| Isolation | Image séparée : l'image applicative par défaut n'embarque pas de navigateur. |
| Discrétion | Le bot rejoint **micro et caméra coupés** et ne demande jamais l'accès au micro. |

Le bot est **visible** des participants (il apparaît dans la liste) : c'est volontaire et
cohérent avec une politique de consentement.

---

## 2. Installation du bot navigateur

> Les sections 2 à 5 concernent le **bot navigateur** (Jitsi). Pour Zoom, allez directement
> à la section 6 : l'installation, le lancement et les vérifications y diffèrent.

```bash
docker build -f Dockerfile.bot -t transcria-bot:latest .
# ou, via compose :
docker compose -f docker-compose.bot.yml build
```

L'image part de l'image **Playwright officielle**, qui fournit Chromium et la liste exacte
des bibliothèques système dont il dépend. Le paquet Python `playwright` y est installé à une
version **figée** sur celle des navigateurs de l'image : un client désaligné refuse de
démarrer.

---

## 3. Lancement

Une réunion = une exécution.

```bash
docker compose -f docker-compose.bot.yml run --rm bot https://jitsi.exemple/ma-salle
```

ou directement :

```bash
docker run --rm --shm-size=1g \
  -e TRANSCRIA_URL=http://transcria:7870 \
  -e TRANSCRIA_TOKEN=tia_… \
  -e BOT_LANGUAGE=fr \
  transcria-bot:latest https://jitsi.exemple/ma-salle
```

> `--shm-size=1g` n'est pas un confort : Chromium sature les 64 Mo de `/dev/shm` par défaut
> et plante de façon erratique sur les pages lourdes.

**Plusieurs réunions en parallèle** : lancez simplement plusieurs conteneurs. Chacun alloue
son propre port de pont interne, il n'y a rien à coordonner.

### Réglages (variables d'environnement)

| Variable | Défaut | Rôle |
|---|---|---|
| `MEETING_URL` | — | URL de la réunion (ou premier argument) |
| `TRANSCRIA_URL` | — | TranscrIA pour la transcription. **Absent → capture sans transcription** |
| `TRANSCRIA_TOKEN` | — | jeton d'API TranscrIA (`tia_…`) |
| `BOT_DISPLAY_NAME` | `TranscrIA` | nom affiché dans la réunion |
| `BOT_LANGUAGE` | — | langue de transcription (ex. `fr`) |
| `BOT_MAX_DURATION_S` | `14400` (4 h) | durée maximale de présence |
| `BOT_ALONE_TIMEOUT_S` | `30` | durée seul en réunion avant de repartir |
| `BOT_ADMISSION_TIMEOUT_S` | `120` | attente en salle d'attente avant d'abandonner |
| `BOT_INSECURE` | — | `1` pour accepter un certificat auto-signé (instance interne) |
| `BOT_LOG_LEVEL` | `INFO` | verbosité |

Le jeton s'obtient dans TranscrIA (jetons d'API personnels) ; la façade doit être activée
(`live.facade.enabled`).

---

## 4. Codes de retour — décider s'il faut rejouer

L'orchestrateur (cron, Kubernetes, superviseur) s'appuie dessus :

| Code | Signification | Rejouer ? |
|---|---|---|
| `0` | la réunion a eu lieu (fin normale, bot parti seul, réunion close, expulsé) | non |
| `1` | le bot n'a pas pu être admis (refus, mot de passe, authentification requise) | non, en l'état |
| `2` | **anomalie technique** (plus de média, transport coupé, navigateur perdu) | **oui** |
| `3` | erreur de configuration (URL manquante) | non |

Cette distinction est le cœur de l'exploitation : une réunion vide n'est pas un échec, une
perte de média en est un.

---

## 5. Vérifier que tout fonctionne

**Sans aucune réunion** — valide la chaîne de capture (navigateur, interception audio, pont,
décodage) :

```bash
python scripts/gate_bot_capture_selftest.py
```

**Avec une réunion complète** — le script peut fabriquer lui-même les participants :

```bash
python scripts/gate_bot_jitsi.py https://jitsi.exemple/salle-test \
  --fake-participant --participant-audio voix.wav \
  --transcribe http://127.0.0.1:7870 --token-file jeton.txt --language fr
```

Un point d'expérience : **un fichier audio de test doit être plus long que la session**,
sinon on mesure du silence sans s'en apercevoir. Et une tonalité pure ne convient pas — les
plateformes appliquent une suppression de bruit qui l'efface. Utilisez de la **vraie parole**.

---

## 6. Bot ZOOM (SDK natif)

### 6.1 Ce qu'il faut obtenir de Zoom

Le type d'app « Meeting SDK » **n'existe plus** comme choix séparé sur le Marketplace.
Parcours exact (libellés relevés sur la [documentation officielle](https://developers.zoom.us/docs/build-flow/quick-start-guide/)) :

1. [marketplace.zoom.us](https://marketplace.zoom.us) → se connecter ;
2. volet de navigation **en bas à gauche** → **Developer** (ce n'est PAS en haut à droite) ;
3. page **Created apps** → bouton **Develop** → **Build an app** ;
4. choisir **General app** → **Create** ;
5. menu de gauche → **Basic Info** : l'encadré des identifiants donne **Client ID** et
   **Client Secret**. ⚠️ Deux jeux coexistent, **Development** et **Production** : prendre
   ceux de **Development** tant que l'app n'est pas publiée ;
6. menu de gauche → **Features** → onglet **Embed** → activer **Meeting SDK**.

Ces identifiants tiennent lieu de SDK Key et SDK Secret. L'app n'a pas besoin d'être publiée
pour un usage sur son propre compte.

**Un compte Zoom GRATUIT (Basic) suffit** pour créer l'app, signer le JWT, rejoindre la
réunion **et capter l'audio brut**. L'ancienne licence « raw data » a été supprimée. Deux
réserves, détaillées en 6.2.

Le point qui décide du reste est **sur quel compte** l'app est créée :

| Réunion à rejoindre | Ce qu'il faut | Revue de l'app par Zoom |
|---|---|---|
| Du **compte propriétaire de l'app** | la signature JWT seule | **non** |
| D'un compte **externe** (client) | + jeton ZAK ou OBF | **oui** |

Autrement dit : pour vos propres réunions, rien d'autre à faire. Pour celles de tiers, Zoom
impose une revue de l'app — **aucun code ne dispense de cette démarche** (durci depuis mars
2026). L'alternative sans revue est que l'**hôte** active RTMS, déjà pris en charge par
TranscrIA (`connector_service/live/rtms_transport.py`).

### 6.2 Le droit d'enregistrer — la condition à ne pas manquer

Zoom ne délivre l'audio brut **qu'à un participant qui a le droit d'enregistrer**. Sans lui,
l'abonnement réussit et aucune frame n'arrive : panne parfaitement muette. Le bot vérifie donc
ce droit, le **demande** si nécessaire, et échoue avec un message explicite plutôt que de
capter le vide.

**Réglage préalable, sans lequel la demande du bot ne s'affichera jamais.** Attention, Zoom a
RENOMMÉ ce réglage : il ne s'appelle plus « Local recording ». Dans le portail web →
**Settings** → onglet **Recording & Transcript** :

- activer **« Record to computer files »** (l'ancien « enregistrement local ») ;
- section **« Who can request host permission to record? »** → cocher au moins
  **Internal meeting participants**.

Cette section propose aussi **« Auto approve their permission requests »**. Cochée, elle
supprime le geste manuel de l'hôte — à confirmer au premier essai, mais c'est la voie la plus
confortable sur un compte gratuit.

Quatre façons d'obtenir le droit, par ordre de confort :

| Voie | Fonctionne sur un compte gratuit ? |
|---|---|
| **« Auto approve their permission requests »** activé | ✅ (à confirmer en réunion réelle) |
| Le bot est **hôte ou co-hôte** | ✅ |
| L'hôte accepte la demande **en séance** | ✅ (une fenêtre s'affiche) |
| **Jeton d'enregistrement local** (automatique, via API) | ❌ **payant uniquement** |

Sans auto-approbation ni statut de co-hôte, l'hôte doit accepter la fenêtre **à chaque
réunion**. Le bot attend jusqu'à `recording_permission_timeout_s` (120 s par défaut) et le
journalise en clair.

Autre limite du plan Basic, sans rapport avec le SDK : les réunions y sont **plafonnées à
40 minutes**.

### 6.3 Installation et lancement

```bash
docker build -f Dockerfile.zoom-sdk -t transcria-zoom-sdk:latest .

docker run --rm \
  -e ZOOM_CLIENT_ID=… -e ZOOM_CLIENT_SECRET=… \
  -e TRANSCRIA_URL=http://hote:7870 -e TRANSCRIA_TOKEN=tia_… \
  transcria-zoom-sdk:latest --meeting "https://us05web.zoom.us/j/1234567890?pwd=…"
```

`--meeting` accepte aussi bien le **lien d'invitation** (le code secret en est extrait) que le
numéro sous sa forme affichée (`123 456 7890`).

Le **Client Secret se lit uniquement dans l'environnement** : il n'existe volontairement
aucune option de ligne de commande pour lui, qui le rendrait lisible dans la liste des
processus de la machine.

### 6.4 Points d'exploitation propres à ce bot

- **Une instance SDK par processus** (`SDKERR_OTHER_SDK_INSTANCE_RUNNING`) : une réunion = un
  conteneur. C'est une contrainte de la bibliothèque, pas un choix.
- **Dépendance de ~275 Mo**, x86_64 Linux uniquement. Elle reste confinée à cette image.
- **Le conteneur est indispensable** : le SDK est un client Zoom complet et exige D-Bus et
  PulseAudio. Sans eux il ne renvoie pas d'erreur, il **plante par segfault**. Ne cherchez pas
  à le lancer hors conteneur.
- **Débit audio** : 32 kHz par défaut, 48 kHz possible. Le SDK n'offre pas 16 kHz.

### 6.5 État de validation (2026-07-27)

Validé contre une **vraie réunion Zoom**, avec un compte **gratuit** :

| Vérifié | Résultat |
|---|---|
| Authentification | `AUTHRET_SUCCESS` |
| Entrée en réunion, micro et caméra coupés | oui, aucun son émis |
| Audio par participant | 9879 frames, crête 32734/32767, 4226 sonores |
| **Locuteurs nommés** | oui — « Martos Martossien », pas un identifiant de flux |
| Transcription bout en bout | 29 segments attribués |
| Code de sortie | `0` |

Quatre défauts n'ont pu être trouvés QUE par cet essai réel, et sont corrigés :

1. `isAudioOff` **ne fait pas entrer en muet**, il empêche de rejoindre la session audio —
   d'où `SDKERR_NOT_JOIN_AUDIO` à l'abonnement. Il faut `JoinVoip()` puis se couper.
2. Le SDK **segfaute à la fermeture** si sa boucle d'évènements est coupée avant le nettoyage,
   puis une seconde fois dans ses destructeurs statiques — un succès sortait en code 139.
3. Le droit d'enregistrement doit être **interrogé**, pas seulement attendu : l'hôte peut
   l'accorder par des voies qui n'émettent aucun rappel.
4. La façade STT **hallucinait sur les fenêtres silencieuses** (cf. 6.6).

### 6.6 Qualité de transcription — ce qui a été mesuré

Un moteur de type Whisper **n'échoue pas sur du silence : il invente**. Mesuré ici, 12 s de
silence numérique pur ont produit une phrase française complète et entièrement fausse.

Le tampon par locuteur exige donc désormais une **durée cumulée de parole** (0,35 s) avant de
soumettre une fenêtre. Rejeu d'un flux réaliste à travers le vrai moteur :

| | Fenêtres envoyées | Segments inventés |
|---|---|---|
| Avant | 5 | 2 |
| Après | 3 | 0 |

Vérifié aussi, pour ne pas corriger la mauvaise cause : le **débit n'y est pour rien** (16, 32
et 48 kHz donnent un texte identique), et du silence AUTOUR d'une parole réelle ne la dégrade
pas. C'est bien l'absence de parole qui fait halluciner.

### 6.7 Vérifier

```bash
docker run --rm --network host \
  -e ZOOM_CLIENT_ID=… -e ZOOM_CLIENT_SECRET=… \
  --entrypoint /usr/local/bin/zoom-sdk-entrypoint \
  -v "$PWD/scripts:/app/scripts:ro" -v /chemin/jeton.txt:/app/token.txt:ro \
  transcria-zoom-sdk:latest \
  python3 -u /app/scripts/gate_bot_zoom_sdk.py --meeting "123 456 7890" --passcode ●●●●●● \
    --join-timeout-s 300 --seconds 120 \
    --transcribe http://127.0.0.1:7870 --token-file /app/token.txt --language fr
```

Le gate échoue explicitement si l'audio est capté mais **sans locuteur nommé** : ce serait
perdre l'apport principal du SDK sans que rien ne le signale.

Deux points appris en le faisant tourner :

- `--join-timeout-s` (attente d'admission) et `--seconds` (durée de CAPTURE) sont **distincts**.
  Les confondre faisait abandonner le bot pendant qu'il patientait légitimement en salle
  d'attente.
- `--network host` n'est utile qu'en **test local** : depuis un conteneur, le pont Docker peut
  être filtré et `host.docker.internal` tomber sur un proxy. En exploitation, le bot joint
  TranscrIA par le réseau comme n'importe quel client, sans réglage particulier.

Pour éviter d'avoir à admettre le bot et à accepter l'autorisation à chaque essai : désactiver
la salle d'attente (**Sécurité** dans la réunion) et passer le bot **co-hôte**.

---

## 7. Limites connues

**Communes**

- **Salle d'attente / mot de passe** : détectés (code `1`), pas franchis — seul l'hôte débloque.
- **Pas de reprise automatique** : le conteneur sort en code `2`, c'est à l'orchestrateur de
  relancer.

**Bot navigateur**

- **Une seule plateforme** : Jitsi. Teams et Meet demanderont chacun leur pilote.
- **Le nom d'un locuteur** est résolu à l'arrivée de sa piste, pas rafraîchi ensuite.
- **Zoom n'est PAS couvert par ce bot** : son client Web refuse l'automatisation.

**Bot Zoom (SDK natif)**

- **Réunions externes** : exigent une revue de l'app par Zoom (cf. 6.1).
- **Sous-salles** : le bot y entre quand l'hôte l'y affecte, et rétablit sa capture (Zoom
  traite l'entrée en sous-salle comme une nouvelle entrée). Mais **le droit d'enregistrer
  doit y être ré-accordé** — sur un compte gratuit, cela signifie une nouvelle fenêtre à
  accepter. Passer le bot co-hôte évite ce geste.
- **Canal de discussion et partage d'écran** : hors périmètre — le bot transcrit la parole,
  il ne participe pas.
- **Bindings en bêta** (`zoom-meeting-sdk`) : la version est figée dans l'image, une montée
  demande de revérifier l'API des rappels bruts.
- **File audio bornée** : si le moteur STT ne suit pas durablement, les frames les plus
  anciennes sont écartées — et journalisées. Un direct qui retarde de dix minutes n'a plus
  d'intérêt ; un trou signalé reste exploitable.
