# Passe sécurité — août 2026

État de référence : `132ddfd` sur `main`, après la passe qualité (`docs/PASSE_QUALITE_2026-08.md`).
**Passe terminée le 2026-08-02** (`ef156cf`) — voir le *Bilan* en fin de document.
Le modèle de sécurité existant est décrit dans `docs/SECURITY_MODEL.md` ; ce document ne le
remplace pas, il liste **ce qui manque encore** et, tout aussi important, **ce qu'on refuse de
faire**.

Chaque constat ci-dessous a été **vérifié dans le code**, pas déduit d'une lecture rapide :
les références de fichier et de ligne ont été ouvertes une par une. Quatre affirmations
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

## Vague S1 — vrais défauts, même sur une machine locale  ✅ **LIVRÉE**

Six points. Ils ont en commun de ne dépendre d'aucune exposition réseau : ils sont faux
aujourd'hui, sur l'installation telle qu'elle tourne — et le resteraient si le portail ne
sortait jamais de sa machine.

### S1.1 — Le service d'inférence échoue OUVERT  ✅ **LIVRÉE**

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

**Critère d'acceptation :** clé absente sans intention explicite → boot refusé ; drapeau de
mode ouvert → démarre avec un avertissement ; clé présente → comportement inchangé. **11 tests.**

**Deux ajustements de périmètre, décidés à l'écriture :**

1. **La condition « écoute sur loopback » n'a pas été retenue** : le service tourne sous
   gunicorn, qui porte l'adresse d'écoute — la fabrique d'application ne peut pas l'observer
   de façon fiable. Prétendre la vérifier aurait donné une garantie fausse. C'est donc le
   **drapeau explicite** qui porte toute la charge, et la documentation qui dit « loopback
   uniquement ». Mieux vaut une garde honnête qu'une garde décorative.
2. **`api_key_env` passe à vide dans les défauts.** Elle valait
   `TRANSCRIA_INFERENCE_API_KEY` : avec la nouvelle règle, *toute* installation aurait été
   refusée, et le drapeau de mode ouvert ne l'aurait pas sauvée (déclarer la variable dit
   « authentifié »). La déclarer devient un **acte volontaire** — ce qui est précisément ce
   qui rend la règle « déclarée mais absente → refus » lisible plutôt qu'absurde.

**Le second défaut fail-open, traité avec :** `resolve_safe_audio_path` autorisait tout
chemin quand `allowed_audio_roots` était vide — et cette clé n'a ni défaut ni exemple, alors
que `file_ref` est le transport **par défaut**. Refuser franchement aurait cassé toutes les
installations ; la borne est donc **déduite de `storage.jobs_dir`**, là où vit l'audio
légitime. Un chemin hors racine répond **403 avant 404** : répondre « introuvable » ferait du
service un oracle d'existence de fichiers.

**Changement cassant, assumé et documenté** au CHANGELOG : un nœud d'inférence dont la
variable de clé a disparu ne démarrera plus. C'est le but.

### S1.2 — Le kit runner interpole une URL dans du Bash  ✅ **LIVRÉE**

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

### S1.3 — La révocation de session tient à un fil (et l'audit se trompait)  ✅ **LIVRÉE**

L'audit annonçait qu'une désactivation de compte ne coupait pas les sessions en cours.
**C'est faux, et la vérification a été instructive.** `flask_login.UserMixin` définit :

```python
@property
def is_authenticated(self):
    return self.is_active
```

Or le modèle `User` possède une **colonne** `is_active`, qui écrase cette propriété.
`@login_required` teste `is_authenticated` — donc un compte désactivé est déjà rejeté à la
requête suivante. Mesuré : `User(is_active=False).is_authenticated` vaut `False`.

**Ce qui reste vrai, et qui est le vrai sujet :** cette protection n'est écrite **nulle part
dans le projet**. Elle repose entièrement sur un détail d'implémentation d'une bibliothèque
tierce, que personne ne relit. Le jour où quelqu'un définit un `is_authenticated` sur le
modèle — pour gérer la double authentification, par exemple — la révocation disparaît **sans
qu'aucun test ne rougisse**.

**Correction (défense de ceinture, pas un correctif de trou) :** le chargeur de session
refuse explicitement un compte inactif, et **deux tests épinglent le comportement** — c'est
eux qui comptent. Un test qui échoue le jour où la propriété est redéfinie vaut mieux qu'une
protection invisible.

**Reste, et non traité ici :** un **changement de mot de passe** ne ferme pas les sessions
ouvertes ailleurs (`is_active` ne bouge pas). La voie propre est un compteur de version
d'identité dans `get_id()`. C'est un vrai chantier de session, pas une ligne ; à trancher
séparément — la désactivation était le cas urgent, et il était déjà couvert.

### S1.4 — Le compte d'amorçage `CHANGE-ME`  ✅ **LIVRÉE**

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

**Critère d'acceptation :** aucune sentinelle ne devient jamais le mot de passe ; le secret
généré est solide, affiché **une seule fois**, différent à chaque installation ; un mot de passe
réellement choisi par l'exploitant est respecté ; l'exemple public ne contient plus de secret
utilisable. **7 tests**, vérifiés en échec sans le correctif.

**Un arbitrage revu : générer plutôt que refuser.** Le document proposait de faire *refuser le
boot* sur la sentinelle. À l'écriture, c'est le mauvais choix : refuser laisse l'exploitant
dehors le jour de son installation, pour un gain nul — le problème n'est pas qu'il démarre,
c'est que le secret soit **public**. La génération le supprime sans rien lui demander. Le
refus aurait aussi transformé une mise à jour en panne pour les installations existantes.

**Effet de bord assumé :** `first_admin_password` **vide** devient la valeur normale, donc le
validateur de schéma ne peut plus exiger une chaîne non vide. Il distingue désormais *absente*
(erreur : clé oubliée) de *vide* (choix explicite), et avertit sur les sentinelles héritées de
l'ancien exemple. Le golden du schéma, lui, est resté intact — c'est ce qui a rendu la
distinction évidente.

**Note sur la suite de tests :** elle utilisait `admin-change-me` comme mot de passe
d'amorçage, devenu une sentinelle. Les tests qui se connectent comme administrateur d'amorçage
ont reçu un vrai secret ; ceux qui testent le **bandeau** « mot de passe par défaut » gardent
la sentinelle — ils portent sur un compte existant, cas qui n'a pas changé.

### S1.5 — Lire et modifier un job passent par la même porte  ✅ **LIVRÉE**

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

**Critère d'acceptation :** un `VIEWER` du même groupe lit (200) et se voit refuser (403) la
réécriture du SRT, la relance d'un traitement et la modification du contexte ; propriétaire,
admin, `OPERATOR` et `MANAGER` conservent leurs droits. **13 tests**, dont les trois de route
vérifiés en échec sans le correctif.

**Le périmètre réel, mesuré :** un balayage AST des routes mutantes utilisant la garde de
lecture en a trouvé **26**. Sur ces 26, **trois n'étaient pas vulnérables** et n'ont pas été
touchées — elles portent déjà une permission que le `VIEWER` n'a pas (`SCHEDULE_MEETINGS` pour
annuler/replanifier une réunion, `DELETE_JOBS` pour supprimer un job). L'affirmation générale
de l'audit ne s'appliquait donc pas partout ; les vérifier une par une valait mieux que
convertir en masse. **23 routes** basculées, plus **4 de l'éditeur SRT** — invisibles au
premier balayage parce qu'elles passent par une aide locale, et pourtant le cas le plus net :
elles remplacent le livrable.

**Une permission plutôt qu'un test de rôle :** `Permission.EDIT_SHARED_JOBS`, accordée à
`ADMIN`, `MANAGER` et `OPERATOR`. Écrire `role != VIEWER` aurait marché aujourd'hui et cassé
au premier rôle ajouté ; la matrice de permissions existait, elle méritait la réponse.

**Deux nuances retenues dans la règle :** le propriétaire garde toujours la main sur son
propre job, même rétrogradé depuis (lui retirer son travail serait une surprise, pas une
protection) ; et le message de refus dit maintenant *lequel* des deux droits manque —
« Accès interdit » ou « Modification interdite ».

### S1.6 — Un chemin de script libre, exécuté en root  ✅ **LIVRÉE**

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

**Critère d'acceptation :** chemin hors racine, traversée `../`, lien symbolique **sortant**,
fichier inscriptible par tous, chemin absent, répertoire → refus avec un message qui dit le
remède. **16 tests.**

**Le périmètre était plus large que l'audit ne le disait :** il citait `gpu.arbitrage_script`.
En cherchant *tous* les endroits où un chemin de configuration est exécuté, on en trouve
**trois** — `services.arbitrage_script`, `services.stop_script` et
`resource_node.engines[].script` (les lanceurs de moteurs STT). Le troisième est le plus
discret : il vient d'un manifeste, pas d'un champ que l'on regarde.

**Décisions prises à l'écriture :**

- **la vérification porte sur le chemin RÉSOLU**, et c'est ce chemin-là qui est exécuté. Sans
  quoi la garde et l'usage porteraient sur deux choses différentes, et
  `scripts/piege.sh -> /tmp/charge.sh` passerait tranquillement ;
- **l'écriture par le groupe est tolérée**, seule `o+w` est refusée. `775` est courant sur un
  dépôt d'équipe ; une garde qui gêne l'exploitant normal finit désactivée, et ne protège
  alors plus personne. On refuse ce qui est réellement dangereux : un script que **n'importe
  quel compte** de la machine peut réécrire ;
- **pas d'exigence de propriété root** : sur le déploiement de référence le dépôt appartient à
  l'utilisateur applicatif et le service tourne en root — l'exiger aurait tout cassé pour un
  gain nul ;
- ~~la clé vit sous `security.allowed_script_roots`~~ → **CORRIGÉ** : c'était le défaut de
  conception de cette première version. Une clé de configuration est éditable par
  l'administrateur applicatif lui-même, donc l'allowlist ne contraignait pas l'acteur
  qu'elle visait. Les racines viennent désormais de l'**environnement du service**
  (`TRANSCRIA_SCRIPT_ROOTS`) et la clé de configuration est supprimée — voir « Reprise après
  second audit ». Elles **s'ajoutent** toujours à `<dépôt>/scripts`, qui reste autorisée.

**Toujours écarté :** le passage des services en non-root. Voir la section « Écarté ».

---

## Vague S2 — ce qui devient nécessaire à l'exposition  ✅ **LIVRÉE** (sauf S2.3, différée)

Ces trois points ne sont pas urgents tant que le portail reste local. Ils le deviennent le jour
où Zoom RTMS ou Teams est activé — c'est-à-dire le jour où une URL du portail est publiée. Les
poser avant évite d'avoir à les poser dans l'urgence, au moment précis où l'on a autre chose à
faire.

### S2.1 — Les défauts de transport, et ce que le doctor doit en dire  ✅ **LIVRÉE**

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

**Livré — et le catalogue portait déjà le bon signal.** Plutôt que de coder « zoom » et
« teams » en dur, le contrôle lit `path: webhook` dans `meeting_connectors.yaml` : c'est
exactement « exige un point d'entrée HTTPS public ». Tout connecteur futur déclaré ainsi sera
couvert sans qu'on y pense. Un connecteur dont les identifiants ne sont qu'à moitié saisis ne
déclenche rien — des champs en cours de remplissage ne sont pas un déploiement exposé. Meet,
qui fonctionne en *pull*, n'est pas concerné : c'est précisément ce que la lecture de `path`
permet de distinguer. **6 tests**, dont la non-régression du WARN fédéré historique et la
priorité du plus grave quand les deux se présentent.

### S2.2 — Requêtes sortantes pilotées par une valeur d'utilisateur  ✅ **LIVRÉE**

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

**Critère d'acceptation revu à l'écriture — et c'est le point important.** La recette
habituelle anti-SSRF (refuser toutes les adresses privées) est **fausse ici**, pour deux
raisons qui se cumulent : TranscrIA est auto-hébergé, donc l'instance Visio vit sur le réseau
de l'exploitant ; et **un réseau local n'est pas forcément en adressage privé** — une
organisation disposant d'un bloc public s'en sert en interne, et son instance est alors sur
une IP publique *et* sur son LAN (remarque d'exploitant, 2026-08-02).

**L'adresse ne dit donc pas si l'on est « chez soi ».** Une garde bâtie sur
« privé = interne » refuserait des déploiements légitimes tout en manquant son objet. D'où
deux niveaux :

1. **toujours refusé** — ce qui n'est *jamais* une instance de visioconférence : boucle
   locale, adresse « toutes interfaces », lien-local (les **métadonnées cloud**). Ce sont les
   deux pivots réels : atteindre un service qui n'écoute que sur la machine, ou lire des
   identifiants d'instance. **La décision porte sur l'adresse RÉSOLUE**, pas sur la forme
   écrite — la première version comparait le texte et cinq notations la contournaient (voir
   « Reprise après second audit ») ;
2. **allowlist stricte** (`VISIO_ALLOWED_HOSTS`) quand l'exploitant la pose — et elle ne peut
   pas rouvrir le niveau 1 : déclarer `localhost` par mégarde ne redonne pas le pivot.

**La garde ne s'applique PAS à `VISIO_API_BASE`** : c'est une valeur d'*exploitant*, qui vise
légitimement la machine locale (la stack de développement officielle). La contrôler
reviendrait à se protéger de soi-même. Distinguer les deux est tout l'objet du correctif : on
borne ce que l'**utilisateur** choisit, pas ce que l'exploitant règle. Un test existant l'a
signalé — ma première version débordait.

**Piège de test rencontré :** `resolve_livekit_room` attrape `Exception` pour retomber sur le
slug. Un espion qui *lève* est donc avalé par ce repli — mes deux tests d'intégration
passaient avec ET sans la garde, c'est-à-dire ne prouvaient rien. Ils **enregistrent**
désormais l'appel et vérifient qu'il n'a pas eu lieu. **21 tests.**

### S2.3 — Tous les exécutants partagent un seul principal  ⏸ **DIFFÉRÉE, à raison**

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

**Non livrée, et c'est le bon choix :** il n'y a aujourd'hui **qu'un seul exécutant**. Le
défaut est donc nul en pratique, et toute correction serait écrite sans le cas d'usage qui
lui donnerait sa forme — c'est ainsi qu'on produit une abstraction qui ne sert pas. À
reprendre **le jour où un deuxième exécutant est posé**, pas avant.

---

## Vague S3 — trois lignes chacun, à faire en passant  ✅ **LIVRÉE**

Rien ici ne mérite un chantier. Tout mérite d'être fait pendant qu'on a le fichier ouvert.

| Point | Correction |
|---|---|
| **`/ready` divulgue l'erreur de base BRUTE, sans authentification** — `health_routes.py:113` n'a aucun `@login_required` et renvoie `str(exc)` de l'exception SQLAlchemy, qui porte l'URI de connexion : hôte, port, utilisateur, nom de base. `/metrics` est anonyme aussi. | statut binaire pour l'anonyme, détail réservé aux **administrateurs** |
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

**Livré — 22 tests, les cinq décisifs vérifiés en échec sans leur correctif.** Ce que
l'écriture a précisé :

- **`/ready` ne supprime pas l'information, il la RÉSERVE.** La sonde anonyme garde son
  oui/non ; un **administrateur** voit toujours le motif technique. (Première version :
  tout compte authentifié — restreint au troisième passage, l'URI de connexion étant de la
  topologie d'infrastructure, pas une information de travail.) Supprimer purement et
  simplement aurait forcé l'administrateur à aller lire les journaux du serveur pour
  diagnostiquer une base tombée — on aurait échangé une fuite contre une gêne ;
- **la garde CSV préfixe, elle ne remplace pas.** La valeur reste entièrement lisible.
  Remplacer ou supprimer aurait abîmé des libellés légitimes (« budget -- révisé ») et la
  garde aurait fini désactivée. Les blancs de tête sont couverts : `"\t=1+1"` s'exécute
  aussi, un tableur les retire **avant** d'interpréter ;
- **le budget de décompression lit l'en-tête du ZIP**, sans rien décompresser : c'est la
  somme des tailles *déclarées* qui décide. Plafond volontairement généreux (200 Mo) — un
  support de réunion volumineux mais légitime doit passer ;
- **l'upload accepte toujours des `bytes`.** Le flux est recopié par blocs de 1 Mio, mais
  les appelants historiques n'ont pas eu à changer. Le test le prouve avec un flux qui
  **refuse** d'être lu d'un coup, plutôt que d'espérer le bon comportement.

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

## Quatre affirmations de l'audit source non retenues

Elles sont plausibles à la lecture, fausses ou trompeuses à la vérification. Les consigner évite
qu'elles reviennent.

1. **« CSRF, CSP, `Secure`, TLS/HSTS désactivés »** — ils ne sont pas *absents*, ils sont
   **opt-in et complets**, et `SameSite=Lax` + `HttpOnly` sont actifs par défaut. Le vrai
   manque est le signal qui les rattache à l'exposition (S2.1), pas le code.
2. **« Le fichier de configuration est écrit sans `chmod` ni atomicité »** — c'était vrai au
   moment de l'audit ; **corrigé depuis** par Q1.4, avec en plus un contrôle au doctor qui
   regarde l'état réel sur disque.
3. **« Désactiver un compte ne révoque pas ses sessions »** — faux : `UserMixin.
   is_authenticated` délègue à `is_active`, que le modèle définit en colonne. La protection
   existe ; ce qui manquait, c'est qu'elle soit **écrite et testée** plutôt que déduite d'une
   bibliothèque tierce (voir S1.3).
4. **« Édition de prompt → exécution de script »** — les prompts sont une **liste fermée**
   (`prompt_files.PROMPT_FILES`), avec garde non-vide et sauvegarde `.bak`. Le trajet réel vers
   l'exécution passe par le mode YAML brut de la configuration (S1.6), pas par les prompts.

---

## Reprise après second audit (2026-08-02)

Un second passage de l'analyse externe a trouvé **cinq défauts réels**, dont **trois qui
m'appartiennent**. Ils sont corrigés ; les consigner ici vaut mieux que de les lisser.

### Ce que j'avais manqué, et pourquoi

**Deux fois, j'ai corrigé l'INSTANCE nommée par l'audit au lieu de la CLASSE.**

- `/ready` corrigé, **`/health` non** — cinq lignes plus haut, défaut identique, même
  fonction d'aide. Les deux sondes partagent désormais la même garde, et le test est
  paramétré sur les deux routes ;
- le **lancement** de l'arbitrage LLM avait reçu la garde de script, son **arrêt** non
  (`vram_manager`). Le trajet restait ouvert par l'autre bout.

C'est exactement le reproche que j'adressais à l'audit ailleurs dans ce document — vérifier
le périmètre plutôt que la ligne citée. Je ne l'ai pas appliqué à moi-même.

### Un défaut de conception : la garde de script ne contraignait personne  🔴

`security.allowed_script_roots` était une **clé de configuration**. Or `/admin/config`
propose un mode YAML brut : l'administrateur applicatif réglait donc lui-même les bornes
censées le contraindre. Il lui suffisait d'ajouter `/tmp`. Et la chaîne complète existait :
le répertoire des prompts est aussi configurable, le **contenu** des prompts est libre —
écrire un fichier choisi, le désigner comme script, autoriser sa racine.

**Une garde dont l'acteur visé fixe les limites ne protège de rien.** Les racines viennent
désormais de l'**environnement du service** (`TRANSCRIA_SCRIPT_ROOTS`, unité systemd),
hors de portée du formulaire d'administration. `<dépôt>/scripts` reste toujours autorisée.
La clé de configuration est **supprimée** plutôt que dépréciée : la laisser aurait entretenu
l'illusion qu'elle sert.

### La garde SSRF regardait la forme, pas la destination  🔴

Cinq contournements, tous vérifiés :

| Contournement | Cause |
|---|---|
| `http://2130706433/`, `0x7f000001`, `017700000001`, `127.1` | le résolveur système accepte ces formes, `ipaddress` les rejette — elles passaient pour des noms de domaine |
| un nom qui **résout** vers `127.0.0.1` | un attaquant contrôle son propre DNS |
| une **redirection** vers la boucle locale | `urlopen` suit les redirections par défaut |

La cause était écrite noir sur blanc dans mon propre module : *« on ne résout PAS »*. La
garde **résout** désormais, et **une seule** adresse interdite suffit à refuser (un nom
multi-adresses ne passe pas parce que la première est saine). Un nom irrésolvable est
refusé : ne pas savoir où l'on va n'est pas une raison d'y aller. L'ouvreur HTTP ne suit
plus les redirections — vérifier puis laisser la bibliothèque aller ailleurs, c'est ne pas
vérifier.

**Limite assumée et écrite dans le code :** entre la vérification et la requête, le DNS peut
changer (*DNS rebinding*). La fermer demanderait d'épingler l'adresse jusqu'à la connexion —
non proportionné ici, où le repli est le nom de la salle et où aucune réponse n'est renvoyée
à l'utilisateur.

### Ce qui n'est pas un défaut de code

**Le `config.yaml` déployé est encore en `0644`.** Le code force `0600` à chaque écriture
(Q1.4) et le doctor le signale, mais un fichier créé *avant* le correctif garde ses
permissions tant que personne n'enregistre depuis l'interface. C'est un geste d'exploitation,
pas un correctif : `chmod 600 config.yaml`. Un enregistrement depuis l'interface suffit aussi.

**`PhaseOutcome` : migration terminée là où elle était annoncée.** Le pipeline renvoie du
typé de bout en bout ; le **runner d'étape** renvoie encore des dictionnaires, et
`from_legacy_dict` ne sert plus qu'à lui. Ce n'est pas un reste oublié — c'est écrit dans
Q2.1 de la passe qualité, avec la raison. Le gate E2E GPU réel de Q2.1 reste, lui,
réellement à jouer.

---

## Troisième passage (2026-08-02) — la chaîne n'était pas fermée

Le second correctif de S1.6 déplaçait l'allowlist hors de portée de l'administrateur. Un
troisième passage a montré que **cela ne suffisait pas** : il pouvait toujours écrire *à
l'intérieur* d'une racine autorisée.

```
workflow.prompts_dir := <dépôt>/scripts     (clé de configuration, éditable)
  → enregistrer un prompt                    (NOM dans une liste fermée, CONTENU libre)
  → le fichier atterrit dans une racine autorisée
  → services.arbitrage_script := ce fichier
  → le pré-lancement LLM l'exécute
```

**Le principe qui manquait : un exécutable ne vit pas dans une zone où l'application
écrit.** `safe_script_path` refuse désormais tout script situé sous `workflow.prompts_dir`
ou `storage.jobs_dir`. Une racine autorisée ne vaut que si personne ne peut y déposer un
fichier.

**Trois autres réserves traitées :**

- **le détail SQL était visible de tout compte authentifié.** L'URI de connexion (hôte,
  port, utilisateur, base) n'est pas une information de travail pour un rédacteur de comptes
  rendus : c'est de la topologie d'infrastructure. Réservée aux **administrateurs** ;
- **`TRANSCRIA_SCRIPT_ROOTS` n'existait nulle part** hors du code. Ajoutée aux unités
  systemd (commentée, avec la raison de son emplacement) et à `.env.example` ;
- **pollution d'environnement entre tests** : j'écrivais `os.environ[...]` directement au
  lieu de `monkeypatch`, donc la variable fuyait vers les tests suivants. Corrigé — c'est
  exactement le genre de défaut qui rend une suite verte pour de mauvaises raisons.

### Un réseau local n'est pas forcément en adressage privé

Remarque d'exploitant qui **corrige le raisonnement** de S2.2, pas seulement sa rédaction.
J'écrivais « l'instance Visio vit sur le LAN (`192.168.x`, `10.x`) ». C'est faux en général :
une organisation disposant d'un bloc d'adresses **publiques** s'en sert en interne, et son
instance est alors sur une IP publique *et* sur son réseau local.

**L'adresse ne dit donc pas si l'on est « chez soi ».** Le correctif tenait déjà — la garde
ne borne aucune plage, ni privée ni publique — mais la justification était bancale, et une
justification bancale se transforme tôt ou tard en règle fausse.

Ce cas est aussi celui où l'**allowlist** compte le plus : c'est le seul mécanisme capable
de distinguer « mon réseau » d'Internet quand l'adressage ne le dit pas. Un contrôle au
doctor la signale désormais quand le connecteur Visio est configuré sans elle — un mécanisme
facultatif que personne ne découvre ne protège personne.

### Ce qui reste ouvert, sciemment

- **DNS rebinding** : entre la vérification et la requête, le nom peut changer d'adresse. Le
  fermer demanderait d'épingler l'adresse résolue jusqu'à la connexion. Non retenu ici : le
  repli est le nom de la salle, et **aucune réponse n'est renvoyée à l'utilisateur** — le
  gain ne paie pas la mécanique ;
- **`config.yaml` en `0644` sur l'installation déployée** : le code force `0600` depuis Q1.4
  et le doctor le signale, mais un fichier créé avant garde ses permissions. `chmod 600
  config.yaml`, ou un simple enregistrement depuis l'interface.

---

## Quatrième passage (2026-08-02) — le temps, et deux gardes inertes

Trois défauts, tous fondés. Ils partagent un motif : **une protection qui existe mais ne
s'applique pas**.

### La garde S1.6 ne voyait que l'instant présent

Interdire qu'un script vive sous `workflow.prompts_dir` fermait le cas *simultané*, pas le
cas **temporel** :

```
prompts_dir := <racine autorisée>   → enregistrer un prompt (contenu libre)
prompts_dir := ailleurs             → le fichier, lui, RESTE
services.arbitrage_script := ce fichier   → plus aucun chevauchement visible → exécuté
```

**Un fichier persiste ; une configuration change.** Une garde qui n'observe que la
configuration courante ne peut donc rien contre le passé. La barrière est désormais à
l'**écriture** — `prompt_files` refuse un `prompts_dir` sous une racine exécutable — ce qui
est permanent. Celle de `script_guard` demeure en seconde ligne.

### Le contrôle Visio du doctor ne se déclenchait jamais

Je cherchais des clés commençant par `VISIO_`. Le connecteur déclare `LIVEKIT_*`. Le
contrôle ajouté au troisième passage était donc **mort-né** — et mon test passait parce
qu'il inventait une clé `VISIO_LIVEKIT_URL` au lieu de lire le catalogue.

**Un test qui valide l'hypothèse de son auteur ne vérifie rien.** Le contrôle lit
maintenant le catalogue, et le test construit sa configuration depuis la même donnée.

### L'allowlist n'atteignait pas le bot

`VISIO_ALLOWED_HOSTS` est lue par la garde qui tourne **dans le conteneur**. Elle n'était
pas dans `_MACHINE_ENV`, la liste des variables relayées : posée sur l'hôte, elle ne
parvenait jamais au bot, qui voyait donc une liste vide et ne bornait rien.

Le plus gênant : la liste porte déjà le commentaire *« vécu : posés au runner mais jamais
relayés au conteneur »*, laissé après le même oubli sur `BOT_IDLE_TIMEOUT_S`. L'avertissement
était écrit à l'endroit exact — je ne l'ai pas lu. Deux tests couvrent désormais le relais,
dont un qui vérifie que la **valeur** ne passe pas par `argv` (visible de tout `ps`).

---

## Cinquième passage (2026-08-02) — dont une régression que j'avais créée

### `/admin/config` répondait 500, à cause de ma propre garde

Le pire endroit où placer un refus. Ma garde levait au **chargement** des prompts, donc la
page d'administration tombait — et l'administrateur ne pouvait même plus corriger le
réglage fautif. Un refus sur un chemin de lecture enferme dehors la personne qui doit
réparer.

**Deux places, deux rôles :** refus à la **validation** (le mauvais réglage n'entre jamais,
et l'interface affiche une erreur lisible comme pour n'importe quelle clé) ; **dégradation**
à la **lecture** (une installation qui porte déjà le mauvais réglage reste affichable et
corrigeable — aucun prompt montré, aucune exception).

### La même chaîne existait par `storage.jobs_dir`

J'avais corrigé les prompts. `storage.jobs_dir` reçoit des fichiers d'**utilisateurs** : le
même trajet marchait avec un audio uploadé. Encore une fois, j'avais traité l'instance et
pas la classe. La vérification est désormais **généralisée à toutes** les zones
inscriptibles, en un seul endroit (`zones_inscriptibles_en_conflit`), appelé par la
validation de configuration.

### Tout `_MACHINE_ENV` était inerte sous l'installation nominale

Le quatrième passage avait fait relayer `VISIO_ALLOWED_HOSTS` du runner vers le conteneur.
Mais **aucune unité systemd ne chargeait de fichier d'environnement** : la variable — comme
`VISIO_API_BASE`, `BOT_HIDDEN` et les identités machine — n'atteignait jamais le processus
runner. Le relais fonctionnait, la source était vide. `EnvironmentFile=-` ajouté aux trois
unités (kit runner, installeur runner, service Meet), avec deux tests qui l'exigent.

### Ce que le cliquet d'architecture a refusé

En voulant éviter un import différé (qui coûte un point au cliquet), j'ai remonté l'import
en tête de module. Le détecteur de cycles l'a refusé : `gpu → vram_manager → config →
config_schema → orchestration → gpu`. J'ai donc gardé l'import différé et **assumé le
point** — un cycle inter-paquets coûte infiniment plus cher qu'une ligne de métrique. Le
filet a fait exactement son travail : m'empêcher d'abîmer l'architecture pour un chiffre.

---

## Bilan

**Tout est livré, sauf un point volontairement reporté.** Neuf correctifs, chacun poussé
séparément avec ses tests et la CI verte avant d'enchaîner — puis **trois passages de
relecture externe**, qui ont trouvé de vrais défauts à chaque fois.

| | Sujet | Tests |
|---|---|---:|
| S1.1 ✅ | Service d'inférence fail-closed (clé + `file_ref` borné) | 11 |
| S1.2 ✅ | URL du kit runner validée puis échappée | 19 |
| S1.3 ✅ | Révocation de session épinglée (l'audit se trompait) | 2 |
| S1.4 ✅ | Plus de mot de passe d'amorçage publié | 7 |
| S1.5 ✅ | Lecture et écriture séparées sur les jobs | 13 |
| S1.6 ✅ | Scripts en root : racine hors config **+** interdiction des zones inscriptibles | 26 |
| S2.1 ✅ | Transport rattaché aux connecteurs à webhook (FAIL) | 6 |
| S2.2 ✅ | Requête sortante bornée — **durcie** : résolution, notations numériques, redirections | 37 |
| S3 ✅ | `/health` **et** `/ready`, formules CSV, décompression, upload en flux | 26 |
| **S2.3 ⏸** | **Un principal par exécutant** | **différée** |

**Ce qui reste, et pourquoi :** S2.3 seulement. Il n'y a qu'un seul exécutant — le défaut
est nul en pratique, et le corriger maintenant produirait une abstraction écrite sans le cas
d'usage qui lui donnerait sa forme. **À reprendre le jour où un deuxième exécutant est
posé.** Voir aussi la section « Écarté volontairement », qui n'est pas une liste d'attente :
ces points sont refusés, pas reportés.

**Trois passages de relecture externe, trois séries de vrais défauts.** C'est le
renseignement le plus utile de ce document :

| Passage | Ce qu'il a trouvé |
|---|---|
| 2ᵉ | deux corrections d'**instance au lieu de classe** (`/health` oublié à côté de `/ready`, l'arrêt de l'arbitrage à côté de son lancement) ; une allowlist que l'administrateur réglait lui-même ; une garde SSRF jugeant la forme écrite plutôt que la destination |
| 3ᵉ | la chaîne S1.6 **toujours ouverte** (écrire un prompt *dans* une racine autorisée) ; le détail SQL visible de tout compte authentifié ; `TRANSCRIA_SCRIPT_ROOTS` absente des unités et guides ; **pollution d'environnement entre mes tests** |
| exploitant | « un LAN n'est pas forcément en adressage privé » — le code tenait, la **justification** était fausse |
| 4ᵉ | S1.6 contournable dans le **temps** (déposer, puis déplacer la configuration) ; le contrôle Visio du doctor cherchait des clés inexistantes ; `VISIO_ALLOWED_HOSTS` jamais relayée au conteneur bot |
| 5ᵉ | la même chaîne par `storage.jobs_dir` ; **une régression que j'avais créée** (`/admin/config` en 500) ; aucune unité systemd ne chargeait de fichier d'environnement, rendant tout `_MACHINE_ENV` inerte |

Aucun de ces passages n'a produit un faux positif sur le fond. Trois de mes correctifs
étaient incomplets, et je ne l'aurais pas vu seul : les deux premiers parce que j'avais
corrigé ce qu'on me montrait plutôt que la classe du défaut, le troisième parce qu'une
garde ne vaut que si l'on cherche activement par où on l'aurait contournée.

**Deux ajouts au diagnostic** que ces passages ont motivés : un contrôle qui **échoue** si
un connecteur à webhook public tourne sans TLS, et un rappel de l'allowlist sortante Visio
quand elle n'est pas posée — un mécanisme facultatif que personne ne découvre ne protège
personne.

**Un changement cassant**, documenté au CHANGELOG : un nœud d'inférence dont la variable de
clé a disparu ne démarre plus. Le portail tout-en-un n'est pas concerné.

**Ce que la méthode a rapporté.** Écrire les tests négatifs *avant* le correctif, et vérifier
qu'ils échouent sans lui, a changé trois conclusions :

- **S1.3 n'était pas un défaut.** Le test est passé sans le correctif :
  `flask_login.UserMixin.is_authenticated` délègue à `is_active`. La protection existait —
  mais nulle part dans ce projet, suspendue à un détail d'une bibliothèque tierce. Elle est
  désormais écrite et testée ;
- **deux tests de S2.2 ne prouvaient rien.** `resolve_livekit_room` attrape `Exception` pour
  retomber sur le slug : un espion qui *lève* est avalé par ce repli, et le test passait avec
  ET sans la garde ;
- **le périmètre a bougé dans les deux sens.** S1.6 touchait **trois** sources de chemins
  exécutables, pas une. À l'inverse, sur les 26 routes mutantes de S1.5, **trois n'étaient
  pas vulnérables** — elles portaient déjà une permission que le `VIEWER` n'a pas.

Aucun de ces points n'a demandé de refonte. C'était délibéré : une passe sécurité qui exige
une réécriture n'est pas appliquée, et une passe sécurité non appliquée ne protège de rien.
