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

**CSP (Content-Security-Policy)** — **limitation assumée** : non posée en 0.2.0. Les
templates utilisent des gestionnaires d'événements inline (`onclick=`) et un bundle
Bootstrap servi par CDN ; une CSP stricte sans *nonce* casserait l'interface. Plan
0.3 : soit inliner le bundle et ajouter des nonces par requête, soit migrer les
handlers vers des écouteurs délégués, puis poser une CSP restrictive.

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

Le préflight `doctor` (`Transport HTTP(S)`) émet un WARN si un backend d'auth **fédéré**
(OIDC/proxy/LDAP — identifiants d'entreprise) tourne sans cookie sécurisé ni proxy TLS
déclaré.
