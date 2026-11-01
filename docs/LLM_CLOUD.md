# LLM d'arbitrage « cloud » (fournisseur externe, passe-plat) — plan directeur

> Statut (2026-11-01) : **lots A, B, C, D livrés** (5729b19, 9266dc9, e94fdfa, 4b70384 ; correctif de migration 8c54f14), **lot E à faire** (qualification réelle — premier constat déjà intégré : ce que prouve `GET /models` dépend du fournisseur, § 3.3.1 et § 8). Plan validé dans ses décisions. Rédigé après une étude
> de code par sous-agent, la lecture des sources d'opencode (v1.19.5 et v2.2.5, clones locaux),
> des sondes réelles, deux vérifications indépendantes (sources opencode ; inventaire de notre
> code), puis une **relecture critique** (17 références contrôlées, trous et contradictions
> relevés — intégrés ici). **VÉRIFIÉ** = au code (fichier:ligne) ou par un appel réel ;
> **NON VÉRIFIÉ** = à contrôler avant de s'y fier.

## 0. Cadrage

Des utilisateurs sous contrat avec un fournisseur d'inférence (Mistral, Ollama cloud, OpenRouter,
le fournisseur d'opencode…) veulent l'utiliser comme LLM d'arbitrage (résumé, correction,
relecture finale, affinage) à la place de la LLM locale.

**Principe : TranscrIA est un passe-plat.** L'admin a choisi, contracté et surveille son
fournisseur ; TranscrIA transmet les prompts et reçoit les réponses. Notre travail se limite à
trois choses : **que ça marche** (le code suppose aujourd'hui une LLM locale), **que ce soit dit**
(les transcriptions quittent le serveur : validation admin, bandeau permanent, interdiction par
groupe, trace par job), **que ça ne casse rien chez nous** (file, admission, Docker, tests). Tout
le reste est hors périmètre et dit comme tel à l'admin : coûts et consommation, disponibilité du
fournisseur, qualité des modèles tiers, STT ou diarisation externes.

Règle de conception : **changer le moins possible**. Les quatre phases agentiques passent déjà par
opencode, qui sait parler à tous ces fournisseurs ; les trois appels directs restent directs avec
le strict minimum d'adaptation. **La bascule local ↔ cloud exige un redémarrage du service**, comme
la bascule de palier aujourd'hui (la config est capturée au démarrage par l'exécuteur et
l'ordonnanceur ; en split l'ordonnanceur est un autre process) : c'est écrit dans l'écran, pas
contourné par une refonte.

**Le STT, la diarisation, les empreintes vocales et le SQUIM restent locaux** (GPU, ou CPU avec
Kroko / Whisper CPU / Nemotron 3 via `audiocpp_cli`). Conséquence : avec la LLM en cloud, **une
machine sans GPU devient une installation complète** (§ 4).

## 1. Ce que notre code fait déjà (VÉRIFIÉ ; inventaire au § 7)

- Toutes les phases agentiques passent par `opencode run --model <fournisseur>/<modèle>`
  (`transcria/llm_tools/opencode_runner.py`, `opencode_cli.py`). `OpenCodeRunner` coupe déjà
  `model_id` sur le premier `/`. **Deux `model_id`** : le résumé lit `workflow.summary_llm.model_id`
  (`phases/summary_llm.py:97,118`) ; correction, relecture finale, affinage et appels directs
  lisent `workflow.arbitration_llm.model_id` (`opencode_runner.py:129`, `llm_backend.py:240,455`).
  La bascule Ollama écrit déjà les deux (`admin_routes.py:949-950`).
- **Le seul interrupteur « LLM distante » est l'hôte** : `arbitrage_endpoint.is_remote_arbitrage`
  = hôte configuré ≠ loopback. Tout en découle. **Sans hôte configuré, un backend cloud serait
  traité comme une LLM locale** : lancement de `llama-server`, sonde `127.0.0.1:8080`, arrêt,
  réservation VRAM, admission.
- `_detect_backend_type` (`gpu/llm_backend.py:210-221`) et `is_ollama_backend`
  (`arbitrage_endpoint.py:17-24`) **ignorent une valeur inconnue** de `services.backend` et
  retombent sur l'auto-détection. Le schéma ne valide pas la clé ; `loader.py` n'a pas de défaut.
- `workflow.arbitration_llm.api_base` **existe déjà** : lu par `HTTPLLMBackend.base_url`
  (`llm_backend.py:458-461`), `https://` accepté par le validateur (`checks/orchestration.py:101-109`),
  classé (`config_classification.yaml:355`). `LLMBackend.base_url` par défaut vaut
  `http://127.0.0.1:{port}/v1` et ignore `arbitrage_llm_host` (`llm_backend.py:30-31`).
- **Trois appels directs**, tous par `workflow/refine_llm.chat_completion` : mode « discuter » du
  chat d'affinage (`phases/refine.py:303`, 900 s, 2000 tokens), arbitrage du multi-STT
  (`phases/multi_stt_review.py:173`, 120 s, 16 tokens, un appel par candidat), extraction des
  champs de type en relecture finale (`phases/final_review.py:375`, 600 s, 1500 tokens). Tous
  best-effort. La couture : `base_url/chat/completions`, **sans `Authorization`**, en `http://`,
  champ non standard `chat_template_kwargs.enable_thinking` retenté sans lui sur **toute** erreur
  HTTP (`refine_llm.py:165-174`), ne retire que `local/` (l.145-148).
- **Chien de garde d'`OpenCodeRunner`** : pré-garde TCP `_wait_llm_endpoint_ready` (l.395) sur
  l'endpoint local → en cloud sans hôte, **le run ne démarre jamais** ; `_llm_is_processing`
  (l.244) sonde `/slots` puis `/metrics`, **rend `False` si les deux refusent** → kill à
  `opencode_idle_grace_s` = 120 s (l.504, défaut l.454) ; deuxième règle `first_contact_grace_s`
  (120 s sans événement, l.494-498) ; repli « inactivité pure » 600 s (`stop_on_pure_idle`,
  l.95-108, dont la régression documentée : relecture finale tuée sur une réunion de 1 h 52).
  **opencode v1 n'émet `text`/`tool_use` qu'en fin de génération** (`cli/cmd/run.ts:714,744`) :
  sans sonde d'activité, toute génération > 600 s serait tuée.
- Le parseur d'événements lit `error.name` + `error.data.message` (**forme v1**,
  `opencode_runner.py:648-654`) ; les `step_finish` (tokens) ne sont pas lus.
- `correction.py:111-123` : `ensure_arbitrage_llm_ready` en échec **et** distant → `vram_wait`
  (re-queue) ; `workflow.vram_wait.max_wait_s` vaut 0 = illimité (`job_executor.py:318-321`).
  **En cloud, une panne ou une clé révoquée rebouclerait sans fin**, avec l'e-mail admin « attente
  de VRAM » trompeur.
- Le `.env` est **sourcé par `start.sh:63-67`** (`set -a; source`), pas par systemd (pas
  d'`EnvironmentFile` dans `transcria.service`) ; `app.py` le relit via python-dotenv. Une clé
  contenant `$`, guillemets ou espaces peut être altérée par le `source`.
- Le DOCX est généré **depuis les seuls fichiers du job** (`generate_docx_report`, lit `metadata/`
  via `fs`, `docx_report.py:1144-1169`) ; le pied de page n'est pas dans l'empreinte du golden
  (l.48-83). `metadata/` est **déjà** dans `SYNCED_PREFIXES` (`artifact_store.py:46`) et **surveillé
  par `AgentWorkspace`** (`agent_workspace.py:46,199-210` : écrire dedans pendant un run d'agent
  produit l'ERROR « canonique altéré »).
- `audit_log` (Flask) n'est pas importable depuis `workflow/`, `services/`, `queue/` ;
  `AuditStore.log` l'est (acteur `system` hors requête). Une action `external_push` existe déjà.
- **Aucun indicateur « LLM OK » dans l'interface** : `system_status.py` ne sonde aucune LLM ; seul
  le doctor le fait.
- Le consentement vocal stocke `form_version` par ligne (`voice/models.py:78`) et la version
  courante en config (`loader.py:43`), **mais aucun code ne compare les deux** : le patron de
  caducité est à écrire.
- `Group` n'a que `id`, `name`, `description`, `created_at` (`auth/models.py:130-150`).
- Le sous-process opencode hérite de **tout** l'environnement du service (`build_run_env(dict(os.environ))`).
  Préexistant, identique en local, **hors périmètre** (ticket séparé « liste blanche »).

## 2. Ce qu'opencode fait (VÉRIFIÉ aux sources v1.19.5 / v2.2.5 et par sondes)

### 2.1 Identifiants et priorités
- `/connect` → « add token » stocke une **clé d'API** ; seuls les abonnements Claude et ChatGPT
  passent par OAuth (hors périmètre).
- **v1** : `auth.json` sous `$XDG_DATA_HOME/opencode/` (`packages/opencode/src/auth/index.ts:10`).
  **v2** : identifiants dans la **base SQLite** (`packages/core/src/credential.ts`) ; `auth.json`
  n'est plus lu ; la migration d'import ne s'exécute pas sur une base vierge
  (`database/migration.ts:29-46`). `opencode auth export` / `auth import` existent.
- **Priorité de la clé** : v1 `options.apiKey` de la config > identifiant stocké > env
  (`provider/provider.ts:1636-1660,1835` ; si le catalogue liste plusieurs variables, `key` reste
  `undefined`, l.1644-1647) ; v2 identifiant stocké > env > config (`core/src/integration.ts:409-423,
  739-747`, `model-resolver.ts:281`). **L'env ne gagne que sans identifiant stocké** : c'est le cas
  sous notre `XDG_DATA_HOME` isolé et vide. `XDG_CONFIG_HOME` n'est **pas** isolé : un
  `provider.<id>.options.apiKey` résiduel dans l'`opencode.json` du HOME du service écraserait la
  variable en v1.
- `{env:VAR}` / `{file:…}` dans `opencode.json` : variable absente ⇒ chaîne vide sans erreur.
  **Pour un fournisseur du catalogue, ne rien mettre dans `opencode.json`.**
- **`.env` n'est jamais lu par le binaire** (`autoloadDotenv: false`, v1 `script/build.ts:175`, v2
  `cli/script/build.ts:180`).
- Notre `opencode.json` actuel ne contient que `provider.local`, `permission.external_directory`
  (`opencode_setup.py:176,212-216`) et `share: manual` (défaut) : rien qui gêne. `permission` est
  global, indépendant du fournisseur ; en v2 natif une permission refusée rend un retour, ne
  suspend pas (`noninteractive.ts:72-73,167`).
- **`share`** ∈ `manual | auto | disabled` (`packages/schema/src/config.ts:42`) : contrôle l'envoi
  d'une session vers le service en ligne d'opencode. Notre provisioning ne la fixe pas.

### 2.2 Catalogue (models.dev)
v1 : fetch + cache `$XDG_CACHE_HOME/opencode/models.json` + snapshot embarqué ; v2 : cache dans la
base SQLite (vide à chaque run isolé) + **snapshot embarqué** + rafraîchissement en tâche de fond
(`core/src/plugin/models-dev.ts:40,69-80`). Un serveur sans sortie internet générale fonctionne
avec le catalogue de la version installée. **Le snapshot v2.2.5 contient** `ollama-cloud`
(`OLLAMA_API_KEY`), `mistral` (`MISTRAL_API_KEY`), `openrouter`, `opencode`, et les modèles
`mistral-large-4`, `zai-glm-5-3`, `zai-glm-5-3-flash` (VÉRIFIÉ) ; ses entrées fournisseur n'ont ni
`api` ni `npm` → **la table des URL reste nécessaire**. Conséquence : un modèle plus récent que le
snapshot peut être « Model unavailable » par intermittence tant que le rafraîchissement n'a pas
abouti → le doctor vérifie la présence du modèle **dans le catalogue d'opencode** (`opencode models`),
pas seulement chez le fournisseur.

| id | URL de base (appels directs) | variable | note |
|---|---|---|---|
| `ollama-cloud` | `https://ollama.com/v1` | `OLLAMA_API_KEY` | openai-compatible ; pas de paquet natif v2 |
| `mistral` | `https://api.mistral.ai/v1` | `MISTRAL_API_KEY` | natif v2 (`ai/src/providers/mistral.ts:31`) |
| `openrouter` | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` | natif v2 (`openrouter.ts:247`) ; routeur |
| `opencode` | `https://opencode.ai/inference/openai/v1` | `OPENCODE_API_KEY` | natif v2 (`opencode.ts:22`) |

En-tête : `Authorization: Bearer <apiKey>` (v2 `ai/src/providers/openai-compatible.ts:31`).

### 2.3 Sortie `--format json` et erreurs
Types `step_start`, `step_finish`, `text`, `tool_use`, `reasoning`, `error`. `step_finish` porte
`cost` et `tokens{input, output, reasoning, cache{read, write}}` (v1 `schema/src/v1/session.ts:240-256`,
v2 `cli/src/run/noninteractive.ts:501-511`). **Erreurs v2** : fournisseur sans connexion →
`ModelUnavailableError` « Model unavailable: <p>/<m> » (`core/src/session/runner/model.ts:30,90-97`)
mappé `provider.no-route` ; 401/403 → `provider.auth` (`to-session-error.ts:85-97`) ; émis
`{"type":"error","error":{type,message}}` + code de sortie 1 (`noninteractive.ts:522-546,698-700`).
**Notre parseur v1-only perd ce message** (« opencode exit 1: error »). Le serveur standalone v2 a
son **stderr ignoré** (`standalone.ts:29`, sauf `OPENCODE_PRINT_LOGS=1`). Retries v1 : ≤ 5 sur
429/5xx, `Retry-After` honoré jusqu'à ~24 j (`session/retry.ts:30-34,193`) → silence, puis notre
chien de garde.

### 2.4 `--standalone` (v2)
Serveur privé enfant avec `extendEnv: true` (`cli/src/services/standalone.ts:20-25`) : hérite de
`XDG_DATA_HOME` et des clés ; jamais le serveur partagé.

### 2.5 Le trou actuel (VÉRIFIÉ par sonde, opencode 2.2.5, ollama cloud)

| Données opencode | Résultat |
|---|---|
| par défaut | « OK » |
| répertoire isolé vide (= notre runner) | `Model unavailable` |
| isolé + copie de `auth.json` | `Model unavailable` |
| isolé + copie de `opencode.db` (807 Mo) | « OK » |
| isolé vide + `OLLAMA_API_KEY` dans l'environnement | « OK » |

**Aujourd'hui, TranscrIA ne peut utiliser aucun fournisseur connecté par `/connect`** ; la
variable d'environnement est la seule voie, sur les deux lignes, en headless, et sert aussi aux
appels directs.

### 2.6 Faits par hébergeur à afficher dans l'écran (recherche web 2026-11-01, sources secondaires)
- **Ollama cloud** : traitement transitoire, pas de conservation au-delà de la requête, pas
  d'entraînement (politique archivée mai 2026) ; hébergement principalement États-Unis, capacité
  Europe et Singapour ; modèles 128K à 1M.
- **Mistral (La Plateforme)** : hébergement UE par défaut ; **conservation 30 jours** sauf « zero
  data retention » (plans payants, appels sans état) ; opt-out d'entraînement = réglage séparé ;
  révision de politique signalée le 28/07/2026. Modèles : `mistral-large-4` (1M), Medium 3.5
  (256K), Small 4 (256K), **tiers hébergés** `zai-glm-5-3` (1M ; `-flash` signalé « Invalid model »
  par un ticket) ; GLM 5.2 retiré au 31/10/2026.
- **OpenRouter** : routeur ; ne conserve pas les prompts sauf journalisation activée ; `zdr: true`
  limite aux points sans rétention ; région UE = comptes entreprise ; le sous-traitant varie.
- **Le fournisseur n'est pas l'éditeur du modèle** : la gouvernance porte sur l'hébergeur ; les
  modèles changent et sont retirés en semaines → `model_id` vérifié par le doctor, jamais figé.

## 3. Décisions (arbitrées avec le mainteneur, 2026-11-01)

### 3.1 Que ça marche
1. **`services.backend: cloud`**, porté par **un prédicat unique `is_cloud_backend(config)`** dans
   `gpu/arbitrage_endpoint.py`, dont `is_remote_arbitrage` devient vrai ; ajouté aux deux tuples
   de valeurs reconnues, `is_ollama_backend` faux. Effets : aucun lancement, arrêt, réservation
   VRAM, préemption, pré-lancement ; `pipeline_admission` sans part LLM ; `_llm_admissible` vrai
   sans sonde ; `vram_reclaim` et `_release_arbitrage_llm` sortent en tête ;
   `ensure_arbitrage_llm_ready` ne compare pas `data[0]` ; les appelants de
   `resolve_arbitrage_endpoint` (≥ 7 : `vram_manager:57`, `opencode_runner` ×3, `correction:71`,
   `summary_llm:47`, `models_catalog:424`) et de `default_base_url` (`entrypoint`, `admin_routes`,
   `setup_opencode`) sont audités ; les logs n'annoncent plus « port 8080 ».
   **Redémarrage requis** après bascule (écrit dans l'écran et dans `UPGRADE.md`).
2. **Clé = variable d'environnement du fournisseur**, au nom du catalogue, dans le `.env` du
   service (sourcé par `start.sh`, relu par `app.py` et le doctor ; monté `./.env` en Docker ;
   héritée par opencode). Jamais dans `config.yaml`, `opencode.json`, un log, une sauvegarde
   lisible. L'assistant écrit la valeur **entre guillemets simples** (robuste au `source`).
3. **Les deux `model_id`** (`summary_llm`, `arbitration_llm`) passent à `<fournisseur>/<modèle>`
   ensemble ; le validateur **refuse un mélange** local/cloud. L'alias `arbitrage` reste réservé à
   `local`. Pour l'API directe, retirer **le premier segment seulement**.
4. **Table des fournisseurs en données** `transcria/data/llm_providers.yaml` (id → URL de base,
   variable, faits à afficher, lien conditions ; test de contrat). Dérogation = **l'existant**
   `workflow.arbitration_llm.api_base` (pas de nouvelle clé) + `workflow.llm_cloud.api_key_env`
   pour un fournisseur non listé.
5. **Appels directs** (`chat_completion`) : clé + URL de la table, `Authorization: Bearer`, premier
   segment retiré, **pas de `chat_template_kwargs`** en cloud, **pas de retry sur 401/403/429/5xx**,
   champ admin `workflow.llm_cloud.extra_request_fields` (dict fusionné dans le corps ; ex.
   `reasoning_effort: none` chez Ollama, qui **rejette** la requête sur un modèle sans
   raisonnement ; `zdr: true` OpenRouter). Contenu vide = échec lisible.
6. **Runner en cloud** : `_wait_llm_endpoint_ready` sautée ; `_llm_is_processing` et
   `_llm_gpus_active` rendent `None` ; **mise à mort par inactivité désactivée** (ni
   `idle_grace`, ni `first_contact`, ni `pure_idle`) : seul le plafond absolu `timeout_seconds`
   (7200 s) s'applique, aucun GPU local n'attend. **Uniquement en cloud** : le mode local garde
   son chien de garde intact.
7. **Panne du fournisseur / clé révoquée (cloud uniquement)** : `ensure` en échec → **une seule
   reprise automatique** par phase LLM (pas de `vram_wait` : le chemin `correction.py:119-122`
   est contourné par le prédicat cloud) puis **échec explicite et relançable**, avec le message du
   fournisseur dans le journal. Pour cela, **le parseur lit la forme d'erreur v2**
   (`error.type` / `error.message`) en plus de la v1, et `OPENCODE_PRINT_LOGS=1` est posé en cloud
   pour récupérer le stderr du serveur standalone.
8. **Verrou LLM** : no-op comme en distant ; la file reste bornée par `max_concurrent_jobs`.
9. **`share: disabled`** posé dans `opencode.json` par `ensure_local_provider` /
   `provision_opencode`, **pour les deux modes** (une session d'agent ne part jamais vers le
   service en ligne d'opencode).

### 3.2 Que ce soit dit
1. **Écran de configuration dédié** (`/admin/config` → « LLM cloud ») : fournisseur, modèle, faits
   de la table (§ 2.6) et liens vers les conditions, périmètre exact de ce qui part (transcription
   intégrale, brief d'invitation, documents joints extraits, lexique filtré, noms et fonctions des
   participants, contexte ; **jamais l'audio ni les voix**), phrase « la LLM peut éditer des
   fichiers dans un espace isolé, comme la LLM locale », mention explicite : **coûts, consommation
   et disponibilité du fournisseur relèvent de l'admin**, et « **la bascule prend effet au
   redémarrage du service** ; les jobs en file seront traités par le nouveau backend ».
2. **Validation admin en base**, table `llm_cloud_acknowledgements` (`provider, model, text_version,
   acknowledged_by, acknowledged_at, connectors_allowed`), **caducité réellement codée** : si
   `text_version` courante ≠ celle enregistrée, la validation est CADUQUE : **tous les traitements
   LLM sont refusés** (garde web à la soumission + rempart worker) et le doctor est en FAIL (lot C).
   **Réalisé (lot B) :** le service DÉMARRE quand même — un refus au démarrage empêcherait l'admin
   d'atteindre l'écran pour valider ; le bandeau permanent affiche « non validée : traitements LLM
   refusés ». Pas dans `config.yaml` (lecture seule en Docker, falsifiable sans trace). Écriture
   auditée (`config_llm_cloud_acknowledge` / `config_llm_cloud_revoke`).
3. **Case « autoriser aussi les réunions captées par connecteurs et bots »** (`connectors_allowed`,
   **décochée par défaut**) : décochée, `/v1/audio/ingest` est refusée en cloud avec un message
   clair dans les journaux du runner (personne ne peut être averti à ce moment-là).
4. **Bandeau permanent**, pas un avertissement ponctuel : sur l'accueil, la page d'upload (avant
   l'autostart du résumé) et le wizard : « cette instance confie les transcriptions à
   <fournisseur> (<hébergement>) ». Il couvre d'un seul endroit toutes les entrées : bouton
   « Traiter », autostart à l'upload (`wizard_api.py:165,180-213`), résumé manuel (`:303,325`),
   affinage (`refine_api.py:66`), synchronisation de synthèse de l'éditeur (`editor_routes.py:551`).
   Le banc E2E (`tests/test_e2e_workflow.py`) **imprime une ligne** « LLM d'arbitrage : <local |
   cloud fournisseur/modèle> » en tête de rapport.
5. **Interdiction par groupe** : colonne `llm_cloud_forbidden` sur `groups` (migration Alembic),
   case dans `/admin/groups` ; **un utilisateur est interdit dès qu'un de ses groupes l'est** ;
   refus à la soumission (toutes les entrées du point 4, message FR qui guide, jamais de repli
   silencieux sur une LLM locale) **et dernier rempart côté worker** (`job_executor._run_process`
   avant toute phase LLM : un job enfilé avant l'interdiction est refusé avec un état explicite).
   `profile_availability` grise les profils à LLM pour un utilisateur interdit. Limite connue : un
   **admin de groupe** peut retirer un membre d'un groupe interdit, ce qui lève l'interdiction pour
   ce membre (action auditée `group_member_remove`) — l'interdiction est une politique de GROUPE.
6. **Trace par job** : fichier de provenance `metadata/llm_provenance.json`
   (`{provider, model, hosting, external: true, phases: [{name, at, tokens?}]}` — tokens notés
   parce que gratuits, sans tableau de bord), écrit par `job_executor` **hors de la fenêtre
   `AgentWorkspace`** (après `verify_and_restore_sources`) ; miroir `extra_data.llm_provider` pour
   l'UI ; bandeau sur la page résultat ; mention au pied de page du DOCX (assertion dédiée) ;
   action d'audit `job_external_llm` (famille `job`, libellé `_VERB_FR`, via `AuditStore.log`,
   propriétaire dans `details`). Nouveau fichier runtime → `docs/DATA_MODEL.md`.
7. **Préparer « discuter avec mes réunions »** sans le coder : la table de validation porte une
   colonne `usages` (json, `{"pipeline": true}` aujourd'hui) pour y ajouter `ask_meetings` plus
   tard ; la provenance a un champ `phases[].name` libre ; `chat_completion` reste **l'unique**
   couture d'appel direct que ce chantier réutilisera ; l'interdiction par groupe s'appliquera
   telle quelle.

### 3.3 Que ça ne casse rien chez nous
1. **Doctor révisé** : nouveau check « LLM cloud » (variable présente, modèle présent dans le
   catalogue d'opencode ET chez le fournisseur via `GET {base}/models` Bearer, validation en base
   non caduque, `share: disabled`, aucun `options.apiKey` résiduel, pas d'identifiant stocké qui
   primerait). **Ce que prouve `GET /models` dépend du fournisseur** (champ `models_endpoint` de
   `llm_providers.yaml`, MESURÉ le 2026-11-01 avec une clé bidon) : Mistral exige la clé (401
   sinon) ⇒ un 200 vaut « clé acceptée » ; **Ollama cloud et OpenRouter répondent 200 à tout le
   monde** ⇒ la sonde ne prouve que la joignabilité et la présence du modèle, la clé n'est
   contrôlée qu'à l'inférence — le doctor écrit « clé présente mais NON vérifiée » et renvoie à
   `--llm-smoke` (appel facturé), jamais « clé valide » ; le fournisseur d'opencode n'a **pas** de
   liste (404) ⇒ aucune sonde, « ni clé ni modèle vérifiables avant le premier appel », et la
   disponibilité avant une phase (`CloudLLMBackend.is_available`) se réduit à « le serveur
   répond » (exiger un 200 rendait ce fournisseur inutilisable à chaque phase) ; `check_opencode_model_resolution` tolère un fournisseur du catalogue ; checks LLM
   locale (`check_arbitrage_llm`, `check_arbitrage_script`, `check_llm_placement_declaration`,
   GPU requis) **neutralisés en cloud** ; **`--llm-smoke` en cloud est un appel payant, dit
   explicitement**. La sonde `GET /models` n'est **jamais** appelée depuis une page web
   (`first_run.py` est servi à chaque accueil admin) : doctor et worker seulement. Golden du
   registre régénéré.
2. **Interface et provisioning** : bascule Ollama (`admin_routes.py:949`) et bascule GGUF
   (`/admin/models/activate`, l.898-924) refusées en cloud ; `models_catalog` ne propose pas de
   GGUF ; `first_run.py` : l'élément « modèles » ne compte pas le GGUF et le texte « sans GPU, les
   phases LLM ne le seront pas » (`_first_run_checklist.html:18`, `express.py:29,54`) est adapté ;
   `hardware_advisor.llm_tier_card` masquée ; textes `dashboard_status.html:13`, `mailer.py:279`,
   `_step_file.html:106`, `_step_launch.html:13` reformulés ; `timing_model` : les durées des
   phases LLM cloud sont enregistrées sous une clé distincte (`job_timing` n'a pas de dimension
   backend, fenêtre de 50 jobs).
3. **Docker et scripts** : `entrypoint.provision_arbitrage_model` et `provision_opencode`
   court-circuités en cloud (**pas de GGUF téléchargé**, `provider.local` non réécrit,
   `ensure_agent_permissions` conservé, `share: disabled` posé) — y compris `--provision-only`
   (`entrypoint.py:527-529`) ; `scripts/docker_quickstart.sh --cpu` (l.176-183) ne désactive plus la
   LLM quand `services.backend: cloud` ; `./.env` déjà monté sur chaque service.
4. **Split** : phases opencode et appels directs tournent dans le process qui exécute la file
   (`job_executor.py:201-215`, rôle `scheduler`/`all`) → **la clé est sur le worker** ; le rôle `web`
   n'a besoin que de la base (validation, interdiction par groupe, bandeau). Pas d'indicateur
   « LLM OK » dans l'interface en v1 : doctor et worker seulement. Doctor par rôle.
5. **Proxy d'entreprise** : `requests` honore `https_proxy` ; comportement de Bun/opencode
   vis-à-vis de `https_proxy`/`no_proxy` **NON VÉRIFIÉ** (précédent LiveKit :
   `connector_service/net_proxy.py`) → à tester au lot E ; `no_proxy` posé par `config_phase.py:215`
   doit continuer de couvrir 127.0.0.1.

### 3.4 Hors périmètre (dit à l'admin, pas codé)
Budget, alertes, tableau de bord de coût ; disjoncteur ; vérification de contexte (128K-1M, même
gamme que nos profils) ; contrôle d'appel d'outils (opencode gère) ; qualification des modèles
tiers au-delà de « lus et validés » ; STT/diarisation externes ; abonnements OAuth ; liste blanche
d'environnement (ticket séparé) ; rechargement à chaud de la config.

## 4. Palier « zéro GPU »

LLM cloud + STT CPU (Kroko, ou Whisper CPU) + diarisation Nemotron 3 CPU (`audiocpp_cli`
précompilé) = installation complète sans carte graphique. À prévoir : branche `cloud` dans
l'installateur et son mode express (pas de `llama-server`, pas de GGUF, pas de calibration VRAM,
pas d'erreur « aucun GPU ») ; **ajout de l'archive précompilée CPU d'`audiocpp_cli`** (déjà
épinglée et vérifiée sha256 pour l'installation native) **à l'image CPU Docker** (`Dockerfile`),
sinon le zéro GPU n'est complet qu'en natif ; entrypoint ; chapitre `INSTALL.md` / `DOCKER.md`
(fr et en). Le doctor cesse d'exiger un GPU pour la LLM.

## 5. Assistant d'installation

`transcria.installer.cli llm-cloud` (opt-in, `install.sh --llm-cloud`, jamais par défaut) : lit
`opencode auth export` (v2) ou `auth.json` (v1) sous le HOME du compte de service (**piège root vs
compte admin** : `systemd_lib.DEFAULT_SERVICE_HOME`, `User=`), propose les fournisseurs trouvés,
écrit la variable dans `.env` (`config/env_file.update_env_file`, 0600, chown service, valeur
quotée), **jamais la valeur à l'écran** ; ou `--provider <id> --key-env <VAR>` ; règle les deux
`model_id` et `services.backend: cloud` ; **n'écrit pas la validation** (elle se fait dans l'écran,
en base, par un admin connecté) mais le dit. Messages fr/en, test de phase à runner injecté.
Compte des phases mis à jour dans AGENTS.md.

## 6. Lots

| Lot | Contenu | Preuve |
|---|---|---|
| A — que ça marche — **livré** (5729b19) | `is_cloud_backend` + tuples + `CloudLLMBackend` (base_url, `auth_headers()`, en-têtes dans `_http_get_json*`), `llm_providers.yaml` + contrat, défauts `loader.py` + vue typée + classification, validateur (mélange refusé), tous les sites du § 7.2, runner (§ 3.1.6-7, parseur v2, `OPENCODE_PRINT_LOGS`), `chat_completion` (§ 3.1.5), `share: disabled` | tests backend (`cloud` gagne malgré `ollama_url`/`arbitrage_script` résiduels), admission sans GPU, `test_refine_llm` cas cloud, faux serveur `integration` (401/429/5xx, modèle inconnu), `test_opencode_runner` (erreur v2 lisible, pas de kill par inactivité), goldens schéma + contrat des vues régénérés |
| B — que ce soit dit — **livré** (9266dc9) | table de validation + caducité, colonne groupe + migration, écran, case connecteurs, bandeau permanent, refus à la soumission (5 entrées) + rempart worker, `profile_availability`, provenance + bandeau résultat + pied DOCX + audit, ligne E2E | Playwright gestes réels + revue visuelle ; tests aux limites ; DOCX local inchangé ; `DATA_MODEL.md`, `API_REFERENCE.md` |
| C — que ça ne casse rien — **livré** (e94fdfa) | doctor révisé (§ 3.3.1), gardes UI/provisioning (§ 3.3.2), Docker/scripts (§ 3.3.3), `timing_model` | golden registre doctor ; tests par rôle ; `docker compose config` |
| D — zéro GPU + install + docs — **livré** (4b70384) | branche installateur/express, `audiocpp_cli` dans l'image CPU, assistant `llm-cloud`, INSTALL/DOCKER (fr+en), CONFIG_REFERENCE (chaque clé feuille), LLM_BACKENDS, SECURITY_MODEL, AUDIT_DPO, UPGRADE, **`docs/README.md` + AGENTS.md** (index documentaire exigé par `release_check`) ; i18n **6 langues** (fr/en/de/es/it/pt) | `i18n_check`, `release_check`, gate d'installation vierge avec `--llm-cloud`, build de l'image CPU (règle C7) |
| E — qualification réelle — **à faire** | E2E complet 17/17 avec **Mistral** (`mistral-large-4` ou `zai-glm-5-3`, UE) puis **Ollama cloud** ; échecs : clé révoquée, modèle retiré, fournisseur injoignable, groupe interdit, connecteur refusé ; proxy ; relecture humaine du DOCX ; réseau coupé + `XDG_DATA_HOME` vide + une variable d'env (catalogue embarqué) ; rotation des clés de banc après coup. **Fait le 2026-11-01 (avant le banc)** : mesure de `GET /models` par fournisseur avec une clé bidon → table `models_endpoint`, verdict doctor honnête, disponibilité d'un fournisseur sans liste ; **les clés du banc se lisent dans le magasin opencode de l'opérateur** (`installer.cli llm-cloud --user-home`), jamais collées dans un échange | journal dans ce document |

Ratchets et gardes : `deep_config_chains` 196 (nouvelles lectures `llm_cloud.*` **par vue typée**,
`tests/contracts/test_config_views_contract.py` à régénérer) ; `test_config_classification`
(toute clé par défaut classée) ; `functions_over_150` compte des fonctions (10 : `OpenCodeRunner.run`
167 l., `phases/refine.py::run` 203 l. — extraire, ne pas rallonger) ; fan-out `vram_manager` 14,
`allocator` 7, `opencode_runner` 6, `admin_routes` 33, `entrypoint` 8 ; `.importlinter`
`noyau-sans-orchestration` (la politique dans `config/`/`gpu/`/`auth/` n'importe pas
`workflow`/`queue`/`services`), `web-pas-une-dependance`.

## 7. Inventaire de notre code (vérifié et recalé 2026-11-01)

### 7.1 Suppositions `local/` / alias `arbitrage`
`refine_llm.py:145-148` ; `deploy/entrypoint.py:204-241` et `scripts/setup_opencode.py:50-51` ;
`admin_routes.py:898-924` (GGUF) et `949-950` (Ollama) ; `config_form.py:92` ; doctor
`checks/llm.py:226-284` ; `arbitrage_api_model_id` lu à 6 sites (`summary_llm`, `correction:70`,
`final_review:174,368`, `refine:242`, `multi_stt_review:143`) ; tests épinglant `local/arbitrage` ou
les prédicats : `test_llm_backend_process_paths`, `test_llm_backend_lifecycle`, `test_refine_llm`,
`test_opencode_cli`, `test_opencode_runner`, `test_opencode_setup`, `test_wizard_llm_prelaunch`,
`test_doctor`.

### 7.2 Hôte:port / cycle de vie local
`vram_manager.py` : `resolve_arbitrage_endpoint` 57 ; `recalibrate…` 101 ; `is_arbitrage_llm_running`
234-253 (`http://` en dur) ; `launch_arbitrage_llm` 379 ; `_is_remote_arbitrage` 489 ;
`ensure_arbitrage_llm_ready` 493-571 ; `stop_arbitrage_llm` 605 ; `_preflight_llm_vram`,
`stop_cleanup_llm_ports`, `_kill_port`. `_port_utils.is_port_open` 61-85. `allocator.py:69` (figé à
l'init), 274, 286, 419. `gpu_phase.py:104-110` ; `pipeline_admission.py:34-46` ;
`scheduler.py:377-398`, ~400 ; `vram_reclaim.py:37-45` ; `llm_prelaunch.py:33` ;
`pipeline_service.py:385-413` ; `stt_instance_planner.py:96` ; `models_catalog.py:203-215,424` ;
`correction.py:69-71,111-123` ; `summary_llm.py:46-47,97,118` ; `hardware_advisor.py:103-124` ;
`first_run.py:53-62` ; `profile_availability.py:37-38,78` ; `timing_store.py:20-25` ; doctor
`checks/llm.py:138-172`, `probes.py:60-64`.

### 7.3 Runner
`opencode_runner.py` : `_llm_is_processing` 244 ; `_llm_gpus_active` 300 ; `_wait_llm_endpoint_ready`
395 ; plafonds 440-455 ; `_communicate_with_watchdog` 490-510 ; `run()` 532-698 ; parsing 622-654 ;
retries `correction.py:35,354-440`, `summary_llm.py:35,234-266`. `opencode_cli.build_run_env`.

### 7.4 Entrées, config, audit, exports, installateur, Docker
Entrées LLM : `processing_api.api_process`, `wizard_api.py:165,180-213,303,325`, `refine_api.py:66`,
`editor_routes.py:551`, `facade_api.py:406,531,538,621`. Config : `loader.py` (`services` 113-120,
`workflow` ~705-740, `vram_wait` 677, locales 956) ; `config_schema.py:91-119` ;
`checks/platform.py:137-155` ; `checks/orchestration.py:37-76,88-109` ; `checks/auth.py:42-48` ;
`config_classification.yaml:355` ; `tests/golden_config_schema.json` ; `config_form.py:79-98` +
`SECRET_SENTINEL` ; `app.py:4-6` ; `start.sh:63-67` ; `doctor.py:100-112`. Audit : `audit/models.py`
(`AuditAction`, `external_push`), `audit/store.py:18`, `audit/decorator.py:14-48`, `jobs/store.py:130`.
DOCX : `docx_report.py:1061-1115` (pied), `1136-1181`. Page résultat `pages_routes.py:763-800`.
Voix (patron) : `voice/models.py:78`, `voice/routes.py:48,194`. Groupes : `auth/models.py:130-150`,
`auth/groups.py`. Workspace : `agent_workspace.py:46,199-210`. Installateur : `installer/cli.py`
(`_add_opencode_parser` 118-130), `messages.py:15`, `systemd_lib.py:15,154-195`,
`env_file.update_env_file`, `express.py:29,54`, `config_phase.py:215`. Docker : `entrypoint.py`
200, 204-241, 229-235, 333-415, 527-529, 550 ; `docker-compose.yml:27,72,94,114,155` (config et
`.env` montés, config en lecture seule) ; `scripts/docker_quickstart.sh:176-183` ;
`scripts/switch_arbitrage_llm.sh:47`.

## 8. Questions ouvertes
- ~~Le fournisseur d'opencode et OpenRouter exposent-ils `GET /v1/models` avec la même clé ?~~
  **Répondu le 2026-11-01 (sondes réelles, clé bidon)** : OpenRouter `GET /api/v1/models` → 200
  sans clé comme avec une clé bidon (liste publique) ; opencode
  `GET /inference/openai/v1/models` → 404 (pas de liste) ; Ollama cloud `GET /v1/models` → 200
  avec une clé bidon, `/v1/chat/completions` → 401 (la clé n'est contrôlée qu'à l'inférence) ;
  Mistral `GET /v1/models` → 401 sans clé valide. Porté dans `llm_providers.yaml`
  (`models_endpoint`), le doctor et `CloudLLMBackend.is_available` (§ 3.3.1).
- Bun/opencode et le proxy d'entreprise (`https_proxy`, `no_proxy`) : à tester au lot E.
- Le binaire opencode peut-il lister les URL de base (→ table réduite aux faits à afficher) ?
