# Référence d'API — GÉNÉRÉE, ne pas éditer

> Fichier produit par `scripts/generate_api_reference.py` (vague C8) et gardé en CI
> (`tests/test_api_reference.py`). Après tout ajout/changement de route :
> `venv/bin/python scripts/generate_api_reference.py` puis committer le diff.
>
> **Contrat scriptable** : les routes marquées ⭐ (``__api_stable__``) forment le
> parcours upload → process → status → download que les auto-hébergeurs peuvent
> scripter — c'est un contrat ; le reste est interne et peut bouger.


## Portail TranscrIA (app principale)

### Blueprint `(app)`

| Route | Méthodes | Auth | Description | Module |
|---|---|---|---|---|
| `/i18n/messages.js` | GET | — | Catalogue JS : ``window.I18N`` = { source_fr: traduction } pour la locale courante. | `transcria.web.i18n` |

### Blueprint `audit`

| Route | Méthodes | Auth | Description | Module |
|---|---|---|---|---|
| `/admin/audit` | GET | connexion + Permission.ACCESS_SYSTEM | _(docstring manquante)_ | `transcria.audit.routes` |
| `/admin/audit/export.csv` | GET | connexion + Permission.ACCESS_SYSTEM | _(docstring manquante)_ | `transcria.audit.routes` |

### Blueprint `auth`

| Route | Méthodes | Auth | Description | Module |
|---|---|---|---|---|
| `/account/password` | GET,POST | connexion requise | _(docstring manquante)_ | `transcria.auth.routes` |
| `/admin/groups` | GET | connexion requise | _(docstring manquante)_ | `transcria.auth.routes` |
| `/admin/groups/<group_id>/edit` | GET,POST | connexion requise | _(docstring manquante)_ | `transcria.auth.routes` |
| `/admin/groups/new` | GET,POST | connexion + Permission.MANAGE_USERS | _(docstring manquante)_ | `transcria.auth.routes` |
| `/admin/users` | GET | connexion + Permission.MANAGE_USERS | _(docstring manquante)_ | `transcria.auth.routes` |
| `/admin/users/<user_id>/edit` | GET,POST | connexion + Permission.MANAGE_USERS | _(docstring manquante)_ | `transcria.auth.routes` |
| `/admin/users/new` | GET,POST | connexion + Permission.MANAGE_USERS | _(docstring manquante)_ | `transcria.auth.routes` |
| `/auth/oidc/callback` | GET | — | Retour de l'IdP : validation complète, JIT, session — ou refus audité. | `transcria.auth.routes` |
| `/auth/oidc/login` | GET | — | Chantier identité lot 1 : départ du flux Authorization Code + PKCE. | `transcria.auth.routes` |
| `/auth/proxy/login` | GET | — | Chantier identité lot 3 : connexion par en-têtes de proxy de confiance. | `transcria.auth.routes` |
| `/login` | GET,POST | — | _(docstring manquante)_ | `transcria.auth.routes` |
| `/logout` | POST | connexion requise | _(docstring manquante)_ | `transcria.auth.routes` |

### Blueprint `central_lexicon`

| Route | Méthodes | Auth | Description | Module |
|---|---|---|---|---|
| `/admin/lexicons` | GET | connexion requise | _(docstring manquante)_ | `transcria.context.central_lexicon_routes` |
| `/admin/lexicons/<lexicon_id>` | GET | connexion requise | _(docstring manquante)_ | `transcria.context.central_lexicon_routes` |
| `/admin/lexicons/<lexicon_id>/delete` | POST | connexion requise | _(docstring manquante)_ | `transcria.context.central_lexicon_routes` |
| `/admin/lexicons/<lexicon_id>/entries` | POST | connexion requise | _(docstring manquante)_ | `transcria.context.central_lexicon_routes` |
| `/admin/lexicons/<lexicon_id>/entries/<entry_id>` | POST | connexion requise | _(docstring manquante)_ | `transcria.context.central_lexicon_routes` |
| `/admin/lexicons/<lexicon_id>/entries/<entry_id>/delete` | POST | connexion requise | _(docstring manquante)_ | `transcria.context.central_lexicon_routes` |
| `/admin/lexicons/<lexicon_id>/export.csv` | POST | connexion requise | _(docstring manquante)_ | `transcria.context.central_lexicon_routes` |
| `/admin/lexicons/<lexicon_id>/import` | POST | connexion requise | _(docstring manquante)_ | `transcria.context.central_lexicon_routes` |
| `/admin/lexicons/<lexicon_id>/metadata` | POST | connexion requise | _(docstring manquante)_ | `transcria.context.central_lexicon_routes` |
| `/admin/lexicons/new` | GET,POST | connexion requise | _(docstring manquante)_ | `transcria.context.central_lexicon_routes` |

### Blueprint `meeting_types`

| Route | Méthodes | Auth | Description | Module |
|---|---|---|---|---|
| `/api/meeting-types` | GET | connexion requise | Catalogue de l'utilisateur courant : intégrés + personnalisés visibles. | `transcria.context.meeting_type_routes` |
| `/api/meeting-types` | POST | connexion requise | _(docstring manquante)_ | `transcria.context.meeting_type_routes` |
| `/api/meeting-types/<template_id>` | DELETE | connexion requise | _(docstring manquante)_ | `transcria.context.meeting_type_routes` |
| `/api/meeting-types/<template_id>` | PUT | connexion requise | _(docstring manquante)_ | `transcria.context.meeting_type_routes` |
| `/api/meeting-types/<template_id>/export` | GET | connexion requise | Fichier d'échange du type (schéma du catalogue, SANS branding) — §8. | `transcria.context.meeting_type_routes` |
| `/api/meeting-types/<template_id>/logo` | DELETE,POST | connexion requise | Logo du type — branding LOCAL (re-encodé, jamais exporté ni importé). | `transcria.context.meeting_type_routes` |
| `/api/meeting-types/<template_id>/preview.docx` | GET | connexion requise | Aperçu d'un type ENREGISTRÉ (avec son logo) — visible de l'utilisateur requis. | `transcria.context.meeting_type_routes` |
| `/api/meeting-types/<template_id>/scope` | POST | connexion requise | _(docstring manquante)_ | `transcria.context.meeting_type_routes` |
| `/api/meeting-types/import` | POST | connexion requise | Import d'un fichier d'échange → type PRIVÉ, INACTIF (à relire) — §8.2. | `transcria.context.meeting_type_routes` |
| `/api/meeting-types/preview.docx` | POST | connexion requise | Aperçu AVANT enregistrement : la définition en cours d'édition → DOCX d'exemple. | `transcria.context.meeting_type_routes` |
| `/meeting-types` | GET | connexion requise | Page « Mes types de réunion » — galerie + éditeur (lot E). | `transcria.context.meeting_type_routes` |

### Blueprint `queue_api`

| Route | Méthodes | Auth | Description | Module |
|---|---|---|---|---|
| `/api/queue/<job_id>/cancel` | POST | connexion requise | _(docstring manquante)_ | `transcria.queue.routes` |
| `/api/queue/<job_id>/move-down` | POST | connexion requise | _(docstring manquante)_ | `transcria.queue.routes` |
| `/api/queue/<job_id>/move-up` | POST | connexion requise | _(docstring manquante)_ | `transcria.queue.routes` |
| `/api/queue/<job_id>/pause` | POST | connexion requise | _(docstring manquante)_ | `transcria.queue.routes` |
| `/api/queue/<job_id>/priority` | POST | connexion requise | _(docstring manquante)_ | `transcria.queue.routes` |
| `/api/queue/<job_id>/resume` | POST | connexion requise | _(docstring manquante)_ | `transcria.queue.routes` |
| `/api/queue/e2e-test-jobs/purge` | POST | connexion requise | _(docstring manquante)_ | `transcria.queue.routes` |
| `/api/queue/status` | GET | connexion requise | _(docstring manquante)_ | `transcria.queue.routes` |
| `/api/schedule/enabled` | POST | connexion + Permission.MANAGE_SCHEDULE | Activer/désactiver l'AGENDA ENTIER depuis la page (constat audit C3.6 : on | `transcria.queue.routes` |
| `/api/schedule/windows` | GET | connexion + Permission.MANAGE_SCHEDULE | _(docstring manquante)_ | `transcria.queue.routes` |
| `/api/schedule/windows` | POST | connexion + Permission.MANAGE_SCHEDULE | _(docstring manquante)_ | `transcria.queue.routes` |
| `/api/schedule/windows/<int:window_id>` | DELETE | connexion + Permission.MANAGE_SCHEDULE | _(docstring manquante)_ | `transcria.queue.routes` |
| `/api/schedule/windows/<int:window_id>` | PUT | connexion + Permission.MANAGE_SCHEDULE | _(docstring manquante)_ | `transcria.queue.routes` |

### Blueprint `queue_pages`

| Route | Méthodes | Auth | Description | Module |
|---|---|---|---|---|
| `/admin/queue` | GET | connexion requise | _(docstring manquante)_ | `transcria.queue.routes` |
| `/admin/schedule` | GET | connexion + Permission.MANAGE_SCHEDULE | _(docstring manquante)_ | `transcria.queue.routes` |

### Blueprint `srt_editor`

| Route | Méthodes | Auth | Description | Module |
|---|---|---|---|---|
| `/api/jobs/<job_id>/audio/stream` | GET | connexion requise | _(docstring manquante)_ | `transcria.web.editor_routes` |
| `/api/jobs/<job_id>/editor/draft` | DELETE | connexion requise | _(docstring manquante)_ | `transcria.web.editor_routes` |
| `/api/jobs/<job_id>/editor/draft` | GET | connexion requise | Contenu du brouillon (écran « Reprendre où vous en étiez »). | `transcria.web.editor_routes` |
| `/api/jobs/<job_id>/editor/draft` | PUT | connexion requise | _(docstring manquante)_ | `transcria.web.editor_routes` |
| `/api/jobs/<job_id>/editor/peaks` | GET | connexion requise | _(docstring manquante)_ | `transcria.web.editor_routes` |
| `/api/jobs/<job_id>/editor/save` | POST | connexion requise | _(docstring manquante)_ | `transcria.web.editor_routes` |
| `/api/jobs/<job_id>/editor/state` | GET | connexion requise | _(docstring manquante)_ | `transcria.web.editor_routes` |
| `/api/jobs/<job_id>/editor/sync-summary` | POST | connexion requise | Enfile la passe LLM « synthèse mise à jour depuis le SRT corrigé ». | `transcria.web.editor_routes` |
| `/jobs/<job_id>/editor` | GET | connexion requise | _(docstring manquante)_ | `transcria.web.editor_routes` |

### Blueprint `voice`

| Route | Méthodes | Auth | Description | Module |
|---|---|---|---|---|
| `/admin/voices` | GET | connexion requise | _(docstring manquante)_ | `transcria.voice.routes` |
| `/admin/voices/<subject_id>` | GET | connexion requise | _(docstring manquante)_ | `transcria.voice.routes` |
| `/admin/voices/<subject_id>/consent-proof/<consent_id>` | GET | connexion requise | _(docstring manquante)_ | `transcria.voice.routes` |
| `/admin/voices/<subject_id>/consents` | POST | connexion requise | _(docstring manquante)_ | `transcria.voice.routes` |
| `/admin/voices/<subject_id>/disable` | POST | connexion requise | _(docstring manquante)_ | `transcria.voice.routes` |
| `/admin/voices/<subject_id>/generate` | POST | connexion requise | _(docstring manquante)_ | `transcria.voice.routes` |
| `/admin/voices/<subject_id>/metadata` | POST | connexion requise | _(docstring manquante)_ | `transcria.voice.routes` |
| `/admin/voices/consent-form.pdf` | GET | connexion requise | _(docstring manquante)_ | `transcria.voice.routes` |
| `/admin/voices/new` | GET,POST | connexion requise | _(docstring manquante)_ | `transcria.voice.routes` |

### Blueprint `web`

| Route | Méthodes | Auth | Description | Module |
|---|---|---|---|---|
| `/` | GET | connexion requise | _(docstring manquante)_ | `transcria.web.pages_routes` |
| `/admin/config` | GET,POST | connexion + Permission.MANAGE_CONFIG | _(docstring manquante)_ | `transcria.web.admin_routes` |
| `/admin/hardware` | GET,POST | connexion + Permission.MANAGE_CONFIG | Préconisations matériel (lot conseiller) : scan GPU vs config courante. | `transcria.web.admin_routes` |
| `/admin/maintenance` | GET | connexion + Permission.MANAGE_CONFIG | _(docstring manquante)_ | `transcria.web.admin_routes` |
| `/admin/maintenance/backup` | POST | connexion + Permission.MANAGE_CONFIG | _(docstring manquante)_ | `transcria.web.admin_routes` |
| `/admin/maintenance/backup/<name>/download` | GET | connexion + Permission.MANAGE_CONFIG | _(docstring manquante)_ | `transcria.web.admin_routes` |
| `/admin/maintenance/restore` | POST | connexion + Permission.MANAGE_CONFIG | _(docstring manquante)_ | `transcria.web.admin_routes` |
| `/admin/maintenance/schedule` | POST | connexion + Permission.MANAGE_CONFIG | _(docstring manquante)_ | `transcria.web.admin_routes` |
| `/admin/models` | GET | connexion + Permission.MANAGE_CONFIG | _(docstring manquante)_ | `transcria.web.admin_routes` |
| `/admin/models/activate` | POST | connexion + Permission.MANAGE_CONFIG | _(docstring manquante)_ | `transcria.web.admin_routes` |
| `/admin/models/download` | POST | connexion + Permission.MANAGE_CONFIG | _(docstring manquante)_ | `transcria.web.admin_routes` |
| `/admin/models/progress/<role>` | GET | connexion + Permission.MANAGE_CONFIG | _(docstring manquante)_ | `transcria.web.admin_routes` |
| `/api/jobs/<job_id>/analyze` | POST | connexion requise | _(docstring manquante)_ | `transcria.web.wizard_api` |
| `/api/jobs/<job_id>/audio/excerpt` | GET | connexion requise | _(docstring manquante)_ | `transcria.web.downloads_api` |
| `/api/jobs/<job_id>/available-lexicons` | GET | connexion requise | _(docstring manquante)_ | `transcria.web.lexicon_api` |
| `/api/jobs/<job_id>/context` | POST | connexion requise | _(docstring manquante)_ | `transcria.web.wizard_api` |
| `/api/jobs/<job_id>/download/audio` | GET | connexion requise | _(docstring manquante)_ | `transcria.web.downloads_api` |
| ⭐ `/api/jobs/<job_id>/download/docx` | GET | connexion requise | Télécharge le compte rendu Word (DOCX — contrat scriptable). | `transcria.web.downloads_api` |
| ⭐ `/api/jobs/<job_id>/download/package` | GET | connexion requise | Télécharge le paquet complet des livrables (ZIP — contrat scriptable). | `transcria.web.downloads_api` |
| ⭐ `/api/jobs/<job_id>/download/srt` | GET | connexion requise | Télécharge le sous-titrage corrigé (SRT — contrat scriptable). | `transcria.web.downloads_api` |
| `/api/jobs/<job_id>/export` | POST | connexion requise | _(docstring manquante)_ | `transcria.web.processing_api` |
| `/api/jobs/<job_id>/lexicon` | POST | connexion requise | _(docstring manquante)_ | `transcria.web.lexicon_api` |
| `/api/jobs/<job_id>/lexicon/debug` | GET | connexion requise | Diagnostic lexique pour faciliter le débogage des affichages contextes. | `transcria.web.lexicon_api` |
| `/api/jobs/<job_id>/lexicon/promote` | POST | connexion requise | Étape 6 : pousser une forme validée du lexique de SESSION vers un lexique | `transcria.web.lexicon_api` |
| `/api/jobs/<job_id>/meeting-invite` | POST | connexion requise | Mémorise une invitation de réunion collée (objet, corps, destinataires). | `transcria.web.wizard_api` |
| `/api/jobs/<job_id>/meeting-invite/document` | POST | connexion requise | Joint un document présenté (PDF/DOCX/PPTX/TXT) au contexte du résumé. | `transcria.web.wizard_api` |
| `/api/jobs/<job_id>/meeting-invite/document/<int:index>` | DELETE | connexion requise | Retire un document joint (par position dans la liste). | `transcria.web.wizard_api` |
| `/api/jobs/<job_id>/participants` | POST | connexion requise | _(docstring manquante)_ | `transcria.web.wizard_api` |
| ⭐ `/api/jobs/<job_id>/process` | POST | connexion requise | Lance le traitement complet du job (mise en file — contrat scriptable). | `transcria.web.processing_api` |
| `/api/jobs/<job_id>/profile` | POST | connexion requise | Persiste le profil choisi à l'étape 1 (le wizard adapte alors ses étapes au profil). | `transcria.web.wizard_api` |
| `/api/jobs/<job_id>/quality` | POST | connexion requise | _(docstring manquante)_ | `transcria.web.processing_api` |
| `/api/jobs/<job_id>/refine` | POST | connexion requise | _(docstring manquante)_ | `transcria.web.refine_api` |
| `/api/jobs/<job_id>/refine/chat` | GET | connexion requise | Endpoint de polling unique du panneau : tours + busy + versions + options. | `transcria.web.refine_api` |
| `/api/jobs/<job_id>/refine/render-options` | POST | connexion requise | Options de rendu déterministes SANS LLM (thème/sections) — effet immédiat. | `transcria.web.refine_api` |
| `/api/jobs/<job_id>/refine/revert` | POST | connexion requise | Restaure un snapshot pris AVANT une application (retour arrière utilisateur). | `transcria.web.refine_api` |
| `/api/jobs/<job_id>/reprocess` | POST | connexion requise | Relance le traitement d'un job déjà terminé (lexique modifié, prompt mis à jour…). | `transcria.web.processing_api` |
| `/api/jobs/<job_id>/selected-lexicons` | POST | connexion requise | _(docstring manquante)_ | `transcria.web.lexicon_api` |
| `/api/jobs/<job_id>/speaker-hint` | POST | connexion requise | Mémorise la fourchette de locuteurs (min/max) choisie par l'utilisateur. | `transcria.web.wizard_api` |
| `/api/jobs/<job_id>/speakers/clip/<path:clip_name>` | GET | connexion requise | _(docstring manquante)_ | `transcria.web.downloads_api` |
| `/api/jobs/<job_id>/speakers/clips` | GET | connexion requise | _(docstring manquante)_ | `transcria.web.downloads_api` |
| `/api/jobs/<job_id>/speakers/detect` | POST | connexion requise | _(docstring manquante)_ | `transcria.web.wizard_api` |
| `/api/jobs/<job_id>/speakers/map` | POST | connexion requise | _(docstring manquante)_ | `transcria.web.wizard_api` |
| `/api/jobs/<job_id>/speakers/voice-match` | POST | connexion requise | _(docstring manquante)_ | `transcria.web.wizard_api` |
| ⭐ `/api/jobs/<job_id>/status` | GET | connexion requise | Endpoint léger de polling — état courant du job pendant le traitement (contrat scriptable). | `transcria.web.processing_api` |
| `/api/jobs/<job_id>/summary` | POST | connexion requise | _(docstring manquante)_ | `transcria.web.wizard_api` |
| ⭐ `/api/jobs/<job_id>/upload` | POST | connexion requise | Dépose le fichier audio d'un job fraîchement créé (contrat scriptable). | `transcria.web.wizard_api` |
| `/api/profiles/availability` | GET | connexion requise | Profils de traitement disponibles + profil recommandé (source unique pour le wizard). | `transcria.web.wizard_api` |
| `/api/resources/status` | GET | connexion requise | État des ressources distantes pour le panneau frontale (mode dégradé inclus). | `transcria.web.processing_api` |
| `/api/system/status` | GET | connexion + Permission.ACCESS_SYSTEM | _(docstring manquante)_ | `transcria.web.processing_api` |
| `/health` | GET | — | _(docstring manquante)_ | `transcria.web.health_routes` |
| `/jobs/<job_id>` | GET | connexion requise | _(docstring manquante)_ | `transcria.web.pages_routes` |
| `/jobs/<job_id>/delete` | POST | connexion + Permission.DELETE_JOBS | _(docstring manquante)_ | `transcria.web.pages_routes` |
| `/jobs/<job_id>/result` | GET | connexion requise | _(docstring manquante)_ | `transcria.web.pages_routes` |
| `/jobs/new` | POST | connexion + Permission.CREATE_JOBS | _(docstring manquante)_ | `transcria.web.pages_routes` |
| `/metrics` | GET | — | _(docstring manquante)_ | `transcria.web.health_routes` |
| `/ready` | GET | — | _(docstring manquante)_ | `transcria.web.health_routes` |
| `/system` | GET | connexion + Permission.ACCESS_SYSTEM | _(docstring manquante)_ | `transcria.web.pages_routes` |

_Portail TranscrIA (app principale) : 126 routes, 92 sans docstring._

## Service d'inférence (nœud de ressources)

### Blueprint `capabilities`

| Route | Méthodes | Auth | Description | Module |
|---|---|---|---|---|
| `/capabilities` | GET | — | _(docstring manquante)_ | `inference_service.routes.capabilities` |

### Blueprint `diarize`

| Route | Méthodes | Auth | Description | Module |
|---|---|---|---|---|
| `/infer/diarize` | POST | — | _(docstring manquante)_ | `inference_service.routes.diarize` |

### Blueprint `engines`

| Route | Méthodes | Auth | Description | Module |
|---|---|---|---|---|
| `/engines/ensure` | POST | — | _(docstring manquante)_ | `inference_service.routes.engines` |

### Blueprint `health`

| Route | Méthodes | Auth | Description | Module |
|---|---|---|---|---|
| `/health` | GET | — | Le process répond — ne charge aucun modèle, toujours 200 si vivant. | `inference_service.routes.health` |
| `/models` | GET | — | Inventaire des modèles servis et leur état (loaded/unloaded, device…). | `inference_service.routes.health` |
| `/ready` | GET | — | Prêt à servir : les moteurs existent et peuvent charger/servent déjà. | `inference_service.routes.health` |

### Blueprint `voice_embed`

| Route | Méthodes | Auth | Description | Module |
|---|---|---|---|---|
| `/infer/voice-embed` | POST | — | _(docstring manquante)_ | `inference_service.routes.voice_embed` |

_Service d'inférence (nœud de ressources) : 7 routes, 4 sans docstring._
