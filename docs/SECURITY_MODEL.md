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
  (`SESSION_COOKIE_SECURE`, à mettre à `true` derrière HTTPS).
- **Durée de session explicite** : `PERMANENT_SESSION_LIFETIME` = 12 h par défaut
  (`auth.session_lifetime_hours`) — plus de session « jusqu'à fermeture du
  navigateur » imprévisible.
- **Anti-bourrinage** (`transcria/auth/rate_limit.py`) : 5 échecs par (IP, identifiant)
  en 5 min → blocage 5 min (429), journalisé en audit (`login_failed` avec motif). En
  mono-process (déploiement local) le compteur est global ; en multi-process chaque
  worker a le sien (le blocage reste efficace, une même IP se répartit mal).
- **Échecs de connexion journalisés** (`AuditAction.LOGIN_FAILED`, avec identifiant tenté).
- **CSRF** : pas de jeton dédié — la protection repose sur `SameSite=Lax`, qui bloque
  l'envoi du cookie sur les POST cross-site initiés par un autre site depuis les
  navigateurs modernes. **Limitation assumée** : les très vieux navigateurs sans
  support SameSite ne sont pas couverts ; un jeton CSRF explicite est un candidat 0.3
  si un déploiement l'exige.

## 3. En-têtes de sécurité (C3.9)

Posés sur toutes les réponses (`app.after_request`) :

- `X-Content-Type-Options: nosniff` — pas de devinette de type MIME ;
- `X-Frame-Options: DENY` — anti-clickjacking (l'app ne s'embarque jamais en iframe) ;
- `Referrer-Policy: strict-origin-when-cross-origin` — ne fuite pas les URLs (jetons
  `?next=`) vers l'extérieur.

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
