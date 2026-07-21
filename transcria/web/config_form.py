"""Éditeur de configuration à formulaires : spécification déclarative + helpers purs.

L'UI `/admin/config` propose des formulaires lisibles pour les réglages courants
(ceux que le README liste « à vérifier après installation ») ; le YAML brut reste
disponible en onglet avancé. Cette surcouche est volontairement fine : un formulaire
ne soumet qu'un **dict partiel** des champs gérés, fusionné dans la config complète
via `_deep_merge` (donc aucune autre clé n'est perdue), puis validé par
`ConfigService.save_if_valid` — toute la logique de validation/sauvegarde existe déjà.

Tout est pur ici (aucune I/O) → testable.
"""
from __future__ import annotations

import copy

from flask_babel import lazy_gettext as _l

# Marqueur d'un secret masqué côté UI (aligné sur web.admin_routes.CONFIG_SECRET_SENTINEL).
SECRET_SENTINEL = "********"

# Spécification déclarative des sections et champs. `path` = chemin pointé dans
# config.yaml (vérifié présent dans les defaults par un test anti-dérive).
# `type ∈ {text, int, bool, csv, select, password}`.
# Les libellés/aides d'AFFICHAGE (`title`/`label`/`help`) sont marqués via `lazy_gettext`
# (résolus dans la locale de l'interface au rendu) ; la LOGIQUE n'utilise que
# `path`/`type`/`options`/`secret`, jamais ces chaînes — le marquage est donc sans effet
# de bord. `lazy_gettext` est extrait par pybabel (clé ajoutée dans scripts/i18n_check.py).
CONFIG_FORM_SECTIONS: list[dict] = [
    {
        "title": _l("Modèles & backends"),
        "help": _l("Choix des moteurs de transcription et de diarisation."),
        "fields": [
            {"path": "models.stt_backend", "label": _l("Backend STT"), "type": "select",
             "options": ["cohere", "whisper", "granite", "parakeet", "voxtral", "kroko", "moss", "remote"],
             "help": _l("Moteur de transcription par défaut (cohere recommandé).")},
            {"path": "models.summary_stt_backend", "label": _l("Backend STT du résumé"), "type": "select",
             "nullable": True,
             "options": ["", "cohere", "whisper", "granite", "parakeet", "voxtral", "kroko", "moss",
                         "qwen3asr", "nemotron"],
             "help": _l("Moteur dédié à la transcription rapide de la phase résumé — vide = même moteur "
                        "que le pipeline. kroko = CPU pur (zéro VRAM) ; qwen3asr/nemotron exigent le "
                        "runtime servi audio.cpp/parakeet.cpp (cf. docs/EXTERNAL_STT_RUNTIMES.md).")},
            {"path": "models.diarization_backend", "label": _l("Backend diarisation"), "type": "select",
             "options": ["pyannote", "sortformer", "remote"],
             "help": _l("Détection des locuteurs (pyannote recommandé).")},
        ],
    },
    {
        "title": _l("LLM d'arbitrage"),
        "help": _l("Résumé et correction par la LLM locale OpenAI-compatible."),
        "fields": [
            {"path": "workflow.arbitration_llm.enabled", "label": _l("Activer la correction LLM"), "type": "bool",
             "help": _l("Désactiver pour produire un SRT brut sans correction.")},
            {"path": "workflow.summary_llm.model_id", "label": _l("Modèle de résumé"), "type": "text",
             "help": _l("Identifiant opencode, ex. local/arbitrage.")},
            {"path": "workflow.arbitration_llm.model_id", "label": _l("Modèle de correction"), "type": "text",
             "help": _l("Identifiant opencode du modèle de correction du SRT.")},
            {"path": "services.arbitrage_llm_port", "label": _l("Port du serveur LLM"), "type": "int",
             "help": _l("Port du backend OpenAI-compatible (llama-server par défaut : 8080).")},
            {"path": "services.arbitrage_api_model_id", "label": _l("Alias modèle rapporté par le serveur"), "type": "text",
             "help": _l("Doit correspondre à l'alias servi (cf. scripts/check_arbitrage_llm.sh).")},
        ],
    },
    {
        "title": _l("File & exécution"),
        "help": _l("File GPU persistante et parallélisme des jobs."),
        "fields": [
            {"path": "workflow.queue.enabled", "label": _l("File persistante activée"), "type": "bool",
             "help": _l("Mise en file des traitements (recommandé).")},
            {"path": "workflow.execution.max_concurrent_jobs", "label": _l("Jobs simultanés max"), "type": "int",
             "help": _l("Concurrence par défaut (1 = comportement historique).")},
            {"path": "storage.shared_backend", "label": _l("Stockage des fichiers de jobs"), "type": "select",
             "options": ["fs", "pg"],
             "help": _l("fs (défaut) : disque local — suffisant en tout-en-un ou avec un jobs_dir "
                        "partagé (NFS). pg : fichiers répliqués via PostgreSQL — REQUIS quand la "
                        "frontale (role=web) et le worker (role=scheduler) sont sur deux machines "
                        "sans filesystem commun (cf. docs/STOCKAGE_PARTAGE_JOBS.md).")},
        ],
    },
    {
        "title": _l("Ressources GPU"),
        "help": _l("Récupération de la VRAM quand un job est bloqué faute de mémoire GPU."),
        "fields": [
            {"path": "gpu.preemption", "label": _l("Politique de préemption VRAM"), "type": "select",
             "options": ["own-only", "aggressive"],
             "help": _l("own-only (recommandé) : n'arrête que NOS process gérés inactifs "
                        "(LLM d'arbitrage), jamais un process tiers. aggressive : préempte "
                        "aussi les serveurs d'inférence tiers, uniquement dans la fenêtre "
                        "calendaire « force_gpu » — à réserver à un GPU dédié à TranscrIA.")},
            {"path": "gpu.min_free_vram_mb", "label": _l("VRAM libre minimale (Mo)"), "type": "int",
             "help": _l("Marge libre exigée en plus du besoin d'une phase avant de l'allouer.")},
        ],
    },
    {
        "title": _l("Sécurité & upload"),
        "help": _l("Limites d'upload et suppression des jobs."),
        "fields": [
            {"path": "security.max_upload_size_mb", "label": _l("Taille max d'upload (Mo)"), "type": "int",
             "help": _l("Taille maximale d'un fichier déposé.")},
            {"path": "security.allowed_upload_extensions", "label": _l("Extensions autorisées"), "type": "csv",
             "help": _l("Liste séparée par des virgules, ex. mp3, wav, mp4, m4a.")},
            {"path": "security.allow_job_delete", "label": _l("Autoriser la suppression de jobs"), "type": "bool",
             "help": _l("Permet aux admins de supprimer un job et ses fichiers.")},
        ],
    },
    {
        "title": _l("Durcissement HTTP(S)"),
        "help": _l("Sécurité du transport. Tout est opt-in (défaut = HTTP, dev/tout-en-un). "
                   "Le proxy TLS et le cookie sécurisé exigent un redémarrage du service."),
        "fields": [
            {"path": "security.behind_tls_proxy", "label": _l("Derrière un proxy TLS (HTTPS)"), "type": "bool",
             "help": _l("À activer si un reverse-proxy (nginx, Caddy…) termine le HTTPS devant "
                        "TranscrIA : l'app reconnaît alors HTTPS (via X-Forwarded-Proto), marque le "
                        "cookie de session Secure et peut émettre le HSTS. L'adresse cliente n'est "
                        "JAMAIS lue depuis X-Forwarded-For (anti-usurpation). Redémarrage requis.")},
            {"path": "security.session_cookie_secure", "label": _l("Cookie de session « Secure »"), "type": "bool",
             "help": _l("Marque le cookie de session Secure (envoyé seulement en HTTPS). Implicite si "
                        "« Derrière un proxy TLS » est activé. Redémarrage requis.")},
            {"path": "security.hsts_enabled", "label": _l("HSTS (forcer HTTPS côté navigateur)"), "type": "bool",
             "help": _l("Émet l'en-tête Strict-Transport-Security, uniquement sur une réponse servie en "
                        "HTTPS réel (nécessite « Derrière un proxy TLS »). Anti-rétrogradation HTTP.")},
            {"path": "security.csrf_origin_check", "label": _l("Contrôle d'origine (anti-CSRF renforcé)"),
             "type": "bool",
             "help": _l("Refuse un envoi de formulaire provenant d'un autre site (en-tête Origin "
                        "croisé), en complément de SameSite. N'affecte ni l'API par jeton, ni les "
                        "requêtes sans en-tête Origin.")},
            {"path": "security.csrf_tokens", "label": _l("Jetons CSRF (protection forte)"), "type": "bool",
             "help": _l("Exige un jeton anti-CSRF à chaque requête mutante authentifiée par cookie "
                        "(injecté automatiquement dans les formulaires et les appels de l'interface). "
                        "Défense la plus forte ; l'API par jeton Bearer (scripts) reste exemptée.")},
            {"path": "security.csp", "label": _l("Content-Security-Policy (CSP)"), "type": "select",
             "options": ["off", "report-only", "enforce"],
             "help": _l("Politique de sécurité du contenu. off : désactivée. report-only : le "
                        "navigateur SIGNALE les violations sans rien bloquer (à déployer d'abord, "
                        "pour observer). enforce : appliquée. Verrouille cadres, objets, base, "
                        "formulaires et origines des ressources.")},
        ],
    },
    {
        "title": _l("Notifications email"),
        "help": _l("Email de fin de traitement (SMTP). Requiert une adresse dans le profil utilisateur."),
        "fields": [
            {"path": "notifications.email.enabled", "label": _l("Activer les emails"), "type": "bool",
             "help": _l("Envoie un email au propriétaire à la fin (succès/échec).")},
            {"path": "notifications.email.smtp_host", "label": _l("Serveur SMTP"), "type": "text"},
            {"path": "notifications.email.smtp_port", "label": _l("Port SMTP"), "type": "int",
             "help": _l("Ex. 587 (STARTTLS) ou 465 (SSL).")},
            {"path": "notifications.email.use_starttls", "label": _l("STARTTLS"), "type": "bool"},
            {"path": "notifications.email.use_ssl", "label": _l("SSL/SMTPS"), "type": "bool"},
            {"path": "notifications.email.from_address", "label": _l("Adresse expéditeur"), "type": "text"},
            {"path": "notifications.email.base_url", "label": _l("URL de base du portail"), "type": "text",
             "help": _l("Utilisée dans les liens des emails, ex. http://localhost:7870.")},
        ],
    },
    {
        "title": _l("Voix enregistrées"),
        "help": _l("Référentiel de voix connues (consentement requis)."),
        "fields": [
            {"path": "voice_enrollment.enabled", "label": _l("Activer les voix enregistrées"), "type": "bool"},
            {"path": "voice_enrollment.storage_dir", "label": _l("Répertoire de stockage"), "type": "text",
             "help": _l("Stockage local protégé des voix et consentements.")},
        ],
    },
    {
        "title": _l("Identité d'entreprise (SSO)"),
        "help": _l("Connexion via un fournisseur OIDC (Keycloak, Entra ID, Authentik…). "
                   "Redémarrage du service requis après changement. Compte local de "
                   "secours : /login?local=1 (cf. docs/GESTION_IDENTITE.md)."),
        "fields": [
            {"path": "auth.backend", "label": _l("Backend d'identité"), "type": "select",
             "options": ["local", "oidc", "proxy", "ldap"],
             "help": _l("local (défaut) : comptes de ce portail. oidc : SSO d'entreprise. "
                        "proxy : identité fournie par un proxy d'authentification frontal "
                        "(Authelia, oauth2-proxy…). ldap : annuaire LDAP / Active Directory "
                        "en direct. Renseignez les champs correspondants avant d'activer.")},
            {"path": "auth.oidc.issuer", "label": _l("Émetteur (issuer)"), "type": "text",
             "help": _l("URL de l'émetteur OIDC, ex. https://sso.exemple.fr/realms/entreprise "
                        "(la découverte /.well-known est automatique).")},
            {"path": "auth.oidc.client_id", "label": _l("Identifiant client"), "type": "text"},
            {"path": "auth.oidc.client_secret", "label": _l("Secret client"), "type": "password",
             "secret": True,
             "help": _l("Laissez vide si le secret est fourni par variable d'environnement.")},
            {"path": "auth.oidc.client_secret_env", "label": _l("Variable d'environnement du secret"), "type": "text",
             "help": _l("Nom d'une variable d'environnement contenant le secret — prioritaire "
                        "sur le champ précédent, évite le secret en clair dans config.yaml.")},
            {"path": "auth.oidc.button_label", "label": _l("Libellé du bouton SSO"), "type": "text",
             "help": _l("Texte du bouton sur la page de connexion, ex. « Connexion Entreprise ». "
                        "Vide = libellé par défaut.")},
            {"path": "auth.proxy.trusted_ips", "label": _l("Adresses de confiance du proxy"), "type": "csv",
             "help": _l("Backend proxy uniquement : adresses IP ou réseaux CIDR du proxy "
                        "frontal, ex. 127.0.0.1, 10.0.0.0/24. Les en-têtes d'identité ne "
                        "sont crus QUE depuis ces adresses (adresse socket réelle, jamais "
                        "X-Forwarded-For).")},
            {"path": "auth.proxy.user_header", "label": _l("En-tête utilisateur du proxy"), "type": "text",
             "help": _l("Nom de l'en-tête portant l'identifiant (convention : Remote-User).")},
            {"path": "auth.proxy.groups_header", "label": _l("En-tête groupes du proxy"), "type": "text",
             "help": _l("Nom de l'en-tête portant les groupes, séparés par des virgules "
                        "(convention : Remote-Groups).")},
            {"path": "auth.proxy.auto_login", "label": _l("Connexion automatique via le proxy"), "type": "bool",
             "help": _l("Activée : /login connecte directement depuis les en-têtes. "
                        "Désactivée : la page affiche un bouton de connexion.")},
            {"path": "auth.ldap.servers", "label": _l("Contrôleurs LDAP/AD"), "type": "csv",
             "help": _l("Backend ldap : un ou plusieurs contrôleurs, ex. ldaps://dc1.corp, "
                        "ldaps://dc2.corp (essayés dans l'ordre pour la haute disponibilité). "
                        "LDAPS (ldaps://) fortement recommandé.")},
            {"path": "auth.ldap.use_ssl", "label": _l("LDAPS (TLS)"), "type": "bool",
             "help": _l("Chiffre la liaison à l'annuaire. À désactiver seulement avec "
                        "auth.ldap.allow_plaintext en connaissance de cause (mot de passe en clair).")},
            {"path": "auth.ldap.bind_mode", "label": _l("Mode de connexion LDAP"), "type": "select",
             "options": ["service", "direct"],
             "help": _l("service (recommandé AD) : un compte de service recherche l'utilisateur "
                        "puis on valide son mot de passe. direct : bind direct via un gabarit de DN.")},
            {"path": "auth.ldap.service_dn", "label": _l("DN du compte de service"), "type": "text",
             "help": _l("Mode service : compte de lecture de l'annuaire, ex. "
                        "CN=svc-transcria,OU=Services,DC=corp,DC=example.")},
            {"path": "auth.ldap.service_password", "label": _l("Mot de passe du compte de service"),
             "type": "password", "secret": True,
             "help": _l("Laissez vide si fourni par variable d'environnement.")},
            {"path": "auth.ldap.service_password_env", "label": _l("Variable d'environnement du mot de passe de service"),
             "type": "text",
             "help": _l("Nom d'une variable d'environnement portant le mot de passe — prioritaire, "
                        "évite le secret en clair dans config.yaml.")},
            {"path": "auth.ldap.base_dn", "label": _l("Base de recherche (base DN)"), "type": "text",
             "help": _l("Racine de recherche des comptes, ex. DC=corp,DC=example.")},
            {"path": "auth.ldap.user_filter", "label": _l("Filtre de recherche utilisateur"), "type": "text",
             "help": _l("Doit contenir {username} (échappé automatiquement). AD : "
                        "(&(objectClass=user)(sAMAccountName={username})).")},
            {"path": "auth.ldap.resolve_nested_groups", "label": _l("Résoudre les groupes imbriqués (AD)"),
             "type": "bool",
             "help": _l("Suit l'appartenance transitive des groupes (règle en chaîne AD). "
                        "Plus complet mais plus coûteux — laisser désactivé si non nécessaire.")},
            {"path": "auth.role_mapping.claim", "label": _l("Claim des groupes"), "type": "text",
             "help": _l("Nom du claim OIDC portant les groupes (généralement « groups »). "
                        "Ignoré en LDAP (memberOf) et proxy (en-tête des groupes).")},
            {"path": "auth.role_mapping.rules", "label": _l("Règles groupe → rôle"), "type": "group_role_rules",
             "help": _l("Une règle par ligne : « groupe = rôle » (rôles : admin, manager, operator, "
                        "viewer). Premier groupe correspondant gagne, égalité stricte. Ex. : "
                        "transcria-admins = admin")},
            {"path": "auth.role_mapping.default", "label": _l("Rôle par défaut (aucun groupe reconnu)"), "type": "select",
             "options": ["deny", "viewer"],
             "help": _l("deny (recommandé) : connexion refusée sans groupe reconnu. "
                        "viewer : accès en lecture seule.")},
        ],
    },
    {
        "title": _l("Serveur & compte admin"),
        "help": _l("Écoute du portail et mot de passe du premier administrateur."),
        "fields": [
            {"path": "server.host", "label": _l("Hôte d'écoute"), "type": "text"},
            {"path": "server.port", "label": _l("Port d'écoute"), "type": "int"},
            {"path": "auth.first_admin_password", "label": _l("Mot de passe admin initial"), "type": "password",
             "secret": True,
             "help": _l("Changez la valeur par défaut avant tout usage réel.")},
        ],
    },
]


def iter_fields(sections: list[dict]):
    """Itère sur tous les champs de toutes les sections."""
    for section in sections:
        for field in section.get("fields", []):
            yield field


def get_dotted(data: dict, path: str, default=None):
    """Lit une valeur via un chemin pointé (`a.b.c`). `default` si absent."""
    current = data
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return default
    return current


def set_dotted(data: dict, path: str, value) -> None:
    """Écrit une valeur via un chemin pointé, en créant les dicts intermédiaires."""
    parts = path.split(".")
    current = data
    for part in parts[:-1]:
        nxt = current.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            current[part] = nxt
        current = nxt
    current[parts[-1]] = value


def coerce_value(field: dict, raw):
    """Convertit la valeur brute du formulaire selon le type du champ.

    `bool` : présence (case cochée) → True/False. `int` : entier ou None si vide.
    `csv` : liste nettoyée. Autres : chaîne nettoyée.
    """
    ftype = field.get("type", "text")
    if ftype == "bool":
        return bool(raw)
    if ftype == "int":
        raw = (raw or "").strip() if isinstance(raw, str) else raw
        if raw in (None, ""):
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    if ftype == "csv":
        return [item.strip() for item in (raw or "").split(",") if item.strip()]
    if ftype == "group_role_rules":
        # Une règle par ligne « groupe = rôle ». Découpage sur le DERNIER « = » :
        # un DN Active Directory contient des « = » (CN=…,OU=…), jamais le rôle.
        rules = []
        for line in (raw or "").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            group, sep, role = line.rpartition("=")
            if sep:
                rules.append({"group": group.strip(), "role": role.strip()})
            else:
                # Ligne sans « = » : conservée telle quelle pour que la validation
                # du mapping la signale à la sauvegarde (jamais de perte muette).
                rules.append({"group": line, "role": ""})
        return rules
    value = (raw or "").strip() if isinstance(raw, str) else raw
    # Champ `nullable` (ex. select avec option vide « défaut ») : la valeur vide
    # signifie EXPLICITEMENT null — elle sera écrite (pas ignorée) au save.
    if field.get("nullable") and value == "":
        return None
    return value


def build_partial_config(form, sections: list[dict]) -> dict:
    """Construit le dict partiel imbriqué à partir des champs gérés du formulaire.

    Ne contient **que** les chemins de `sections` → fusionnable sans perte via _deep_merge.
    Les valeurs `int` vides (None) sont omises pour ne pas écraser un défaut par None.
    """
    partial: dict = {}
    for field in iter_fields(sections):
        path = field["path"]
        if field.get("type") == "bool":
            value = coerce_value(field, form.get(path))  # case absente → False
        else:
            if path not in form:
                continue
            value = coerce_value(field, form.get(path))
            # None = « ne pas toucher » (int vide) — SAUF champ nullable, où None
            # est une valeur légitime à écrire (retour au défaut « comme le pipeline »).
            if value is None and not field.get("nullable"):
                continue
        set_dotted(partial, path, value)
    return partial


def secret_paths(sections: list[dict]) -> list[str]:
    """Chemins des champs marqués secret (masqués à l'affichage)."""
    return [f["path"] for f in iter_fields(sections) if f.get("secret")]


def display_values(cfg: dict, sections: list[dict]) -> dict:
    """Valeurs de pré-remplissage par chemin, secrets masqués par le sentinelle."""
    secrets = set(secret_paths(sections))
    values: dict = {}
    for field in iter_fields(sections):
        path = field["path"]
        value = get_dotted(cfg, path)
        if path in secrets and value:
            value = SECRET_SENTINEL
        elif field.get("type") == "group_role_rules" and isinstance(value, list):
            value = "\n".join(f"{r.get('group', '')} = {r.get('role', '')}"
                              for r in value if isinstance(r, dict))
        values[path] = value
    return values


def restore_masked_secrets(submitted: dict, current_cfg: dict, sections: list[dict]) -> dict:
    """Remplace un secret resté au sentinelle par sa valeur courante (jamais le sentinelle)."""
    restored = copy.deepcopy(submitted)
    for path in secret_paths(sections):
        if get_dotted(restored, path) == SECRET_SENTINEL:
            set_dotted(restored, path, get_dotted(current_cfg, path, ""))
    return restored
