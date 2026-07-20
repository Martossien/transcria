# Gestion d'identité d'entreprise — OIDC, LDAP/AD, proxy de confiance, jetons d'API

> **Statut : PLAN VALIDÉ, non implémenté.** Document d'analyse et de cadrage
> (2026-07-20), rédigé en réponse à des demandes utilisateurs : « gestion des
> utilisateurs en LDAP, Active Directory, OIDC… pour les droits (admins,
> utilisateurs, etc.) ». Il suit la même discipline que
> `PISTES_AMELIORATION.md` : état des lieux vérifié contre le code, choix
> argumentés, lots avec définition de fini, matrice de tests. Les principes
> d'implémentation du projet (§ « Principes » de `PISTES_AMELIORATION.md`)
> s'appliquent intégralement — en premier lieu : **les comptes locaux restent
> le défaut, rien ne change pour les installations existantes**.

## 0. Résumé exécutif

Le portail ne connaît aujourd'hui que des comptes locaux (mot de passe haché en
base). Pour une adoption en entreprise, c'est le blocage classique : pas
d'offboarding automatique (un employé parti garde son accès tant qu'un admin ne
pense pas à le désactiver), pas de politique de mots de passe ni de MFA
centralisés, double saisie des droits.

La stratégie retenue tient en une phrase : **un socle « fournisseur d'identité
enfichable » et quatre connecteurs**, du plus structurant au moins cher —

1. **OIDC** (Authlib) — couvre d'un coup Entra ID/Azure AD, Keycloak,
   Authentik, Zitadel, Google Workspace, Okta… **et Active Directory on-prem
   par fédération** (l'IdP devant l'annuaire) ;
2. **LDAP/AD direct** (`ldap3`, bind LDAPS + groupes) — pour les structures qui
   refusent tout composant intermédiaire ;
3. **En-têtes de proxy de confiance** (`Remote-User`/`Remote-Groups`) — le
   standard de facto du self-hosted (Authelia, oauth2-proxy, Pomerium),
   quasi gratuit à implémenter ;
4. **Jetons d'API personnels** — pour les intégrateurs du contrat scriptable ⭐
   (aujourd'hui l'API exige un cookie de session : vrai frein).

Kerberos/SPNEGO, SAML 2.0 et SCIM sont **explicitement différés** (§7) : la
fédération par IdP les fournit à qui en a besoin, et l'architecture enfichable
permettra de les ajouter sans refonte si la demande devient réelle.

## 1. État des lieux (vérifié contre le code, 2026-07-20)

Le socle interne est en meilleur état que la demande ne le laissait craindre —
les **droits** sont déjà abstraits, seule l'**authentification** est mono-mode :

| Brique | Où | État |
|---|---|---|
| Rôles | `transcria/auth/models.py:11` — `Role` : `ADMIN`, `MANAGER`, `OPERATOR`, `VIEWER` (+ hiérarchie `:19`) | ✅ prêt : c'est la cible du mapping groupes→rôles |
| Permissions | `transcria/auth/permissions.py:11` — 10 `Permission`, table `_ROLE_PERMISSIONS` (`:24`), décorateur `requires()` (`:61`) posé sur les routes sensibles | ✅ prêt : aucun connecteur n'a à toucher aux permissions |
| Comptes | `transcria/auth/store.py` — `UserStore` complet (create/get/update/deactivate/change_password) ; `User` avec `is_active`, `password_hash` | ✅ base du provisionnement à la volée (JIT) |
| Groupes **locaux** | `transcria/auth/groups.py` — `GroupStore` (16 méthodes), `Group`/`GroupMembership`/`GroupRole` | ⚠ à articuler : les groupes FÉDÉRÉS (claims/memberOf) ne doivent pas entrer en collision avec les groupes locaux (partage de lexiques…) — voir §3.4 |
| Session | `flask_login` (`transcria/auth/routes.py:5`), login local classique (`:63-84`) | ✅ conservé tel quel : tout connecteur aboutit à un `login_user()` standard |
| Secours | `reset-admin-password` (CLI maintenance, 0.3.8) + `first_admin_password` | ✅ devient la procédure break-glass officielle (IdP en panne) |
| Audit | `transcria/audit/` (actions typées, acteur, détails) | ✅ chaque login fédéré/JIT/refus sera audité |
| API | contrat scriptable ⭐ (upload → process → status → download), cookie de session obligatoire | ❌ pas de jeton d'API : à créer (lot 4) |

**Conclusion de l'état des lieux** : le travail est un travail de *connecteurs*,
pas de refonte. L'ossature (principe n°2) n'a pas à bouger : chaque connecteur
authentifie, projette l'identité sur un `User` local, et laisse rôles,
permissions, sessions, audit et ownership des jobs inchangés.

## 2. Choix techniques (argumentés, recherche du 2026-07-20)

| Brique | Choix | Alternatives écartées et pourquoi |
|---|---|---|
| Client OIDC | **Authlib** (BSD, Python ≥ 3.10, intégration Flask native, JWKS/discovery/JWT inclus, maintenance active + support Tidelift) | `flask-oidc` (maintenance faible), assembler `oauthlib`+`joserfc` à la main (réinventer ce qu'Authlib fait bien) |
| LDAP/AD | **`ldap3` en direct** (pure Python, mûr) — bind LDAPS + recherche `memberOf`, ~200 lignes maîtrisées | `flask-ldap3-login` (wrapper à maintenance légère — dépendance dormante pour si peu), `python-ldap` (binaire, compilation) |
| Proxy de confiance | Convention `Remote-User`/`Remote-Groups`/`Remote-Name`/`Remote-Email` **uniquement si l'adresse source est le proxy déclaré** | — (pas de bibliothèque : c'est 1 garde + 1 lecture d'en-têtes) |
| Jetons d'API | `secrets.token_urlsafe` + hachage en base (mêmes motifs que les mots de passe), préfixe identifiable `tia_` | JWT auto-signés (révocation impossible sans liste noire — un hachage en base est révocable trivialement) |
| IdP factice (tests) | **`oidc-provider-mock`** (PyPI) — serveur OIDC en thread, **fixture pytest**, claims injectables par test | mock maison des endpoints (fragile), MockServer (Java) |
| IdP réel (E2E/CI) | **Dex** en conteneur (léger, cas d'usage documenté par Docker) | Keycloak en CI (2-4 Go, lent au boot) |

### L'écosystème IdP côté utilisateurs (à documenter, pas à implémenter)

- **Keycloak** — la fédération LDAP/AD la plus complète de l'open-source (AD,
  FreeIPA, Kerberos) : le choix « DSI » du guide §6 ;
- **Authentik** — IdP + serveur LDAP + forward-auth en un produit, ~1-2 Go :
  le milieu de gamme ;
- **Authelia** — < 30 Mo de RAM, forward-auth derrière Nginx/Traefik : le
  chouchou du self-hosted — c'est LUI que le connecteur « en-têtes » (lot 3)
  sert nativement ;
- **Zitadel** — multi-tenant B2B ; à écarter pour le cas AD (pas de vraie
  interface LDAP) ;
- **Entra ID (Azure AD)** — OIDC natif : couvert par le lot 1 sans travail
  spécifique (beaucoup de demandes « Active Directory » sont en réalité
  celle-ci — à qualifier avec chaque demandeur).

## 3. Architecture cible — socle commun aux quatre connecteurs

### 3.1 Backend d'identité enfichable

Nouveau paquet `transcria/auth/identity/` (l'ossature existante ne bouge pas) :

```
transcria/auth/identity/
  __init__.py      # get_identity_backend(config) -> IdentityBackend (résolution unique)
  base.py          # contrat + FederatedIdentity (dataclass gelée)
  local.py         # comportement historique EXTRAIT tel quel (golden sur la route)
  oidc.py          # lot 1
  ldap.py          # lot 2
  proxy.py         # lot 3
```

Contrat (`base.py`) — volontairement minimal, tout le reste est commun :

```python
@dataclass(frozen=True)
class FederatedIdentity:
    subject: str            # identifiant STABLE chez le fournisseur (sub OIDC,
                            # objectGUID AD, Remote-User) — JAMAIS l'email
    username: str           # preferred_username / sAMAccountName / Remote-User
    display_name: str
    email: str
    groups: tuple[str, ...] # claims `groups`, memberOf (DN complets), Remote-Groups
    source: str             # "oidc" | "ldap" | "proxy"

class IdentityBackend(Protocol):
    #   None  -> identifiants refusés (message générique, jamais « lequel » a échoué)
    #   Lever IdentityUnavailable -> fournisseur injoignable (message + break-glass)
    def authenticate(self, request_ctx) -> FederatedIdentity | None: ...
```

Le dispatch vit dans `auth/routes.py` (`login()`, aujourd'hui `:63-84`) : le
POST local reste inchangé ; `GET /login` affiche le bouton SSO si
`auth.backend != local`. Les nouvelles routes (lot 1) : `GET /auth/oidc/login`
(redirection autorize + state/nonce en session serveur), `GET /auth/oidc/callback`
(échange code→jetons, validation, JIT, `login_user()`), `GET /auth/oidc/logout`.
Le `LoginRateLimiter` existant (`transcria/auth/rate_limit.py`, C3.3) s'applique
au callback et au bind LDAP comme au formulaire local — même budget, même bannissement.

### 3.2 Modèle de données (migration Alembic additive, lot 0)

`users` (`transcria/auth/models.py:27`) — colonnes AJOUTÉES, nullable, aucun
impact sur l'existant :

| Colonne | Type | Rôle |
|---|---|---|
| `identity_source` | `String(16)`, défaut `"local"`, indexée | provenance du compte ; les chemins mot-de-passe (change_password, reset CLI) REFUSENT si ≠ local |
| `external_subject` | `String(255)`, nullable, **unique par (source, subject)** (index composite) | clé de rapprochement JIT — jamais le username, jamais l'email |
| `last_identity_sync` | `DateTime(tz)`, nullable | dernière resynchronisation des attributs |

`api_tokens` (lot 4, nouvelle table) :

```
id           String(36) PK
user_id      FK users.id, indexé, CASCADE delete
token_id     String(16) unique indexé   -- partie publique du jeton (lookup O(1))
secret_hash  String(64)                 -- sha256 du secret (comparaison hmac.compare_digest)
label        String(80)                 -- nom donné par l'utilisateur
created_at / expires_at (nullable) / last_used_at (mise à jour throttlée à 1/min)
revoked_at   DateTime(tz) nullable      -- révocation = soft (trace d'audit conservée)
```

Format du jeton : `tia_<token_id>_<secret 32 octets urlsafe>` — le préfixe
`tia_` permet aux scanners de secrets (GitHub push protection, gitleaks) d'être
configurés, `token_id` évite le scan complet de table au lookup.

### 3.3 Flux OIDC détaillé (lot 1)

Authorization Code **+ PKCE (S256)** — même en client confidentiel, coût nul et
protège le canal :

1. `GET /auth/oidc/login` → génère `state` (32 o), `nonce` (32 o),
   `code_verifier` ; stockés en **session serveur Flask** (cookie signé
   existant : `HTTPONLY`/`SameSite=Lax` déjà posés — `app_services.py:174-175`) ;
   redirection vers `authorization_endpoint` (résolu par la découverte
   `{issuer}/.well-known/openid-configuration`, cachée en mémoire, TTL 1 h).
2. Callback : vérifier `state` (comparaison constante, usage unique — supprimé
   de session immédiatement) ; échange code→jetons (Authlib gère PKCE + auth
   client) ; **validation de l'ID token** : signature via JWKS (cache Authlib,
   re-fetch sur `kid` inconnu — c'est la rotation de clés), `iss` exact,
   `aud == client_id`, `exp/iat` avec `leeway` configurable (défaut 30 s),
   `nonce` égal à celui de la session.
3. Claims → `FederatedIdentity` : `sub` obligatoire ; `groups` lu depuis le
   claim configuré (`auth.oidc.role_mapping.claim`), en tolérant le format
   Entra ID (IDs de groupes) comme Keycloak (noms) — la comparaison du mapping
   est une égalité de chaînes, l'admin met ce que son IdP émet.
4. JIT (§3.5) → `login_user()` → redirection vers la cible initiale
   (sauvegardée AVANT la redirection IdP, avec validation same-origin stricte —
   jamais d'open redirect).
5. **Pas de refresh token en v1** : pas de scope `offline_access`, aucun jeton
   IdP persisté — la session applicative Flask (durée
   `PERMANENT_SESSION_LIFETIME`, `app_services.py:161`) est la seule vérité
   après login. C'est le choix qui évite le stockage chiffré de jetons et la
   moitié de la surface d'attaque.
6. Logout : `logout_user()` local puis, si `end_session_endpoint` découvert,
   redirection RP-initiated logout (`id_token_hint` non conservé → on passe
   `client_id` + `post_logout_redirect_uri` déclarée).

### 3.4 Flux LDAP/AD détaillé (lot 2)

Deux modes (configurables) :

- **bind direct** (simple) : `bind(userDN construit via template, password)` ;
- **service + recherche** (recommandé AD) : bind du compte de service, recherche
  `(&(objectClass=user)(sAMAccountName={username}))` sous `base_dn`, puis
  re-bind avec le DN trouvé et le mot de passe utilisateur.

Détails d'implémentation qui font la différence en production AD :

- `ldap3.ServerPool` (plusieurs contrôleurs de domaine, stratégie FIRST avec
  reprise), timeouts connect/receive explicites (défauts 5 s / 10 s) ;
- **LDAPS obligatoire** par défaut (`use_ssl=true`, CA configurable
  `tls_ca_file`) ; `allow_plaintext: true` exigé explicitement sinon refus au
  boot (validation de schéma) ;
- échappement systématique de l'entrée utilisateur dans les filtres :
  `ldap3.utils.conv.escape_filter_chars` (injection LDAP) ;
- groupes : lecture `memberOf` du compte ; option
  `resolve_nested_groups: false` — si true, recherche
  `(member:1.2.840.113556.1.4.1941:={userDN})` (OID
  `LDAP_MATCHING_RULE_IN_CHAIN`, récursif côté serveur AD ; coûteux, documenté) ;
- codes de résultat AD distingués dans les messages ET l'audit : 49/data 52e
  (mauvais mot de passe), 533 (compte désactivé), 701 (expiré), 775
  (verrouillé) — l'utilisateur voit un message générique, l'ADMIN voit la
  cause dans l'audit ;
- referrals désactivés (`auto_referrals=False`) — source classique de
  suspensions mystérieuses contre AD multi-domaines.

### 3.5 Provisionnement JIT — algorithme exact (commun aux 3 connecteurs)

```
identité = backend.authenticate(...)
u = UserStore.get_by_external(identité.source, identité.subject)   # nouvelle méthode
si u existe :
    resynchroniser display_name/email ; recalculer le rôle via role_mapping
    (le rôle PEUT baisser — la vérité vient de l'IdP à chaque login) ;
    si is_active == False → REFUS (un compte désactivé localement le reste,
    même si l'IdP le connaît encore : la désactivation locale est un veto).
sinon :
    rôle = role_mapping(identité.groups) ; si deny → REFUS audité (groupe manquant)
    username = identité.username ; si collision avec un compte EXISTANT
    (local ou autre source) → suffixe « @<source> » (jamais d'écrasement,
    jamais de fusion silencieuse — test dédié) ;
    créer User(identity_source=source, external_subject=subject,
               password_hash=UNUSABLE, role=rôle)
audit(LOGIN_FEDERATED, acteur, source, groupes_décisifs)
login_user(u)
```

`UNUSABLE` = valeur sentinelle non vérifiable (`"!"` préfixé) : `check_password`
retourne False par construction ; `change_password`/`reset-admin-password`
refusent avec message explicite si `identity_source != local`.

### 3.6 Mapping groupes → rôles (commun) — sémantique précise

```yaml
auth:
  role_mapping:
    claim: groups            # OIDC ; ignoré en LDAP (memberOf) et proxy (Remote-Groups)
    rules:                   # ORDONNÉ, premier match gagne, égalité stricte de chaîne
      - group: "transcria-admins"
        role: admin
      - group: "CN=Transcria Users,OU=Apps,DC=corp,DC=example"
        role: operator
    default: deny            # deny | viewer — validé par le schéma, rien d'autre
```

Invariants (tests dédiés) : un login sans AUCUNE règle applicable suit
`default` ; `default: deny` → 403 avec message i18n « accès non attribué,
contactez votre administrateur » + audit du refus AVEC la liste des groupes
reçus (c'est l'outil de diagnostic n°1 de l'admin) ; le rôle est REMPLACÉ à
chaque login (jamais max(ancien, nouveau)) ; aucune règle ne peut cibler un
rôle inexistant (validation de schéma contre `Role`).

### 3.7 En-têtes de proxy de confiance (lot 3) — règles exactes

```yaml
auth:
  proxy:
    trusted_ips: ["127.0.0.1", "10.0.0.5"]   # OBLIGATOIRE, vide = backend refusé au boot
    user_header: "Remote-User"
    groups_header: "Remote-Groups"            # séparateur virgule
    name_header: "Remote-Name"
    email_header: "Remote-Email"
    auto_login: true                          # false = bouton « continuer en SSO »
```

Gardes non négociables : la requête dont `remote_addr` ∉ `trusted_ips` qui
PORTE ces en-têtes est journalisée en WARNING (tentative d'usurpation) et les
en-têtes ignorés ; le guide de déploiement montre la directive proxy qui
**écrase** les en-têtes entrants (`proxy_set_header Remote-User …` côté Nginx —
jamais de passthrough) ; `ProxyFix` n'est PAS utilisé pour résoudre
`remote_addr` de cette garde (on compare l'adresse socket réelle, pas
`X-Forwarded-For`, falsifiable par construction).

### 3.8 Jetons d'API (lot 4) — chemin d'authentification

Nouveau décorateur d'entrée dans `web/request_helpers.py` : si
`Authorization: Bearer tia_…` présent → lookup `token_id`, comparaison
`hmac.compare_digest(sha256(secret), secret_hash)`, contrôles
révocation/expiration/`user.is_active`, puis injection du user dans le contexte
`flask_login` (login_user(user, remember=False) par requête, sans cookie —
`session.permanent = False` et session non émise). Périmètre v1 : les routes ⭐
UNIQUEMENT (liste explicite, pas une regex) ; le jeton porte les permissions de
son propriétaire (un viewer ne POST pas /process — test dédié). `last_used_at`
mis à jour au plus 1×/min (éviter une écriture DB par requête de polling).

### 3.9 Break-glass (IdP en panne) — procédure vérifiable

`auth.backend: oidc|ldap|proxy` n'éteint PAS le formulaire local : il reste
servi sur `GET /login?local=1` (non lié depuis la page SSO), accepte les seuls
comptes `identity_source == "local"`. Doctor (lot 1) : FAIL si un backend
fédéré est actif et qu'aucun admin local actif n'existe ; WARN si le mot de
passe du premier admin est resté à `first_admin_password`. Le scénario complet
(IdP éteint → login local → reset CLI) fait partie de la matrice §5 et se
rejoue en session HTTP réelle avant chaque release touchant l'auth.

## 4. Plan de lots

Chaque lot suit le rituel du projet : clés en config validées par le schéma et
classées, docs (CONFIG_REFERENCE + INSTALL + UPGRADE), i18n FR/EN (page de
login incluse), check doctor, tests unitaires + intégration + **UI en session
réelle**, suite + E2E verts avant push, lecture des parcours par un humain.

### Lot 0 — Socle enfichable (prérequis, S/M)

Extraction du chemin local vers `identity/local.py` **à comportement
octet-pour-octet identique** (goldens sur la route login) ; contrat
`IdentityBackend` ; `auth.backend: local` par défaut ; dispatch dans la route.
DoD : suite verte sans modification d'aucun test existant.

### Lot 1 — OIDC (M/L, le cœur)

Authlib (Authorization Code + PKCE ; découverte par `issuer`) ; JIT §3.2 ;
mapping §3.3 ; logout (local + `end_session_endpoint` si exposé) ; bouton SSO
i18n sur la page de login (libellé configurable) ; doctor : discovery
joignable, `client_id` présent, horloge (skew JWT), au moins un admin local.
Config : `auth.oidc.{issuer, client_id, client_secret(env), scopes,
role_mapping…}`. Tests : `oidc-provider-mock` en fixture (nominal, claims sans
groupe → `default`, IdP down → message clair + break-glass, état/nonce rejoués,
jeton expiré). E2E-intégration : Dex en conteneur. **Validation manuelle contre
un Keycloak réel** avant de déclarer le lot fini.

### Lot 2 — LDAP / Active Directory direct (M)

`ldap3` : bind de service (ou bind direct utilisateur, configurable), LDAPS
obligatoire par défaut (`allow_plaintext: false`), base DN + filtre de
recherche, `memberOf` → mapping §3.3 (mêmes règles, mêmes tests), JIT idem.
Attention AD documentées : groupes imbriqués (option `LDAP_MATCHING_RULE_IN_CHAIN`,
coût), comptes verrouillés/expirés (codes de résultat AD distincts → messages
distincts), referrals désactivés par défaut. Tests : serveur `ldap3` en mode
mock (le paquet fournit `MockSyncStrategy`) + validation manuelle contre un
Samba AD en conteneur.

### Lot 3 — En-têtes de proxy de confiance (S)

`auth.proxy.{trusted_ips, user_header, groups_header, auto_login}` ; refus
absolu si l'appelant n'est pas dans `trusted_ips` (et journalisation du
contraire) ; mapping §3.3 sur `Remote-Groups`. Guide Authelia + oauth2-proxy.
C'est le lot au meilleur ratio adoption/effort — livrable avec le lot 1.

### Lot 4 — Jetons d'API personnels (S/M)

Page « Mon compte » : créer/révoquer des jetons (`tia_` + secret affiché une
seule fois, hachage en base, expiration optionnelle, dernier usage affiché) ;
en-tête `Authorization: Bearer tia_…` accepté sur les routes ⭐ (et elles
seules, v1) ; le jeton porte les permissions de son propriétaire, jamais plus ;
audit des usages. Régénérer `docs/API_REFERENCE.md` (garde C8).

### Points d'ancrage dans le code (par lot)

| Lot | Fichiers touchés (créés ➕ / modifiés ✏) |
|---|---|
| 0 | ➕ `auth/identity/{__init__,base,local}.py` ; ✏ `auth/routes.py:63` (dispatch), `config/loader.py` (défauts `auth.backend`), `config_schema.py` (+`_check_auth_backend`), `data/config_classification.yaml` |
| 1 | ➕ `auth/identity/oidc.py` ; ✏ `auth/routes.py` (3 routes), `templates/login.html` (bouton SSO i18n), `diagnostics/doctor.py` (+`check_oidc_provider`, `check_local_admin_exists`), `web/translations` FR/EN, `requirements` (+authlib) |
| 2 | ➕ `auth/identity/ldap.py` ; ✏ doctor (+`check_ldap_reachable`), requirements (+ldap3) |
| 3 | ➕ `auth/identity/proxy.py` (~150 lignes gardes comprises) ; ✏ doctor (WARN si trusted_ips contient 0.0.0.0/0) |
| 4 | ➕ `auth/api_tokens.py` + migration `api_tokens` + page « Mon compte » ; ✏ `web/request_helpers.py` (Bearer), routes ⭐ (aucun changement de code — le décorateur d'entrée est commun), régénération `docs/API_REFERENCE.md` (garde C8) |

Estimations (avec la discipline maison — tests/i18n/doctor/docs compris) :
lot 0 ≈ 1 j ; lot 1 ≈ 3-4 j ; lot 2 ≈ 2-3 j ; lot 3 ≈ 1 j ; lot 4 ≈ 2 j.

### Modes de panne et comportements (contrat utilisateur)

| Panne | Vu par l'utilisateur | Vu par l'admin (audit/logs) | Comportement |
|---|---|---|---|
| IdP OIDC injoignable | « Fournisseur d'identité indisponible » + lien break-glass | WARNING avec cause réseau | aucun retry automatique (c'est un humain devant un écran) |
| JWKS : `kid` inconnu | transparent (re-fetch) ou échec propre | INFO rotation / WARNING échec | 1 re-fetch, pas de boucle |
| ID token invalide (iss/aud/exp/nonce) | « Connexion refusée » générique | WARNING détaillé (quel contrôle) | refus sec, jamais de fallback local implicite |
| Groupes absents du claim | selon `default` | refus audité AVEC groupes reçus | l'admin diagnostique en 1 lecture |
| LDAP : DC injoignable | « Annuaire indisponible » + break-glass | WARNING par serveur du pool | bascule ServerPool puis échec |
| AD : compte verrouillé/expiré | message générique | code AD précis dans l'audit | jamais de détail côté formulaire (énumération) |
| Proxy : en-tête depuis IP non déclarée | 401 | WARNING « usurpation possible » + IP | en-têtes ignorés |
| Jeton API révoqué/expiré | 401 JSON | audit usage refusé | pas de distinction révoqué/expiré côté client |

### Ordre conseillé

Lot 0 → Lot 1 + Lot 3 (livrés ensemble : une release « SSO ») → Lot 4 →
Lot 2 (à la première demande AD-direct qualifiée — voir §2, beaucoup de
demandes « AD » sont Entra ID, couvertes dès le lot 1).

## 5. Matrice de tests (sécurité : la barre est au maximum)

| Scénario | Lot | Outil |
|---|---|---|
| Login OIDC nominal, JIT création puis resynchronisation | 1 | oidc-provider-mock |
| Claims sans groupe mappé → `default: deny` (401) puis `viewer` | 1 | oidc-provider-mock |
| IdP injoignable → message clair, break-glass accessible | 1 | fixture éteinte |
| `state`/`nonce` rejoués, jeton expiré, `iss`/`aud` faux → refus | 1 | oidc-provider-mock |
| Un compte local existant N'EST PAS écrasé par un JIT homonyme | 1 | unitaire |
| Bind LDAPS ok / mauvais mot de passe / compte verrouillé AD → messages distincts | 2 | ldap3 mock |
| Groupes imbriqués AD (option activée/désactivée) | 2 | ldap3 mock |
| En-tête depuis IP non déclarée → 401 + audit | 3 | client de test |
| Jeton révoqué/expiré → 401 ; jeton d'un viewer ne peut pas POST /process | 4 | client de test |
| Break-glass : backend fédéré actif, IdP down, admin local entre par /login?local=1 | 1-3 | session réelle |
| Doctor : discovery KO, zéro admin local actif, LDAPS sans TLS → FAIL/WARN explicites | 1-3 | unitaire doctor |
| Parcours UI réels FR et EN (bouton SSO, messages d'erreur, page jetons) | tous | session HTTP réelle |

## 6. Documentation à livrer avec les lots

- `INSTALL.md` : section « Identité d'entreprise » avec **trois guides
  pas-à-pas** : Keycloak devant votre Active Directory (~15 min), Authelia
  devant le portail (lot 3), Entra ID (enregistrement d'application) ;
- `CONFIG_REFERENCE.md` : sections `auth.*` complètes, défauts, redémarrage ;
- `UPGRADE.md` : « rien à faire » (défaut local inchangé) + procédure
  break-glass ;
- README EN/FR : la ligne « SSO entreprise (OIDC, LDAP/AD, proxy) » dans les
  features — c'est un argument d'adoption majeur, il doit se voir.

## 7. Différé explicitement (et pourquoi)

| Sujet | Raison du report | Condition de réouverture |
|---|---|---|
| Kerberos/SPNEGO | complexe à déboguer, servi par la fédération Keycloak | demande ferme d'une DSI sans IdP possible |
| SAML 2.0 | ancien monde ; les IdP font passerelle SAML→OIDC | exigence contractuelle explicite |
| SCIM (dé/provisionnement poussé) | le JIT + refus IdP couvre l'essentiel | besoin réel de désactivation active de sessions |
| Sync groupes fédérés → groupes locaux | collisions de nommage, suppressions surprises | après retours d'usage des lots 1-3 |
| MFA/TOTP local | dès qu'il y a un IdP, c'est lui qui porte le MFA | demande des installations 100 % locales |

## 8. Risques et garde-fous

- **Sécurité du mapping** : défaut `deny`, jamais d'élévation implicite, audit
  de chaque attribution — revue dédiée de ce module avant merge.
- **Lockout** : le doctor refuse d'activer un backend fédéré sans admin local
  actif ; break-glass documenté et testé en session réelle.
- **Secrets** : `client_secret`/bind password via variable d'environnement ou
  fichier à droits restreints, jamais en clair dans les logs ni l'audit ;
  masqués dans le formulaire admin (mécanique `SECRET_SENTINEL` existante).
- **Compatibilité** : `auth.backend: local` par défaut — aucune installation
  existante ne change de comportement, aucune migration de base pour les lots
  0-3 (lot 4 : une table `api_tokens`, migration Alembic additive).

---

*Sources de la recherche (2026-07-20) : github.com/authlib/authlib ·
oauth.net/code/python · github.com/nickw444/flask-ldap3-login ·
pypi.org/project/oidc-provider-mock · docs.docker.com/guides/dex ·
comparatifs IdP self-hosted 2026 (elest.io, authhost.de, cerbos.dev).*
