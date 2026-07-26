# Bot de réunion — installation, lancement, exploitation

Le bot rejoint une visioconférence comme un participant, capte l'audio **par locuteur** et
le transcrit via TranscrIA. C'est la **voie de secours** du chantier temps réel : contrairement
aux connecteurs officiels (qui reçoivent des webhooks et exigent donc un port ouvert), le bot
n'établit que des connexions **sortantes** — il traverse un pare-feu ou un proxy d'entreprise
sans ouverture entrante.

Plateforme prise en charge aujourd'hui : **Jitsi** (instance publique ou auto-hébergée).

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

## 2. Installation

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

## 6. Limites connues

- **Une seule plateforme** : Jitsi. Zoom-web, Teams et Meet demanderont chacun leur pilote.
- **Salle protégée par mot de passe** : détectée (code `1`), pas franchie.
- **Pas de reprise automatique** après perte du navigateur : le conteneur sort en code `2`,
  c'est à l'orchestrateur de relancer.
- **Le nom d'un locuteur** est résolu à l'arrivée de sa piste, pas rafraîchi ensuite.
