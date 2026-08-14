# Modèle de sécurité TranscrIA

> Chantiers C3.3 / C3.4 / C3.9 (docs/archive/RELEASE_0.2.0.md). Document destiné à un DPO /
> RSSI : ce qui protège l'accès, qui peut faire quoi, ce qui est durci et ce qui est
> une **limitation assumée** (jamais « inconnue »).

## 1. Rôles et permissions

Quatre rôles hiérarchiques (`transcria/auth/permissions.py`) plus l'**admin de
groupe** (appartenance à un groupe avec droit d'administration, orthogonal au rôle).

| Permission | Viewer | Operator | Manager | Admin |
|---|:---:|:---:|:---:|:---:|
| Télécharger les livrables | ✅ | ✅ | ✅ | ✅ |
| **Modifier un traitement PARTAGÉ** | | ✅ | ✅ | ✅ |
| Créer un traitement | | ✅ | ✅ | ✅ |
| Voir les rapports qualité | | ✅ | ✅ | ✅ |
| Voir TOUS les traitements | | | ✅ | ✅ |
| Relancer un traitement | | | ✅ | ✅ |
| Supprimer un traitement | | | | ✅ |
| Gérer les utilisateurs | | | | ✅ |
| Gérer la configuration | | | | ✅ |
| Accès page Système | | | | ✅ |
| Gérer la planification | | | | ✅ |

Les **lexiques centraux** et le **partage de types de réunion** sont gérés par les
admins de groupe (et les admins) — voir `CentralLexiconStore.can_manage_lexicons`.
Un utilisateur ne voit que les traitements de ses groupes (sauf `VIEW_ALL_JOBS`).

**Voir et modifier sont deux droits distincts** (0.4.0). Jusque-là, les routes mutantes se
contentaient de la garde de LECTURE — un compte `VIEWER` partageant un groupe avec le
propriétaire pouvait donc **réécrire le sous-titrage** d'un travail qui n'était pas le sien.
`can_edit_job` exige désormais `EDIT_SHARED_JOBS` en plus du lien de groupe, et le refus
indique lequel des deux droits manque. Le propriétaire garde la main sur son propre travail,
même si son rôle a été rétrogradé depuis.

**Garde de non-régression** : chaque route mutante est protégée par `@requires(...)`
ou `@login_required` ; les tests RBAC couvrent les refus (403) par rôle.

## 2. Authentification et sessions (C3.3)

- Mots de passe hachés (jamais stockés en clair) ; longueur minimale imposée.
- **Cookie de session** : `HttpOnly`, `SameSite=Lax`, `Secure` activable
  (`security.session_cookie_secure`, ou automatiquement quand
  `security.behind_tls_proxy: true` — voir §7).
- **Durée de session explicite** : `PERMANENT_SESSION_LIFETIME` = 12 h par défaut
  (`auth.session_lifetime_hours`) — plus de session « jusqu'à fermeture du
  navigateur » imprévisible.
- **Anti-bourrinage** (`transcria/auth/rate_limit.py`) : 5 échecs par (IP, identifiant)
  en 5 min → blocage 5 min (429), journalisé en audit (`login_failed` avec motif). La
  clé repose sur l'**adresse socket** (`request.remote_addr`), JAMAIS sur
  `X-Forwarded-For` — cet en-tête est client-contrôlé, le faire varier à chaque
  tentative contournerait le seuil (revue de sécurité, chantier identité). En
  mono-process (déploiement local) le compteur est global ; en multi-process chaque
  worker a le sien (le blocage reste efficace, une même IP se répartit mal).
- **Échecs de connexion journalisés** (`AuditAction.LOGIN_FAILED`, avec identifiant tenté).
- **CSRF** : trois couches, de la plus légère à la plus forte.
  1. `SameSite=Lax` (toujours actif) — bloque l'envoi du cookie sur les POST cross-site.
  2. `security.csrf_origin_check` (opt-in) — refuse (403) un POST cookie dont l'en-tête
     `Origin` est croisé ; couvre les vieux navigateurs sans SameSite.
  3. `security.csrf_tokens` (opt-in, **défense la plus forte**) — jeton synchroniseur
     en session, exigé à chaque requête mutante authentifiée par cookie (champ
     `csrf_token` ou en-tête `X-CSRFToken`). Le jeton est injecté AUTOMATIQUEMENT dans
     tous les formulaires et tous les `fetch` par `static/js/csrf.js` (aucun formulaire
     à modifier). `transcria/web/csrf.py` valide en temps constant.
  L'API par jeton (`Authorization: Bearer`) et les requêtes sans en-tête `Origin`/jeton
  d'API sont exemptées (elles ont leur propre authentification). Les **scripts** doivent
  utiliser un jeton d'API (`Bearer`), pas un cookie de session, quand `csrf_tokens` est actif.

## 3. En-têtes de sécurité (C3.9)

Posés sur toutes les réponses (`app.after_request`) :

- `X-Content-Type-Options: nosniff` — pas de devinette de type MIME ;
- `X-Frame-Options: DENY` — anti-clickjacking (l'app ne s'embarque jamais en iframe) ;
- `Referrer-Policy: strict-origin-when-cross-origin` — ne fuite pas les URLs (jetons
  `?next=`) vers l'extérieur ;
- `Strict-Transport-Security` (**HSTS**, opt-in `security.hsts_enabled`) — émis
  UNIQUEMENT sur une réponse réellement servie en HTTPS (jamais sur du HTTP en clair,
  ce qui piégerait le navigateur) ; durée `security.hsts_max_age_days` (défaut 365).

**CSP (Content-Security-Policy)** — opt-in `security.csp` (`off` défaut | `report-only`
| `enforce`), voir `transcria/web/csp.py`. La politique **verrouille** les vecteurs les
plus dangereux quel que soit l'état des scripts : `object-src 'none'`,
`base-uri 'self'`, `frame-ancestors 'none'` (anti-framing), `form-action 'self'`
(anti-détournement de formulaire), `default-src 'self'` (+ CDN Bootstrap listé
explicitement, seule origine tierce). Déploiement sûr : commencer en `report-only`
(le navigateur signale les violations sans bloquer), puis `enforce`.

**`script-src` STRICT** : `'self'` + un **nonce par requête** (`transcria/web/csp.get_request_nonce`,
exposé aux templates via `csp_nonce()`) pour les rares îlots de données inline, SANS
`'unsafe-inline'`. Rendu possible par la migration de tous les gestionnaires inline
(`onclick=`…) vers une délégation `data-action` (`static/js/ui_actions.js`) et le passage
sous nonce des îlots `<script>window.X = …|tojson</script>`. Validé en navigateur
(Playwright) : mode `enforce` → **zéro violation CSP** sur tous les écrans, interactions
fonctionnelles. `style-src` garde `'unsafe-inline'` (Bootstrap pose des styles inline via
JS ; l'injection de STYLE est un risque bien moindre que celle de script).

## 4. Données et secrets

- **Secrets** (`HF_TOKEN`, DSN avec mot de passe) : dans `.env`, jamais versionné,
  jamais embarqué dans une sauvegarde (seule son empreinte figure au manifeste — voir
  `docs/UPGRADE.md`). Une garde de test vérifie qu'aucun motif de secret n'apparaît
  dans les logs d'un E2E.
- **Données biométriques** (empreintes vocales) : stockées dans `voices/`, soumises au
  consentement RGPD (voir `voice_enrollment.consent`). Rétention et purge : voir
  `docs/AUDIT_DPO.md` (C3.10).
- **Uploads** : bornés par `MAX_CONTENT_LENGTH` (`security.max_upload_size_mb`, 1 Go
  par défaut) ; type audio validé à l'analyse.
- **Traversée de chemin** : les fichiers de job sont adressés par UUID + chemin
  relatif contrôlé ; l'autorisation d'accès est vérifiée par propriétaire/groupe sur
  chaque route de téléchargement.

## 5. Déploiement recommandé

- **Reverse proxy TLS** devant l'application (nginx/Caddy) ; activer
  `SESSION_COOKIE_SECURE`.
- **Pare-feu** : n'exposer que le port du proxy ; la base et les nœuds GPU restent sur
  le réseau interne.
- **Permissions fichiers** : `config.yaml`, `.env` et les archives de sauvegarde en
  `600`, propriété de l'utilisateur du service.
- **Sauvegardes chiffrées au repos** si le disque n'est pas déjà chiffré (les archives
  contiennent config + données).

## 6. Identité d'entreprise (SSO, LDAP/AD, proxy) et jetons d'API

Le portail délègue l'authentification à un fournisseur d'entreprise selon
`auth.backend` (`docs/GESTION_IDENTITE.md`). Le défaut `local` (comptes du
portail) ne change pas ; les backends fédérés sont opt-in.

- **Backends** : `oidc` (Authorization Code + PKCE, validation `iss`/`aud`/`exp`/`nonce`,
  aucun refresh token stocké), `proxy` (en-têtes `Remote-User`/`Remote-Groups` crus
  UNIQUEMENT depuis l'adresse socket ∈ `auth.proxy.trusted_ips`, jamais
  `X-Forwarded-For`), `ldap` (LDAP/Active Directory : LDAPS ou StartTLS
  **obligatoire** avec certificat vérifié — en clair refusé au boot sauf
  `allow_plaintext` ; entrée échappée `escape_filter_chars` anti-injection ; mot de
  passe vide refusé avant tout bind ; le compte de service lit, le bind utilisateur
  prouve le mot de passe).
- **Provisionnement JIT** commun : rapprochement sur `(source, external_subject)`
  jamais l'email ; rôle **REMPLACÉ** à chaque login via `role_mapping` (premier match,
  égalité stricte, `default: deny|viewer` — jamais d'élévation implicite) ;
  `is_active=False` local est un **veto** ; un refus de mapping est audité AVEC les
  groupes reçus, l'utilisateur ne voit qu'un message générique (anti-énumération).
- **Comptes fédérés sans mot de passe local** : `password_hash` sentinelle inutilisable
  → `check_password` faux par construction ; `change_password`/`reset-admin-password`
  refusent si `identity_source != local`.
- **Break-glass** : le formulaire local reste servi sur `/login?local=1` (comptes
  locaux uniquement) ; le préflight `doctor` met en **FAIL** un backend fédéré actif
  sans admin local actif (sinon une panne du fournisseur verrouille tout le monde).
- **Jetons d'API personnels** (`tia_<id>_<secret>`) : seul le SHA-256 du secret en base
  (comparaison à temps constant `hmac.compare_digest`), révocation/expiration honorées,
  acceptés via `Authorization: Bearer` sur les routes du contrat scriptable ⭐
  UNIQUEMENT, sans émettre de cookie ; le jeton porte les permissions de son
  propriétaire, jamais plus, et meurt avec la désactivation du compte.
- **Coût nul pour les installations locales** : `authlib` (oidc) et `ldap3` (ldap) sont
  importés de façon différée — jamais chargés en backend `local`.

## 7. Durcissement HTTP(S) (transport) — opt-in

Tout ci-dessous est **désactivé par défaut** (dev / tout-en-un accédé en HTTP reste
fonctionnel) et se règle depuis Administration → Configuration → « Durcissement HTTP(S) ».

- `security.behind_tls_proxy` (défaut `false`) — à activer quand un reverse-proxy
  (nginx, Caddy…) termine le HTTPS devant TranscrIA. Monte `ProxyFix` pour lire le
  **schéma** (`X-Forwarded-Proto`) → l'app sait qu'elle est en HTTPS (cookie `Secure`
  automatique, HSTS possible). **Point de sécurité crucial** : on n'active JAMAIS
  `x_for` — laisser `ProxyFix` réécrire `remote_addr` depuis `X-Forwarded-For`
  (client-contrôlé) rouvrirait le contournement de l'anti-bourrinage. L'IP reste
  l'adresse socket réelle (cohérent avec le connecteur proxy et le rate-limiter).
  **Redémarrage requis.**
- `security.session_cookie_secure` (défaut `false`) — force le flag `Secure` du cookie
  de session (implicite si `behind_tls_proxy`). **Redémarrage requis.**
- `security.hsts_enabled` / `security.hsts_max_age_days` (défaut `false` / `365`) — HSTS
  (§3), émis uniquement sur une réponse HTTPS réelle.
- `security.csrf_origin_check` (défaut `false`) — contrôle d'origine (§2).
- `security.csrf_tokens` (défaut `false`) — jetons CSRF synchroniseurs (§2, défense forte).
- `security.csp` (défaut `off`) — Content-Security-Policy (§3), déployer en `report-only` puis `enforce`.

Le préflight `doctor` (`Transport HTTP(S)`) émet un WARN si un backend d'auth **fédéré**
(OIDC/proxy/LDAP — identifiants d'entreprise) tourne sans cookie sécurisé ni proxy TLS
déclaré.

---

## 8. Postures qui échouent FERMÉ (0.4.0)

Une passe de sécurité dédiée a inversé plusieurs défauts qui « échouaient ouvert » : le
système continuait de fonctionner en ayant perdu sa protection, sans que rien ne le signale.
Le raisonnement complet, y compris ce qui a été **refusé** et pourquoi, vit dans
[`archive/PASSE_SECURITE_2026-08.md`](archive/PASSE_SECURITE_2026-08.md).

### Service d'inférence — plus de démarrage ouvert

Sa garde de clé API était un **no-op** quand aucune clé n'était configurée. Comme la clé se
lit d'abord dans une variable d'environnement, une variable disparue au déploiement
transformait *en silence* un service authentifié en service ouvert — sur un port qui écoute
en `0.0.0.0:8002`. Désormais :

- `inference.auth.api_key_env` **déclarée mais variable absente** → refus de démarrer, même
  si le mode ouvert est demandé par ailleurs (la configuration se contredirait) ;
- **aucune clé** → mode ouvert seulement s'il est explicitement demandé
  (`inference.auth.allow_unauthenticated`), avec un avertissement bruyant.

Le transport `file_ref` est borné de la même façon : sans `inference.allowed_audio_roots`,
la racine est déduite de `storage.jobs_dir`. Un chemin hors racine répond **403 avant 404** —
répondre « introuvable » ferait du service un oracle d'existence de fichiers.

### Amorçage — plus de secret publié, ni généré

`config.example.yaml` livrait un mot de passe d'amorçage, et le compte était réellement créé
avec (corrigé en S1.4 par un secret généré journalisé une fois — indécouvrable en pratique,
issue #11). Depuis 0.4.4 : sur base vierge sans `auth.first_admin_password` configuré,
**aucun compte n'est créé** — le portail impose la page `/setup` à la première visite
(backend local uniquement, verrouillée dès qu'un compte existe). Compromis assumé, standard
des portails auto-hébergés : entre le premier démarrage et la création du compte, le premier
visiteur crée l'admin ; la fenêtre se ferme d'elle-même, et un déploiement exposé peut la
supprimer en configurant `auth.first_admin_password` avant le premier démarrage.

### Scripts exécutés depuis la configuration

Trois clés désignent un fichier lancé avec `bash` par un service qui tourne en root, et
`/admin/config` propose un mode YAML brut. Trois garde-fous, tous nécessaires :

1. les racines autorisées viennent de la variable d'environnement **`TRANSCRIA_SCRIPT_ROOTS`**
   (unité systemd), **jamais de la configuration** — une allowlist que l'acteur visé règle
   lui-même ne contraint personne ;
2. un exécutable **ne vit pas dans une zone où l'application écrit** (`workflow.prompts_dir`,
   `storage.jobs_dir`, `voice_enrollment.storage_dir`) — sinon il suffit d'y déposer un
   fichier. Un test **recense** ces zones : une clé de configuration nouvelle qui ressemble à
   un répertoire d'accueil fait rougir la suite tant que personne n'a tranché ;
3. le chemin **résolu** est vérifié, et c'est lui qui est exécuté — sans quoi un lien
   symbolique sortant passerait.

### Requêtes sortantes d'un bot

Un bot vise une URL fournie par un **utilisateur**. La garde refuse ce qui n'est jamais une
salle de réunion — boucle locale, adresse « toutes interfaces », lien-local (métadonnées
cloud) — en décidant sur l'**adresse résolue**, jamais sur la forme écrite (`2130706433` et
`127.1` désignent la boucle locale). Les navigations interdites sont **abandonnées avant
émission** (interception de route), redirections comprises.

**Aucune plage privée ou publique n'est bornée**, et c'est délibéré : *l'adresse ne dit pas
si l'on est chez soi*. Une organisation disposant d'un bloc public l'utilise en interne. Le
seul mécanisme qui sache distinguer « mon réseau » d'Internet est l'allowlist
**`BOT_ALLOWED_HOSTS`** — le diagnostic la rappelle quand un connecteur est configuré sans
elle.

### Fichier de configuration

Écriture **atomique** et `0600` à chaque enregistrement — y compris sur un fichier existant
trop permissif. Un contrôle au doctor regarde l'état **réel** sur disque : le code corrigé ne
dit rien des fichiers créés avant lui.

### Sondes et exports

`/health` et `/ready` ne divulguent plus l'URI de connexion à la base (hôte, port,
utilisateur) : le détail est réservé aux **administrateurs**, la sonde anonyme garde son
oui/non. Les exports CSV neutralisent les formules (`=`, `+`, `-`, `@`) — le titre d'un job
est une valeur d'utilisateur, et un tableur l'exécute à l'ouverture.
