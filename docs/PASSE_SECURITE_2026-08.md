# Passe sécurité — août 2026

État de référence : `132ddfd` sur `main`, après la passe qualité (`docs/PASSE_QUALITE_2026-08.md`).
Le modèle de sécurité existant est décrit dans `docs/SECURITY_MODEL.md` ; ce document ne le
remplace pas, il liste **ce qui manque encore** et, tout aussi important, **ce qu'on refuse de
faire**.

Chaque constat ci-dessous a été **vérifié dans le code**, pas déduit d'une lecture rapide :
les références de fichier et de ligne ont été ouvertes une par une. Trois affirmations
plausibles de l'audit source ont été écartées à la vérification — elles sont listées en fin de
document, pour qu'elles ne reviennent pas.

---

## 1. Le modèle de menace, écrit AVANT les correctifs

C'est la partie qui décide de tout le reste. Une liste de failles sans modèle de menace
produit toujours la même chose : on durcit ce qui est facile à durcir, pas ce qui est
exposé.

**Aujourd'hui, TranscrIA est un outil auto-hébergé sur une machine de confiance.** Le portail
n'est pas publié sur Internet, ses utilisateurs sont authentifiés et connus, l'administrateur
applicatif est aussi l'administrateur de la machine. Dans ce cadre, l'immense majorité des
constats d'un audit générique sont théoriques : un attaquant qui peut déjà se connecter comme
admin n'a pas besoin d'une élévation de privilèges, il a les clés.

**Ce qui change tout : les connecteurs de réunion.** Meet fonctionne par *pull* (Pub/Sub) et
ne demande rien d'entrant. Mais **Zoom RTMS et Teams exigent un point d'entrée HTTPS public** :
webhook de validation, notifications de cycle de vie, flux temps réel. Le jour où l'un des deux
est activé, la surface d'attaque cesse d'être hypothétique — une partie du portail devient
joignable depuis Internet, et certaines routes doivent l'être **avant authentification** (c'est
la nature d'un webhook).

D'où le classement de ce document, en deux temps :

| | Question posée |
|---|---|
| **Vague S1** | Qu'est-ce qui est un vrai défaut **même sur une machine locale** ? |
| **Vague S2** | Qu'est-ce qui devient nécessaire **le jour de l'exposition**, et qu'il vaut mieux avoir posé avant ? |

Un troisième groupe, S3, rassemble ce qui coûte trois lignes et qu'il serait absurde de ne pas
faire en passant.

**Le fil conducteur :** on ne durcit pas un outil local comme un service exposé. On rend
possible de l'exposer sans se réécrire.

---

## 2. Ce qui est déjà en place — et qu'on ne refait pas

Une passe sécurité qui ne dit pas ce qui tient déjà donne l'impression que tout est à faire, et
fait perdre du temps sur des sujets réglés.

- **Mots de passe** hachés (Werkzeug), longueur minimale imposée ; **jetons d'API** aléatoires,
  stockés hachés, comparés en temps constant.
- **Session** : `HttpOnly` et `SameSite=Lax` **actifs par défaut**, durée explicite (12 h),
  anti-bourrinage 5 échecs / 5 min par (adresse socket, identifiant) — et la clé repose
  volontairement sur l'adresse socket, jamais sur `X-Forwarded-For`, qui est client-contrôlé.
- **Identité d'entreprise** : OIDC avec `state`/`nonce`/PKCE, LDAP chiffré, connecteur proxy
  borné à l'adresse socket, break-glass local garanti (le doctor refuse un backend fédéré sans
  administrateur local actif).
- **Base de données** : SQLAlchemy avec paramètres liés partout ; aucun `shell=True` dans le
  code Python.
- **Références et codes d'accès de réunion** chiffrés (Fernet), avec **échec fermé** si la clé
  manque.
- **Traversée de chemin** contrôlée sur les téléchargements et la maintenance.
- **Durcissement HTTP(S) complet, déjà écrit** : `Secure`, HSTS, contrôle d'`Origin`, jetons
  CSRF synchroniseurs, CSP en trois modes. Tout existe et tout est testé — c'est **opt-in**,
  pas absent. Voir `SECURITY_MODEL.md` §7, et la vague S2 ci-dessous pour la question des
  défauts.
- **Permissions du fichier de configuration** : corrigées par la passe qualité (Q1.4) —
  écriture atomique, `0600` à chaque enregistrement, et un contrôle au doctor qui regarde
  l'état **réel** sur disque.

---

## Vague S1 — vrais défauts, même sur une machine locale

Six points. Ils ont en commun de ne dépendre d'aucune exposition réseau : ils sont faux
aujourd'hui, sur l'installation telle qu'elle tourne — et le resteraient si le portail ne
sortait jamais de sa machine.

### S1.1 — Le service d'inférence échoue OUVERT

`inference_service/security.py` — `enforce_api_key()` est un **no-op quand aucune clé n'est
configurée** :

```python
expected = expected_api_key(config)
if not expected:
    return  # mode ouvert (dev)
```

Et `expected_api_key()` lit d'abord une variable d'environnement (`inference.auth.api_key_env`),
avec repli sur une valeur en clair dans la configuration. Donc : **une variable d'environnement
qui disparaît au déploiement transforme silencieusement un service authentifié en service
ouvert.** Rien ne le signale — ni au boot, ni à la première requête.

Ce n'est pas théorique dans ce projet : `deploy/transcria-inference.service` écoute sur
**`0.0.0.0:8002`**, en **root**. Les routes de transcription, diarisation et empreinte vocale
acceptent une référence de fichier locale du client.

**Ce que ça vaut :** sur un poste isolé, peu. Sur une machine reliée à un réseau d'entreprise
— la topologie « frontale + nœud GPU » que le projet documente et encourage — le nœud GPU
devient joignable sans authentification par quiconque atteint le port.

**Correction (courte) :** inverser le défaut. Sans clé configurée, le service **refuse de
démarrer**, sauf drapeau de développement explicite ET écoute sur loopback. Un message qui dit
quoi faire, pas un warning qu'on ne lit jamais.

**Critère d'acceptation :** clé absente + écoute non-loopback → boot refusé (test) ; clé absente
+ `127.0.0.1` + drapeau dev → démarre avec un avertissement visible (test) ; clé présente →
comportement actuel (test de non-régression).

### S1.2 — Le kit runner interpole une URL dans du Bash

`transcria/ingestion/runner_kit.py` — `build_kit_script()` construit le script d'installation
par f-string :

```python
PORTAL_URL="{portal}"
```

La seule validation en amont (`admin_routes.py:627`) est un préfixe `http://`/`https://`. Une
valeur comme `https://x";$(commande);"` sort du guillemet et exécute. Le nom d'exécutant, lui,
**est** validé (`valid_runner_name`) — c'est l'URL qui ne l'est pas.

**Ce que ça vaut :** il faut être administrateur du portail pour générer un kit. Mais le
docstring de la fonction dit lui-même l'essentiel : *« la personne au clavier n'est pas
forcément l'admin du portail »*. Le script est **transmis à quelqu'un d'autre**, qui l'exécute
**en root sur une autre machine**. Ce n'est pas « l'admin peut se nuire à lui-même » : c'est un
admin qui fabrique une charge exécutée par un tiers sur un hôte distinct.

**Correction (très courte) :** `urlsplit` avec schéma dans `{http, https}`, hostname obligatoire,
pas de *userinfo*, pas de caractère de contrôle ; puis `shlex.quote` sur chaque valeur injectée.
Le YAML produit passe par un sérialiseur, pas par une f-string.

**Critère d'acceptation :** tests négatifs sur guillemet, `$(…)`, backtick, CR, LF, `@` de
userinfo, schéma `file://`. Chacun refusé **avant** génération.

### S1.3 — Désactiver un compte ne ferme pas ses sessions

`transcria/app_services.py:232` :

```python
@login_manager.user_loader
def load_user(user_id: str) -> User | None:
    return UserStore.get_by_id(user_id)
```

Aucune vérification de `is_active`. `UserStore.deactivate_user()` pose bien le drapeau en base,
mais **la session en cours continue de fonctionner** jusqu'à son expiration — jusqu'à 12 h. Même
chose après un changement de mot de passe : les sessions ouvertes ailleurs survivent.

**Ce que ça vaut :** c'est le geste qu'on fait quand quelqu'un part, ou quand on soupçonne un
compte compromis. Qu'il n'ait pas d'effet immédiat est exactement le contraire de ce que
l'administrateur croit avoir fait. C'est le défaut le moins spectaculaire de la liste et
probablement le plus fréquemment rencontré en vrai.

**Correction (une ligne, plus un test) :** `load_user` retourne `None` si le compte est inactif.
Pour le changement de mot de passe, la voie propre est un compteur de version d'identité dans
`get_id()` — plus intrusive ; à trancher séparément, la désactivation étant le cas urgent.

**Critère d'acceptation :** session ouverte + désactivation → la requête suivante est
redirigée vers la connexion (test) ; le compte actif n'est pas affecté (test).

### S1.4 — Le compte d'amorçage `CHANGE-ME`

`config.example.yaml:77` publie `first_admin_password: "CHANGE-ME"`, `config/loader.py:54` le
reprend en défaut, et le compte est **réellement créé**. `config/checks/auth.py` ne produit
qu'un avertissement.

**Ce que ça vaut :** le secret initial est dans un dépôt public. Toute installation qui n'a pas
changé le mot de passe est ouverte avec un identifiant connu de tous. Le fait qu'un
avertissement existe ne compte pas : un avertissement au boot d'un service qu'on ne regarde
jamais n'est pas une protection.

**Correction :** pas de mot de passe par défaut. Au premier démarrage sans administrateur, on
génère un secret aléatoire **affiché une seule fois** dans le journal de démarrage — ou on exige
qu'il soit fourni par l'environnement. La sentinelle `CHANGE-ME` fait **refuser le boot** hors
mode debug.

**Critère d'acceptation :** boot avec la sentinelle hors debug → refus explicite (test) ; boot
sans administrateur → secret aléatoire généré, journalisé une fois, non stocké en clair dans la
configuration (test).

### S1.5 — Lire et modifier un job passent par la même porte

`transcria/web/job_access.py` n'offre qu'une garde, `can_access_job` : propriétaire, admin, ou
**membre d'un groupe commun avec le propriétaire**. Les routes mutantes de l'éditeur
(`editor_routes.py` : `PUT /draft`, `DELETE /draft`, `POST /save`, `POST /sync-summary`) ne
portent que `@login_required` et cette même garde.

Conséquence concrète : un compte de rôle **`VIEWER`**, dont la seule permission est
`DOWNLOAD_EXPORTS`, peut **réécrire le sous-titrage d'un job qui ne lui appartient pas**, dès
lors qu'il partage un groupe avec le propriétaire. Le rôle dit « lecture seule » ; le code dit
autre chose.

**Correction (bornée, pas une refonte) :** ajouter `require_job_edit` à côté de
`require_job_access`, exigeant propriétaire OU admin OU membre de groupe **avec un rôle
d'écriture**. Les routes mutantes l'utilisent. On ne construit pas une matrice
route × rôle × relation automatique : ce serait de la sur-ingénierie ici — quatre verbes
suffisent à couvrir le besoin réel.

**Critère d'acceptation :** un `VIEWER` membre du même groupe obtient 200 en lecture et **403**
sur chaque route mutante (test paramétré sur la liste des routes) ; le propriétaire et l'admin
conservent leur accès (test).

### S1.6 — Un chemin de script libre, exécuté en root

`transcria/gpu/llm_backend.py:270` lance `["/bin/bash", self.launch_script]`, où `launch_script`
vient de `gpu.arbitrage_script` en configuration. Or `/admin/config` propose un **mode YAML
brut** (`admin_routes.py:205-230`) : un administrateur applicatif peut donc désigner n'importe
quel fichier, qui sera exécuté par un service tournant en root.

**Ce que ça vaut, honnêtement :** sur l'installation de référence, l'admin applicatif *est* le
propriétaire de la machine — le trajet ne lui apporte rien. Mais TranscrIA est un projet public :
ailleurs, « administrateur du portail » peut être un rôle métier confié à quelqu'un qui n'a
aucun accès système. La permission `MANAGE_CONFIG` ne devrait pas valoir shell root.

**Correction (bornée) :** contraindre le chemin à une **racine allowlistée** (`./scripts/` et un
répertoire configurable), refuser les fichiers inscriptibles par autrui, refuser les liens
symboliques sortant de la racine. **On ne re-architecture pas** le service en non-root ni en
registre fermé de backends : voir la section « Écarté ».

**Critère d'acceptation :** chemin hors racine, chemin traversant (`../`), lien symbolique
sortant, fichier world-writable → refus au démarrage du backend, message explicite (tests).

---

## Vague S2 — ce qui devient nécessaire à l'exposition

Ces trois points ne sont pas urgents tant que le portail reste local. Ils le deviennent le jour
où Zoom RTMS ou Teams est activé — c'est-à-dire le jour où une URL du portail est publiée. Les
poser avant évite d'avoir à les poser dans l'urgence, au moment précis où l'on a autre chose à
faire.

### S2.1 — Les défauts de transport, et ce que le doctor doit en dire

Tout existe (`SECURITY_MODEL.md` §7) : `Secure`, HSTS, contrôle d'`Origin`, jetons CSRF, CSP.
Tout est **désactivé par défaut**, ce qui est le bon choix pour un usage local en HTTP — un
défaut qui casse l'installation d'essai est un défaut qu'on désactive sans lire.

Le problème n'est donc pas les défauts : c'est qu'**aucun signal ne les rattache à l'exposition**.
`check_transport_security` avertit déjà quand un backend d'identité **fédéré** est actif sans
cookie sécurisé. Il ne dit rien quand c'est un **connecteur de réunion public** qui est activé.

**Correction :** étendre le contrôle existant — connecteur Zoom RTMS ou Teams activé **et**
transport non sécurisé → **FAIL**, pas WARN. Et documenter en un paragraphe le profil
« exposé » : `behind_tls_proxy`, `csrf_tokens`, CSP en `enforce`, HSTS.

**Pourquoi FAIL et pas WARN :** un webhook public en HTTP clair transporte des jetons de
plateforme. Ce n'est pas une posture perfectible, c'est une erreur de déploiement.

### S2.2 — Requêtes sortantes pilotées par une valeur d'utilisateur

`connector_service/bot/visio.py:79-108` : le lien de réunion fourni détermine l'hôte interrogé.

```python
base = os.environ.get("VISIO_API_BASE", "").rstrip("/") or f"{parts.scheme}://{parts.netloc}"
```

Puis `urllib.request.urlopen(api, timeout=10)`. Un utilisateur authentifié qui soumet un lien
choisit donc l'hôte que le service contacte — SSRF aveugle classique. Le repli en cas d'échec
est honnête (on retombe sur le slug), ce qui limite l'exfiltration : l'attaquant apprend peu.
Reste que la requête part, vers un réseau interne potentiellement.

**Correction :** allowlist d'hôtes de service en configuration (le déploiement sait quelles
instances il utilise), et refus des plages d'adresses privées/locales quand l'allowlist est
vide. Journaliser l'hôte contacté.

**Critère d'acceptation :** lien pointant vers `127.0.0.1`, `169.254.169.254`, une plage RFC1918
ou un hôte hors allowlist → aucune requête émise (test avec ouvreur factice qui échoue si
appelé).

### S2.3 — Tous les exécutants partagent un seul principal

`runner_kit.py:55` émet chaque jeton pour le **même compte**, `RUNNER_ACCOUNT` (`svc-runner`).
Deux exécutants sur deux machines différentes sont indiscernables côté portail : même identité,
mêmes droits, et révoquer l'un revient à trancher entre « révoquer le compte » (tous tombent) et
« révoquer un jeton » (encore faut-il savoir lequel appartient à qui).

**Ce que ça vaut :** nul avec un seul exécutant, ce qui est le cas aujourd'hui. Réel dès qu'il y
en a deux — et le kit existe précisément pour qu'il y en ait plusieurs.

**Correction (minimale) :** le jeton porte déjà un libellé par exécutant ; il suffit d'exposer la
correspondance jeton ↔ exécutant dans l'interface et de permettre la révocation **par exécutant**.
Un principal distinct par runner est la version propre, plus lourde ; à trancher quand un
deuxième exécutant existera vraiment.

---

## Vague S3 — trois lignes chacun, à faire en passant

Rien ici ne mérite un chantier. Tout mérite d'être fait pendant qu'on a le fichier ouvert.

| Point | Correction |
|---|---|
| **`/ready` divulgue l'erreur de base BRUTE, sans authentification** — `health_routes.py:113` n'a aucun `@login_required` et renvoie `str(exc)` de l'exception SQLAlchemy, qui porte l'URI de connexion : hôte, port, utilisateur, nom de base. `/metrics` est anonyme aussi. | statut binaire pour l'anonyme, détail réservé aux comptes authentifiés |
| **Injection de formule dans les exports CSV** — `audit/routes.py:132` écrit `target_label` (= le **titre du job**, donc une valeur d'utilisateur) et `actor_username` sans échappement. Une valeur commençant par `=`, `+`, `-` ou `@` est exécutée par le tableur qui l'ouvre. | préfixer d'une apostrophe les cellules commençant par ces caractères |
| **Budget de décompression des documents** — `document_extractor.py` borne l'entrée (25 Mo) et le texte retenu (12 000 caractères), mais pas la taille **décompressée** d'un DOCX/PPTX | plafonner la somme des `file_size` de l'archive avant extraction |
| **Upload lu intégralement en mémoire** — `wizard_api.py:139` fait `file.read()` avec `MAX_CONTENT_LENGTH` à **1 Gio** | écrire par blocs vers le disque |

Deux remarques sur ce lot, qui n'est « mineur » qu'en apparence.

`/ready` mérite d'être fait **avant** l'exposition et non « en passant » : un point d'entrée
anonyme qui recrache la chaîne de connexion à la base dès que celle-ci tombe est exactement le
genre de détail qu'on ne découvre qu'après. Il est classé ici parce que la correction tient en
trois lignes, pas parce que l'enjeu est faible.

L'upload, lui, est le seul point du document qu'un **utilisateur parfaitement légitime**
déclenche sans le vouloir, en envoyant trois gros fichiers en parallèle.

---

## Écarté volontairement

Autant que la liste des corrections, celle des refus. La consigne était explicite : pas de
révolution, et distinguer la sécurité nécessaire de la sur-sécurité.

| Écarté | Pourquoi |
|---|---|
| **Faire tourner les services en non-root** | C'est la correction « propre » de S1.6, et elle est réelle. Mais elle touche l'installeur, les unités systemd, les droits sur `jobs/`, le cache des modèles, l'accès GPU et les images Docker — pour un gain nul sur le déploiement de référence, où l'admin applicatif est le propriétaire de la machine. À reprendre comme chantier d'installation, jamais au fil de l'eau. La racine allowlistée ferme l'essentiel du trajet pour quelques lignes. |
| **Registre fermé de backends LLM à la place des chemins de script** | Refonte de l'extensibilité du projet pour une classe de risque déjà couverte par l'allowlist. |
| **Matrice RBAC route × rôle × relation, générée et testée automatiquement** | Séduisant sur le papier. En pratique : un générateur à maintenir, des faux positifs sur chaque route nouvelle, et une couverture qui ne dit rien de la *sémantique* d'un droit. Quatre gardes explicites valent mieux qu'une matrice qu'on cesse de lire. |
| **SBOM, scan d'images, politique SLA de vulnérabilités** | Déjà écarté par la passe qualité, pour la même raison : outillage d'organisation à coût de maintenance permanent, sans mainteneur dédié. |
| **Signature/authentification des sauvegardes** | La sauvegarde est locale et restaurée par la même personne sur la même machine. Signer protégerait d'un attaquant qui a déjà l'accès disque — c'est-à-dire qui n'a plus besoin de la sauvegarde. |
| **Rotation de clé Fernet et intégration d'un coffre** | Le chiffrement est correct et échoue fermé. Une rotation sans coffre déplace le problème ; un coffre est une dépendance d'infrastructure qui ne correspond à aucun déploiement actuel. |
| **Coffre de secrets externe pour le YAML** | Q1.4 a mis le fichier en `0600` avec contrôle au doctor. Sur une machine mono-utilisateur, le gain marginal d'un coffre ne paie pas sa complexité d'exploitation. |
| **Reproductibilité complète des images et épinglage exhaustif** | Vrai sujet d'industrialisation, faux sujet de sécurité à ce stade. |

---

## Trois affirmations de l'audit source non retenues

Elles sont plausibles à la lecture, fausses ou trompeuses à la vérification. Les consigner évite
qu'elles reviennent.

1. **« CSRF, CSP, `Secure`, TLS/HSTS désactivés »** — ils ne sont pas *absents*, ils sont
   **opt-in et complets**, et `SameSite=Lax` + `HttpOnly` sont actifs par défaut. Le vrai
   manque est le signal qui les rattache à l'exposition (S2.1), pas le code.
2. **« Le fichier de configuration est écrit sans `chmod` ni atomicité »** — c'était vrai au
   moment de l'audit ; **corrigé depuis** par Q1.4, avec en plus un contrôle au doctor qui
   regarde l'état réel sur disque.
3. **« Édition de prompt → exécution de script »** — les prompts sont une **liste fermée**
   (`prompt_files.PROMPT_FILES`), avec garde non-vide et sauvegarde `.bak`. Le trajet réel vers
   l'exécution passe par le mode YAML brut de la configuration (S1.6), pas par les prompts.

---

## Ordre proposé

1. **S1.3** (une ligne), **S1.2** (`shlex` + `urlsplit`) — le meilleur rapport effet/effort du
   document.
2. **S1.1** et **S1.4** — inverser deux défauts dangereux ; courts, mais ils changent le
   comportement au boot, donc à faire avec leurs tests de non-régression.
3. **S1.5** puis **S1.6** — les deux qui demandent de lire des routes et des chemins.
4. **S3** en passant.
5. **S2** avant d'activer Zoom RTMS ou Teams en production — pas après.

Aucun de ces points ne demande de refonte. C'est délibéré : une passe sécurité qui exige une
réécriture n'est pas appliquée, et une passe sécurité non appliquée ne protège de rien.
