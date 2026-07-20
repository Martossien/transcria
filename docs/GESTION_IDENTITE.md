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

```yaml
auth:
  backend: local          # défaut INCHANGÉ ; local | oidc | ldap | proxy
  # Chaque connecteur a sa section, inerte tant que backend ne le désigne pas.
```

Un module `transcria/auth/identity/` (nouveau, ossature intacte ailleurs) :
`base.py` (contrat `IdentityBackend.authenticate(...) -> FederatedIdentity |
None`), `local.py` (comportement historique, extrait sans le modifier),
`oidc.py`, `ldap.py`, `proxy.py`. La route `login` dispatche selon
`auth.backend` ; **tous les chemins aboutissent au même `login_user()`**.

### 3.2 Provisionnement à la volée (JIT)

Au premier login fédéré : création d'un `User` local « projection »
(`username` = claim stable — `preferred_username`/`sub` préfixé, jamais
l'email seul ; `password_hash` inutilisable ; marqueur `identity_source`).
Aux logins suivants : resynchronisation du nom/email/rôle. L'ownership des
jobs, les groupes locaux, l'audit continuent de fonctionner sans une ligne
changée — c'est le cœur du choix « projection » plutôt que « session sans
compte ».

**Désactivation** : un utilisateur retiré de l'IdP ne peut plus se connecter
(c'est l'IdP qui refuse). Pour la désactivation *active* (sessions en cours,
visibilité admin), le lot 5 (SCIM) est la vraie réponse — en attendant, durée
de session bornée (`auth.session_max_age`) et page admin listant les comptes
fédérés avec leur dernière connexion.

### 3.3 Mapping groupes → rôles (commun OIDC/LDAP/proxy)

```yaml
auth:
  role_mapping:
    claim: groups                    # OIDC : nom du claim ; LDAP : memberOf ; proxy : Remote-Groups
    rules:                           # premier match gagne, comparaison exacte
      - group: "CN=transcria-admins,OU=…"   # ou nom court côté OIDC/proxy
        role: admin
      - group: "transcria-users"
        role: operator
    default: deny                    # deny | viewer — JAMAIS operator/admin par défaut
```

Règles de sécurité : défaut restrictif (`deny`), l'admin DOIT mapper
explicitement ; un compte fédéré ne peut JAMAIS obtenir un rôle supérieur à son
mapping (pas d'élévation locale persistante) ; chaque attribution/refus est
audité avec le groupe déclencheur.

### 3.4 Groupes locaux vs groupes fédérés

Les `Group` locaux (partage de lexiques, `groups.py`) restent des objets
LOCAUX : le mapping fédéré ne produit que des **rôles**, pas des adhésions aux
groupes locaux (v1). Une synchronisation groupes-fédérés → groupes-locaux est
notée comme extension (§7) — la faire d'emblée créerait des collisions de
nommage et des suppressions surprises.

### 3.5 Break-glass (IdP en panne)

Le compte admin LOCAL survit à tous les backends : `auth.backend: oidc`
n'éteint pas le formulaire local pour les comptes `identity_source=local` de
rôle admin (route `/login?local=1`, non proposée par défaut sur la page).
Procédure documentée : `reset-admin-password` (CLI 0.3.8) + ce chemin. Le
doctor vérifie qu'au moins un admin local actif existe quand un backend fédéré
est activé.

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
