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

Créez une app de type **« Meeting SDK »** sur [marketplace.zoom.us](https://marketplace.zoom.us),
puis relevez son **Client ID** et son **Client Secret**.

Le point qui décide de tout est **sur quel compte** l'app est créée :

| Réunion à rejoindre | Ce qu'il faut | Revue de l'app par Zoom |
|---|---|---|
| Du **compte propriétaire de l'app** | la signature JWT seule | **non** |
| D'un compte **externe** (client) | + jeton ZAK ou OBF | **oui** |

Autrement dit : pour vos propres réunions, rien d'autre à faire. Pour celles de tiers, Zoom
impose une revue de l'app — **aucun code ne dispense de cette démarche** (durci depuis mars
2026). L'alternative sans revue est que l'**hôte** active RTMS, déjà pris en charge par
TranscrIA (`connector_service/live/rtms_transport.py`).

### 6.2 Installation et lancement

```bash
docker build -f Dockerfile.zoom-sdk -t transcria-zoom-sdk:latest .

docker run --rm \
  -e ZOOM_CLIENT_ID=… -e ZOOM_CLIENT_SECRET=… \
  -e TRANSCRIA_URL=http://hote:7870 -e TRANSCRIA_TOKEN=tia_… \
  transcria-zoom-sdk:latest --meeting "https://us05web.zoom.us/j/5786297113?pwd=…"
```

`--meeting` accepte aussi bien le **lien d'invitation** (le code secret en est extrait) que le
numéro sous sa forme affichée (`578 629 7113`).

Le **Client Secret se lit uniquement dans l'environnement** : il n'existe volontairement
aucune option de ligne de commande pour lui, qui le rendrait lisible dans la liste des
processus de la machine.

### 6.3 Points d'exploitation propres à ce bot

- **Une instance SDK par processus** (`SDKERR_OTHER_SDK_INSTANCE_RUNNING`) : une réunion = un
  conteneur. C'est une contrainte de la bibliothèque, pas un choix.
- **Dépendance de ~275 Mo**, x86_64 Linux uniquement. Elle reste confinée à cette image.
- **Le conteneur est indispensable** : le SDK est un client Zoom complet et exige D-Bus et
  PulseAudio. Sans eux il ne renvoie pas d'erreur, il **plante par segfault**. Ne cherchez pas
  à le lancer hors conteneur.
- **Débit audio** : 32 kHz par défaut, 48 kHz possible. Le SDK n'offre pas 16 kHz.

### 6.4 Vérifier

```bash
docker run --rm -e ZOOM_CLIENT_ID=… -e ZOOM_CLIENT_SECRET=… \
  --entrypoint /usr/local/bin/zoom-sdk-entrypoint \
  -v "$PWD/scripts:/app/scripts:ro" transcria-zoom-sdk:latest \
  python3 -u /app/scripts/gate_bot_zoom_sdk.py --meeting "578 629 7113" --seconds 60
```

Le gate échoue explicitement si l'audio est capté mais **sans locuteur nommé** : ce serait
perdre l'apport principal du SDK sans que rien ne le signale.

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
- **Bindings en bêta** (`zoom-meeting-sdk`) : la version est figée dans l'image, une montée
  demande de revérifier l'API des rappels bruts.
- **File audio bornée** : si le moteur STT ne suit pas durablement, les frames les plus
  anciennes sont écartées — et journalisées. Un direct qui retarde de dix minutes n'a plus
  d'intérêt ; un trou signalé reste exploitable.
