# Pistes d'amélioration — analyse transversale post-0.3.7

> **Pourquoi ce document.** Toutes les fonctionnalités du produit sont implantées et
> testées (campagne qualité A0→C8, v0.3.7). Ce document fait ce que la campagne n'a
> pas fait : une analyse **à froid** des choix par défaut — vitesse, moteurs, gestion
> des ressources, parcours utilisateur, exploitation — pour identifier où un effort
> ciblé rapporterait le plus. Chaque piste est présentée avec son état actuel
> (références code vérifiées), le gain attendu, les contreparties (« oui, mais… »),
> une estimation d'effort et une priorité.
>
> **Ce document ne décide rien** : c'est une base de discussion pour choisir les
> prochains chantiers. Les références `fichier:ligne` correspondent à la v0.3.7
> (commit `9ef95a6`).

Échelles utilisées :
- **Effort** — S : < 1 jour ; M : 1 à 3 jours ; L : chantier (> 3 jours).
- **Priorité** — P1 : gain majeur ou irritant utilisateur direct ; P2 : gain net,
  non urgent ; P3 : opportuniste.

## Principes d'implémentation (opposables à chaque lot)

Toute piste retenue de ce document s'implémente sous six règles non négociables :

1. **Paramétrable, défaut inchangé.** Chaque changement de comportement vit
   derrière une clé `config.yaml` dont le **défaut reproduit le comportement
   actuel** ; la clé est validée par le schéma et documentée dans
   `CONFIG_REFERENCE.md` (garde de couverture existante). Un défaut ne bascule
   vers le nouveau comportement qu'à une version ultérieure, après validation —
   jamais dans le lot qui l'introduit.
2. **L'ossature ne bouge pas.** Les pistes réutilisent les coutures existantes
   (allocateur, profils, reprise par phases, `LLMBackend`, registre STT) ; aucune
   nouvelle couche, aucun contournement d'abstraction. Les ratchets CI du chantier
   qualité (cycles, imports différés, fan-out, fonctions géantes) en sont les
   garants mécaniques.
3. **Amélioration prouvée, pas supposée.** Mesure avant/après pour toute
   optimisation (d'où l'instrumentation en tête du lot 1) ; banc de validation LLM
   pour tout ce qui touche la qualité de sortie. Ce qui n'est pas mesurable reste
   opt-in.
4. **Prévu dans l'installation.** Toute nouvelle clé entre dans la génération de
   config de l'installeur ; tout nouveau composant a sa phase de provisionnement
   et son check `doctor` ; `test_install_script`/`test_install_e2e` couvrent le
   chaînage.
5. **Code maintenable.** Mêmes exigences que la campagne 0.3.7 : couverture
   ≥ 80 %, ratchet de docstrings, docs synchronisées par contrôles exécutables,
   suite complète + E2E réel verts avant tout push.
6. **Gérable par l'utilisateur.** `config.yaml` pour l'opérateur ; exposition dans
   l'UI d'administration quand c'est une décision d'exploitation (backend du
   résumé, seuils d'attente…) ; i18n FR/EN pour toute nouvelle chaîne visible.

Sommaire : [0. Résumé exécutif](#0-résumé-exécutif) ·
[1. Données de référence](#1-méthode-et-données-de-référence) ·
[2. Vitesse du pipeline](#2-vitesse-du-pipeline) ·
[3. Repli CPU](#3-repli-cpu-quand-la-vram-manque--analyse-des--oui-mais-) ·
[4. Choix des moteurs](#4-choix-des-moteurs) ·
[5. Parcours utilisateur](#5-parcours-utilisateur-et-exports) ·
[6. Système et maintenance](#6-système-et-maintenance) ·
[7. Pistes écartées](#7-pistes-envisagées-et-écartées) ·
[8. Feuille de route](#8-feuille-de-route-suggérée)

---

## 0. Résumé exécutif

Les mesures réelles (§1) montrent que, sur cette machine (2× RTX 5090), **le temps
d'un job est dominé par les passes LLM, pas par le STT** : pour 2 minutes d'audio,
la correction LLM prend 169 s et la relecture finale 83 s, contre 21 s de
transcription. Les pistes les plus rentables ne sont donc pas celles qu'on
imaginerait spontanément :

| # | Piste | Gain attendu | Effort | Priorité |
|---|---|---|---|---|
| 2.1 | Backend STT « éclair » dédié à la phase résumé (nemotron/parakeet.cpp ou kroko CPU) | −30 à −60 s par job sur la phase wizard la plus visible | M | **P1** |
| 2.3 | Garder le LLM d'arbitrage chaud entre les phases d'un même job (et entre jobs rapprochés) | −17 s × nombre de démarrages (2 à 4 par job) | M | **P1** |
| 5.4 | Montrer à l'utilisateur sa position dans la file et l'estimation d'attente (le calcul existe déjà, il n'est affiché qu'aux admins) | irritant n°1 de l'attente résolu à coût faible | S | **P1** |
| 6.1 | Sous-commandes `backup --db-only` / `--files-only` + restauration sélective | sauvegardes quotidiennes rapides de la base seule | S | **P1** |
| 5.1 | DOCX à la demande pour les profils SRT (après édition dans l'éditeur) | débloque un parcours utilisateur aujourd'hui en 404 | M | **P2** |
| 3 | Politique de repli CPU encadrée (résumé seulement, opt-in) | jobs courts traités pendant les pics GPU | M/L | **P2** |
| 2.5–2.7 | Dé-doublonner le préflight, mutualiser le décodage 16 kHz, instrumenter le bloc preprocess | −10 à −30 s par job + visibilité sur les coûts réels | M | **P2** |
| 6.2 | Purge planifiée (timer systemd) + garde-fou de quota disque | plus de croissance silencieuse de `jobs/` | S | **P2** |
| 4.3 | Politique de cycle de vie LLM par backend (keep-warm agressif en vLLM) + réutilisation de préfixe de prompt | indispensable avant tout déploiement vLLM local ; TTFT réduit passes 2-3 | M | **P2** |
| 6.6 | `hf_transfer` + téléchargements modèles parallèles à l'installation | installation nue ×3-5 plus rapide sur le poste modèles | S | **P2** |
| 2.4 | Réduire le coût des passes LLM (prompts, lots, paliers de modèles) | le poste n°1 du pipeline, mais le plus risqué qualité | L | **P3** |

Le détail, les contreparties et les pistes écartées suivent.

---

## 1. Méthode et données de référence

### 1.1 Mesures réelles (E2E v0.3.7, 2× RTX 5090, audio de 2 min, 29 segments, 2 locuteurs)

Source : exécution `tests/test_e2e_workflow.py --audio tests/test2.mp3` du 2026-07-16
(celle qui a validé la release).

| Poste | Durée | Part |
|---|---:|---:|
| **Total wizard + pipeline** | 393 s | 100 % |
| Phase résumé (STT rapide + scène + pyannote + LLM) | 104 s | 26 % |
| Pipeline complet | 283 s | 72 % |
| — dont correction LLM | 169 s | 43 % |
| — dont relecture finale LLM | 83 s | 21 % |
| — dont transcription (cohere, 29 chunks) | 21 s | 5 % |
| — dont diarisation (checkpoint réutilisé) | 0,9 s | < 1 % |
| Démarrage du LLM d'arbitrage (llama.cpp, port 8080) | 17 s **× 2 dans ce job** | 9 % |

Deux conclusions structurantes :
1. **Correction + relecture finale = 64 % du temps total à elles seules** ; en
   ajoutant la part LLM de la phase résumé (~60-80 s), les passes LLM dépassent
   les trois quarts du temps d'un job court. Toute optimisation STT/audio ne
   touche que le reste.
2. Le LLM d'arbitrage a été **démarré deux fois dans le même job** (une fois pour le
   résumé, une fois pour la correction — arrêté entre les deux), soit 34 s de pur
   démarrage, 9 % du total.

Ces proportions varient avec la durée de l'audio : sur une réunion d'une heure, la
part STT monte (le LLM travaille sur le texte, quasi indépendant de la durée), mais
le démarrage LLM et la phase résumé restent des coûts quasi fixes par job.

### 1.2 Vitesses des moteurs STT (benchmark interne sur réunions réelles)

Source : `docs/STT_BENCHMARK_REAL_MEETINGS.md` (fenêtres de 5 min, français
bande étroite, WER vs verbatim professionnel).

| Moteur | WER | Temps / fenêtre de 5 min | Empreinte |
|---|---:|---:|---|
| cohere (défaut production) | 0.46 | 84 s | GPU 6 Go |
| whisper large-v3 | 0.44 | 112 s | GPU 10 Go |
| voxtral Mini 3B | 0.43 | 130 s | GPU 11 Go |
| MOSS-Transcribe-Diarize 0.9B | **0.41** | 65–104 s | GPU 4 Go, **ASR+locuteurs en une passe** |
| **Qwen3-ASR-1.7B (audio.cpp)** | **0.42** | **10–14 s** | GPU ~4 Go |
| **Nemotron 3.5 ASR 0.6B (audio.cpp)** | 0.49 | **~2 s** | GPU ~1,4 Go |
| Nemotron 3.5 ASR 0.6B (parakeet.cpp) | 0.49 | 7–8 s | GPU ~1,4 Go |
| **Kroko-ASR FR (sherpa-onnx)** | **0.43** | **10 s** | **CPU pur, 155 Mo** |

Lecture : il existe des moteurs **6 à 40 fois plus rapides que le défaut**, à
qualité comparable (Qwen3-ASR 1.7B, Kroko) voire légèrement inférieure mais
suffisante pour un aperçu (Nemotron). C'est le socle factuel des pistes 2.1 et 3.

---

## 2. Vitesse du pipeline

### 2.1 Un backend STT « éclair » pour la phase résumé — **P1, effort M**

**État actuel.** La phase résumé transcrit l'audio pour alimenter le LLM
(participants, contexte, lexique) — c'est nécessaire. Mais elle utilise **le même
backend que le pipeline principal** (`models.stt_backend`, défaut cohere) :
`workflow/phases/summary.py:46`, `stt/summary.py:70`. Le transcriber est chargé puis
déchargé du GPU à chaque job (`stt/summary.py:70,88`). Il n'existe **aucune clé de
type `summary_stt_backend`** (vérifié). Résultat : la phase la plus visible du
wizard (l'utilisateur attend devant son écran) paie le prix du moteur de qualité
finale, alors que sa sortie ne sert qu'à pré-remplir des champs et à diagnostiquer
la difficulté de l'audio (`stt/summary.py:130-164`).

**Piste.** Introduire `models.summary_stt_backend` (défaut : `null` = comportement
actuel) permettant de dédier un moteur rapide au résumé :
- **Nemotron via parakeet.cpp** (déjà intégré, runtime servi, ~2-8 s par 5 min,
  1,4 Go) — le candidat vitesse pure ;
- **Qwen3-ASR-1.7B via audio.cpp** (déjà intégré, 10-14 s par 5 min, WER 0.42
  meilleur que cohere) — le candidat vitesse + qualité ;
- **Kroko** (déjà intégré, CPU pur, 10 s par 5 min, WER 0.43) — le candidat
  « zéro VRAM » : la phase résumé ne réserverait plus de GPU du tout
  (`kroko_transcriber.py:95` : `vram_mb = 0`). Le modèle français pèse 155 Mo
  (le dépôt `Banafo/Kroko-ASR` complet, multi-langues, ~3,2 Go — ne télécharger
  que la langue utile).

**Gain attendu.** Sur l'E2E de référence, le STT rapide + son chargement pèsent
l'essentiel des ~20 premières secondes de la phase résumé ; sur une réunion d'une
heure avec cohere, la transcription du résumé se chiffre en minutes. Avec Kroko, le
gain est double : temps ET libération de la contention VRAM (le résumé n'entre plus
en concurrence avec les jobs du pipeline — plus de « reclaim » du LLM d'arbitrage
par le STT du résumé, `summary_stt.py:106-108`).

**Oui, mais…**
- *La qualité du résumé LLM dépend du texte fourni.* Un WER légèrement supérieur
  (Nemotron 0.49) peut dégrader les suggestions de participants/lexique. Contre-mesure :
  Qwen3-ASR ou Kroko (WER ≤ cohere) plutôt que Nemotron ; ou Nemotron seulement
  au-delà d'une durée seuil.
- *Le diagnostic de difficulté* (`_build_diagnostics`, `stt/summary.py:130-164`)
  alimente `should_force_quality_backend_for_degraded_summary`
  (`pipeline_config.py:185-215`). Il faut vérifier que ses seuils (hallucinations,
  segments courts) restent pertinents sur la sortie du moteur rapide — sinon le
  mécanisme d'escalade qualité se déclenchera à tort ou à travers.
- *Les runtimes servis (audio.cpp / parakeet.cpp) sont opt-in à l'installation*
  (`installer/audiocpp_phase.py:14-16`) : en faire le défaut du résumé imposerait
  de les embarquer partout (fait pour l'image bundled, pas pour une install nue).
  Kroko n'a pas cette contrainte (paquet Python + modèle 155 Mo).
- *Chunking différent* : le STT du résumé chunk par VAD Silero ou 30 s fixe
  (`stt/summary.py:60-64`), pas par tours pyannote. Aucun impact : c'est déjà le cas.

**Verdict.** La variante **Kroko par défaut pour le résumé** est la plus séduisante :
déjà intégrée, zéro VRAM, qualité équivalente au défaut actuel, aucun prérequis
d'installation. Les runtimes C++ restent l'option « turbo » documentée.

### 2.2 Réutiliser la transcription du résumé dans le pipeline principal — **écartée en l'état** (voir §7)

**État actuel.** L'audio est transcrit deux fois par job : STT rapide du résumé
(chunks VAD/30 s), puis STT du pipeline (chunks alignés sur les tours pyannote,
`stt/transcription.py:126-167`). La diarisation, elle, **n'est PAS refaite** : la
phase résumé écrit un checkpoint que le pipeline réutilise
(`stt/diarization.py:138-141` ; mesuré : 0,9 s au lieu d'une pleine passe).

**Pourquoi c'est écarté.** Les deux transcriptions ne produisent pas le même
artefact : celle du pipeline est découpée par tours de parole exclusifs, condition
de l'attribution fiable des locuteurs au SRT. Réutiliser les segments VAD du résumé
imposerait une re-projection segments→tours dont les cas limites (chevauchements,
segments à cheval sur deux tours) sont précisément ce que le découpage par tours
évite. Le vrai levier est 2.1 (rendre la première passe quasi gratuite), pas la
fusion des deux passes.

### 2.3 Cycle de vie du LLM d'arbitrage : le garder chaud — **P1, effort M**

**État actuel.** `ensure_arbitrage_llm_ready` (`gpu/vram_manager.py:515-625`) gère
trois cas : réutilisation (A), redémarrage pour changement de modèle (B), lancement
à froid (C). Chaque lancement coûte **17 s mesurées** (attente du port 8080). Dans
un même job, le LLM peut être **arrêté puis relancé** : le STT du résumé peut
stopper le LLM inactif pour récupérer sa VRAM (`summary_stt.py:106-108`,
`gpu_phase.py:141-148`), puis la phase LLM du résumé le relance ; en fin de
pipeline, il est arrêté systématiquement (`pipeline_service.py`, arrêt sous verrou
depuis B3). L'E2E de référence montre 2 démarrages par job ; avec reclaim, on peut
monter à 3-4.

**Piste.** Deux mesures complémentaires :
1. **Ne plus arrêter le LLM en fin de pipeline si la file n'est pas vide** (regarder
   `QueueStore` avant l'arrêt) : les jobs enchaînés réutilisent l'instance (CAS A).
   L'arrêt inconditionnel actuel est une politique de restitution de VRAM qui n'a de
   sens que si personne n'attend.
2. **Éviter le cycle stop-relance intra-job** : si le backend STT du résumé tient
   dans la VRAM restante (ou est CPU, cf. 2.1), le reclaim ne se déclenche pas. La
   piste 2.1 avec Kroko fait disparaître ce cas mécaniquement.

**Oui, mais…**
- *Un LLM qui reste chaud occupe 14,7 Go (config actuelle) en permanence.* Sur une
  machine partagée avec d'autres usages GPU, c'est un choix d'exploitation — d'où :
  ne pas arrêter *si la file est non vide*, plutôt que *jamais*.
- *La course arrêt-vs-lancement corrigée en B3* (verrou `__pipeline_stop__`) doit
  être préservée : la décision « j'arrête ou pas » doit se prendre sous le même
  verrou, sinon on réintroduit la fenêtre de course.
- *Cas B (mauvais modèle)* : si des jobs alternent des profils LLM différents, le
  keep-warm ne sert à rien (redémarrage forcé). En pratique un seul modèle
  d'arbitrage est configuré.

### 2.4 Le poste n°1 : les passes LLM elles-mêmes — **P3, effort L**

**État actuel.** Correction (169 s) + relecture finale (83 s) + résumé LLM
(~60-80 s de la phase résumé) : les trois passes opencode dominent tout le reste.
Chaque passe est un agent opencode complet (31 outils appelés pour la correction sur
l'E2E de référence).

**Pistes envisageables** (à instruire séparément, gros chantier) :
- prompts plus directifs pour réduire le nombre d'allers-retours outils de l'agent ;
- fusionner correction + relecture finale en une passe pour les profils
  intermédiaires (`word_rapide`) ;
- paliers de modèles (`docs/LLM_TIERS.md` existe déjà) : un modèle plus petit pour
  le résumé (tâche d'extraction), le gros modèle réservé à la correction.

**Oui, mais…** C'est le poste où le risque qualité est maximal : la correction LLM
est la valeur ajoutée visible du produit (fromager/vendeur, lexique métier). Toute
économie ici doit passer par le banc de validation LLM avant d'être un défaut.
À traiter comme un chantier de fond avec métriques, pas comme une optimisation.

### 2.5 Le préflight audio est exécuté deux fois — **P2, effort S**

**État actuel.** `AudioPreflightAnalyzer.analyze()` tourne une première fois à la
phase « analyze » du wizard (`services/job_service.py:69-73`, résultat écrit dans
`metadata/audio_preflight.json`) puis une **seconde fois** dans le bloc preprocess
du pipeline (`pipeline_steps/preflight.py:32`) — sans relire le JSON déjà écrit.
Chaque exécution refait décodage complet + SQUIM + DNSMOS + métriques acoustiques.

**Piste.** Dans l'étape pipeline, recharger `audio_preflight.json` s'il existe et
que l'audio n'a pas changé (l'empreinte du fichier est déjà le mécanisme utilisé
par le checkpoint de diarisation, `stt/diarization.py:138`). Recalcul seulement si
un prétraitement (normalisation, débruitage) a produit un nouveau fichier d'entrée.

**Oui, mais…** Si un prétraitement modifie l'audio entre les deux appels, le
second préflight est légitime (il mesure l'audio réellement transcrit). La garde
« même empreinte de fichier » règle exactement ce cas.

### 2.6 L'audio est décodé et rééchantillonné 5 à 8 fois par job — **P2, effort M**

**État actuel.** Tout le monde vise 16 kHz mono, mais chacun décode pour soi :
préflight ×2, worker de scène (`_scene_analysis_worker.py:442`), VAD
(`audio/vad.py:31`), STT résumé (`stt/summary.py:41`), transcription
(`stt/transcription.py:130`), diarisation (`stt/base_diarizer.py:120-126`) — plus
les resamples internes indépendants de SQUIM et DNSMOS. Un convertisseur canonique
existe (`audio/converter.py:18-26`, WAV mono 16 kHz) mais n'est utilisé que par le
transcripteur distant (`stt/remote_transcriber.py:161`).

**Piste.** Produire une fois pour toutes `input/audio_16k.wav` au début du
preprocess (ou à l'upload, en tâche de fond) et faire pointer les consommateurs
dessus. Le décodage ffmpeg d'un MP3 d'une heure vers WAV 16 kHz se compte en
secondes ; les décodages répétés par librosa (resampling python) sont plus lents et
mobilisent le CPU en plein pipeline.

**Oui, mais…**
- *Espace disque* : le WAV 16 kHz mono 16 bits pèse ~115 Mo/heure — marginal
  face aux poids modèles, mais à purger avec le job (mécanisme `purge_input_files`
  existant, `jobs/artifact_store.py:479-489`).
- *La diarisation veut parfois le natif* : pyannote travaille correctement à
  16 kHz ; vérifier que les extraits locuteurs (clips) restent taillés dans
  l'original pour l'écoute.
- *Prudence sur les prétraitements* : si normalisation/débruitage sont actifs, la
  chaîne aval doit consommer le fichier traité, pas le canonique initial — la règle
  « le dernier fichier produit fait foi » existe déjà dans le bloc preprocess.

### 2.7 Le bloc preprocess est séquentiel et non instrumenté — **P2, effort S→M**

**État actuel.** Les 7 étapes du preprocess s'exécutent strictement en série
(`pipeline_service.py:194-203`) et **aucune n'est enregistrée** dans
`StageMetrics`/`JobTimingStore` (seuls `transcribe` et les étapes aval le sont,
`pipeline_service.py:233-234,291-292`). Les logs `duree=` existent pour certaines
étapes seulement. Conséquence : impossible de savoir sur le parc réel combien
coûtent préflight, scène, normalisation — et donc de prioriser leurs optimisations.

**Piste (deux temps).**
1. *Instrumenter* : enregistrer chaque étape preprocess dans `JobTimingStore`
   (effort S, aucune contrepartie) — c'est le préalable à toute décision chiffrée.
2. *Paralléliser ce qui est indépendant* : préflight (CPU/GPU léger) et analyse de
   scène (subprocess CPU) n'ont aucune dépendance mutuelle ; les lancer de front
   économise le plus court des deux. Les étapes qui réécrivent l'audio
   (séparation → filtre → débruitage → normalisation) restent en série par nature.

**Oui, mais…** Le worker de scène est déjà un subprocess isolé
(`scene_analyzer.py:117-129`) : le paralléliser avec le préflight ajoute peu de
complexité, mais tout parallélisme dans le pipeline doit rester compatible avec la
reprise sur checkpoint (le bloc entier est un seul checkpoint `preprocess`,
`pipeline_service.py:184,203` — c'est compatible : on ne change pas la frontière).

### 2.8 Divers vitesse (faible priorité)

- **Demucs rechargé à chaque appel** (`audio/source_separation.py:360`, pas de
  singleton) — sans impact tant que la séparation est désactivée par défaut
  (`config.yaml:454`). À corriger si elle devient un défaut. Effort S.
- **Le probe `available` de l'analyse de scène lance un subprocess `import librosa`
  à chaque job** (`scene_analyzer.py:74-83`) : mémoïsable au niveau process. Effort S.
- **`librosa.yin` par segment de parole** pour le genre
  (`_scene_analysis_worker.py:380-417`) : coût linéaire au nombre de segments ;
  acceptable aujourd'hui, à surveiller sur les réunions très hachées.

### 2.9 La transcription locale est strictement séquentielle — **P3, effort M/L**

**État actuel.** Le pipeline transcrit les tours **un par un** : la concurrence de
chunks existe (`stt/transcription.py:631-651`, `ThreadPoolExecutor` `:746`) mais ne
s'active que si le backend est `concurrent_safe` — c'est-à-dire **distant/servi**
— ET que `inference.stt.concurrency > 1` (`transcription.py:717-724`). Tous les
backends locaux en-process tournent à `workers=1` (visible dans chaque log de job :
`backend=cohere workers=1`). Sur une machine à 2 GPU, un seul travaille pendant la
transcription.

**Pistes.**
- *Voie déjà câblée* : servir le STT localement via audio.cpp/parakeet.cpp
  (loopback) et monter `inference.stt.concurrency` — la concurrence de chunks
  s'active sans une ligne de code pipeline. C'est un argument de plus pour les
  runtimes C++ (cf. 2.1, 4.1).
- *Voie lourde* : deux instances du modèle local sur les deux cartes et
  distribution des chunks — double la VRAM STT réservée, complexifie l'admission ;
  à n'envisager que si les réunions longues deviennent le cas dominant.

**Oui, mais…** Le gain ne se matérialise que sur les audios longs (beaucoup de
tours) ; sur l'E2E de référence le STT ne pèse que 21 s. À chiffrer avec
l'instrumentation §2.7 avant d'investir la voie lourde ; la voie servie, elle, est
quasi gratuite à essayer.

---

## 3. Repli CPU quand la VRAM manque — analyse des « oui, mais »

**État actuel.** Quand une phase ne peut pas réserver sa VRAM, le job est replanifié
toutes les 30 s (`job_executor.py:239-253`), **sans borne de durée** (contrairement
au mode distant, borné par `max_unavailable_s=600 s`,
`inference/resource_status.py:64-73`). L'utilisateur voit un message statique
(« reprendra automatiquement dès que la mémoire GPU sera libérée »,
`wizard_api.py:55-58`) sans position de file ni estimation. Un e-mail part vers les
admins au premier passage (`job_executor.py:252-253`).

**Le CPU est déjà largement présent dans le code** — c'est le fait central :

| Composant | CPU aujourd'hui | Remarque |
|---|---|---|
| Kroko (STT) | **CPU pur, seul mode** | `kroko_transcriber.py` (`vram_mb=0`) — 10 s/5 min mesurés |
| whisper (faster-whisper) | oui (`compute_type` int8, `cpu_threads`) | RTF CPU large-v3 : plusieurs × temps réel → inutilisable en repli |
| Tous les transcribers natifs | repli automatique si CUDA absent | `_resolve_device` par backend — mais « CUDA absent » ≠ « VRAM pleine » |
| pyannote (diarisation) | oui (`pick_device` → CPU si rien de libre) | lent mais praticable sur audio court |
| SQUIM (préflight) | **repli CPU déjà actif** + « collant » après OOM | `squim_scorer.py:86-91,197-199` |
| DNSMOS | **CPU forcé** (onnxruntime) | `dnsmos_scorer.py:52` |
| VAD Silero, scène, genre | **CPU nativement** | — |
| LLM d'arbitrage | non réaliste | 14,7 Go de poids ; llama.cpp CPU = minutes par requête |

**La nuance décisive** : les replis existants se déclenchent quand CUDA est
*absent*, pas quand la VRAM est *occupée*. Un job en `WAITING_VRAM` a un GPU
présent mais plein — aucun `_resolve_device` ne bascule dans ce cas ; c'est
l'allocateur qui refuse en amont (`allocator.py:229`).

**Analyse par scénario.**

1. **Repli CPU du STT principal** — *déconseillé sauf Kroko.* Whisper large-v3 ou
   cohere en CPU multiplient le temps de transcription par un facteur qui transforme
   une attente de minutes en traitement d'heures : on dégrade le débit global pour
   un gain de latence illusoire. **Exception** : Kroko est *déjà* un backend CPU de
   qualité honorable (WER 0.43) — le « repli CPU » raisonnable consiste à proposer
   *le changement de backend*, pas le même backend sur un autre device.
2. **Repli CPU de la phase résumé** — *le cas rentable.* Si 2.1 (Kroko pour le
   résumé) est retenu, la phase wizard la plus sensible à l'attente ne consomme
   plus de VRAM du tout : le « repli » devient le mode nominal, sans aucune
   politique conditionnelle à écrire.
3. **Repli CPU de la diarisation** — *possible, borné.* Pyannote CPU sur 2 h
   d'audio se compte en dizaines de minutes ; acceptable en dépannage sur des
   fichiers courts (< 15-20 min). Politique : seuil de durée d'audio + opt-in.
4. **Repli CPU du LLM** — *non.* (Voir tableau.)

**Les « oui, mais » transverses.**
- *Contention CPU* : le CPU fait déjà tourner scène, VAD, DNSMOS, ffmpeg — et le
  serveur web. Un STT CPU de plus par job concurrent peut saturer la machine et
  ralentir *tout le monde*, y compris les jobs GPU en cours. Toute politique de
  repli doit compter les slots CPU comme l'allocateur compte la VRAM (sinon on
  déplace la famine au lieu de la résoudre).
- *Prévisibilité* : un job tantôt GPU tantôt CPU a des durées très variables ; le
  modèle de temps machine (`JobTimingStore`) mélangerait les deux populations.
  Contre-mesure simple : enregistrer le device avec le timing.
- *Qualité* : passer de cohere à Kroko en repli change le texte produit. Pour le
  résumé c'est indolore ; pour le SRT final c'est un choix que l'utilisateur doit
  faire, pas un automatisme silencieux.
- *Complexité d'admission* : aujourd'hui l'admission est « VRAM-aware » et simple
  (`scheduler.py:272-310`). Ajouter un chemin CPU crée un deuxième plan de
  capacité ; commencer par le périmètre où il n'y a *pas* de décision à prendre
  (résumé → Kroko, toujours) avant de généraliser.

**Recommandation.** Ne pas construire de « repli CPU » générique. Faire, dans
l'ordre : (a) 2.1 — le résumé en CPU nominal ; (b) borner l'attente VRAM locale
comme l'est l'attente distante (au-delà de N minutes : proposer explicitement à
l'utilisateur *SRT express via Kroko* ou *continuer d'attendre*) ; (c) seulement si
un besoin réel émerge, un repli diarisation CPU sous seuil de durée.

**Esquisse de (b)** — symétrie volontaire avec le mode distant :
```yaml
workflow:
  vram_wait:
    max_wait_s: 0        # 0 = illimité (comportement actuel, défaut)
    on_timeout: "notify" # notify = e-mail propriétaire + proposition dans l'UI
                         # (jamais de bascule automatique de backend : le
                         #  changement de moteur est un choix utilisateur)
```
Le compteur d'attente existe déjà côté distant (`_remote_unavailable_since`,
`pipeline_remote_gate.py:25-37`) ; la même persistance entre re-planifications
s'applique au cas local. La « proposition » réutilise la mécanique de reprise par
phases (le job re-soumis en profil `srt_express` + backend kroko ne rejoue pas ce
qui est déjà fait).

---

## 4. Choix des moteurs

### 4.1 STT : la grille de choix actuelle est bonne, sa présentation moins

**État actuel.** 8 backends natifs + 2 servis (registre `stt/registry.py:76-88`,
`models_catalog.py:38-54`), tous benchmarkés (§1.2). Le défaut (`cohere`) est un
choix qualité raisonnable mais **sous licence CC-BY-NC et gated** — les
alternatives Apache-2.0 plus rapides (Qwen3-ASR) ou plus légères (Kroko) existent
dans le produit sans être mises en avant.

**Pistes.**
- Faire de la **matrice moteur × usage** (préview / SRT express / qualité / une
  passe ASR+diar) un élément d'interface, pas seulement de doc : le wizard sait déjà
  forcer un backend par profil (`profiles.py`), il manque le conseil.
- **MOSS single-pass** : MOSS produit ASR + locuteurs + timestamps en une passe
  (WER 0.41, le meilleur du banc) — un profil « MOSS intégral » sauterait la
  diarisation pyannote séparée. Oui, mais : ses comptes de locuteurs sont
  approximatifs sur les réunions chaotiques, et le banc a documenté un **saut
  silencieux de 22 s** sur un monologue — le garde-fou « trou entre segments
  consécutifs » décrit dans le benchmark doit être implémenté avant tout usage par
  défaut. Effort M (garde-fou) + validation.

### 4.2 Diarisation : peu d'alternatives intégrées, un vrai sujet de veille

**État actuel.** pyannote (2000 Mo, défaut) et sortformer (3500 Mo, streaming)
sont intégrés ; le checkpoint inter-phases fonctionne (§2.2). La diarisation n'est
**pas** un poste de coût mesuré significatif sur les jobs courants (0,9 s en
réutilisation ; la pleine passe reste raisonnable sur GPU).

**Pistes** (aucune urgente) :
- la voie la plus prometteuse n'est pas un nouveau moteur mais **MOSS single-pass**
  (cf. 4.1) qui supprime l'étape ;
- surveiller pyannote `community-1` vs versions suivantes (le modèle est configuré
  par clé `models.pyannote_model`, `stt/diarization.py:119-121` — l'essai d'un
  nouveau modèle est déjà trivial) ;
- si un besoin temps-réel émerge (transcription live), sortformer streaming est
  déjà là.

### 4.3 LLM d'arbitrage : le backend et le modèle comptent autant que le cycle de vie

**État actuel.** L'arbitrage est agnostique du moteur (API compatible OpenAI) avec
**quatre backends au cycle de vie très différent**, abstraction `LLMBackend`
documentée dans `docs/LLM_BACKENDS.md` :

| Backend | Coût d'un démarrage | Coût d'un déchargement/rechargement |
|---|---|---|
| llama.cpp (défaut all-in-one) | **17 s mesurées** (chargement GGUF + port) | idem à chaque cycle |
| **vLLM** (topologie split, nœud GPU) | **minutes** (init moteur, compilation graphes CUDA, poids) | le plus pénalisé : `unload` tue `EngineCore`/`Worker_TP`, tout est à refaire |
| Ollama | démon persistant ; charge/décharge le **modèle** seulement | rechargement = chargement modèle (pas de redémarrage démon) |
| http distant | géré ailleurs ; `unload` = **no-op** | jamais déchargé par TranscrIA |

Les mesures du §1.1 (2 démarrages par job) concernent llama.cpp — **avec vLLM, le
même comportement coûterait des minutes par job**, pas 34 s. Le mode distant est
déjà protégé (no-op) ; le risque vLLM concerne le vLLM *local piloté par script*
(`launch_arbitrage_vllm.sh`).

**Pistes.**
1. **Politique de cycle de vie par backend** : le keep-warm de 2.3 (« ne pas
   arrêter si la file n'est pas vide ») devrait être *plus agressif encore* pour
   vLLM — ne décharger qu'après N minutes d'inactivité réelle (le coût de relance
   change la rentabilité du seuil). Le backend expose déjà son type
   (`llm_backend.py`) : la politique peut s'y indexer sans toucher aux appelants.
2. **Vitesse de génération : le palier de modèle est le levier n°1.** Les paliers
   validés existent (`docs/LLM_TIERS.md` : Qwen3.5-9B → Qwen3.6-35B selon la VRAM)
   mais un seul modèle sert les trois passes. Piste : **un palier par phase** —
   le résumé (tâche d'extraction, tolérante) sur un modèle petit et rapide, la
   correction SRT (la valeur ajoutée) sur le gros. Oui, mais : deux modèles
   résidents = VRAM double ou rechargements (contradiction directe avec 2.3) —
   n'a de sens qu'en Ollama (bascule de modèle bon marché) ou avec beaucoup de
   VRAM ; à instruire avec le banc LLM, pas en défaut.
3. **Vitesse de prompt : réutilisation du préfixe.** Les trois passes partagent de
   longs préfixes stables (prompt système, SRT complet). llama.cpp
   (`--cache-reuse`, prompt caching) et vLLM (prefix caching automatique) savent
   ne pas re-traiter le préfixe déjà vu — à condition que la LLM ne soit pas
   redémarrée entre les passes (encore 2.3) et que les prompts soient construits
   préfixe-stable-d'abord. Gain potentiellement important sur le *time-to-first-token*
   des passes 2 et 3 d'un même job ; à mesurer avant/après sur le banc.
4. **Pré-lancement dès l'upload** : le temps que l'utilisateur remplisse les
   étapes du wizard, les 17 s (llama.cpp) sont absorbées. Oui, mais : ne pas le
   faire si un job pipeline est sur le point de réclamer toute la VRAM. Effort S
   si couplé à 2.3.

---

## 5. Parcours utilisateur et exports

### 5.1 Profils « SRT seul » : autoriser le DOCX après coup — **P2, effort M**

**État actuel.** Les profils `srt_express` et `srt_locuteurs` déclarent
`docx_level="none"` (`workflow/profiles.py:139-188`) et le téléchargement DOCX
répond **404** (`downloads_api.py:133-135`). Or l'éditeur SRT reste disponible pour
ces profils, et le DOCX est par ailleurs **régénéré à chaque téléchargement** à
partir du SRT effectif (`downloads_api.py:119-142`, `exports/docx_report.py:1470-1489`) —
techniquement, produire un DOCX verbatim après édition ne coûte rien.

**Piste.** Pour les profils SRT, proposer sur la page résultat un bouton
« Générer un rapport DOCX » qui produit un DOCX *sans section synthèse* (le profil
n'a pas fait tourner le LLM — `meeting_context["summary"]` est vide, le générateur
doit dégrader proprement cette section). Variante plus ambitieuse : « promouvoir »
le job vers un profil supérieur (relancer les seules phases manquantes : résumé
LLM, correction), la mécanique de reprise par phases existant déjà
(`_done_profile_phases`, `scheduler.py:341-362`).

**Oui, mais…**
- *Cohérence commerciale des profils* : si le SRT express donne accès au DOCX, la
  frontière avec `word_rapide` s'estompe. Réponse : le DOCX « verbatim seul » (sans
  synthèse LLM) reste clairement distinct du rapport complet.
- *Le générateur DOCX suppose des artefacts présents* (participants, stats
  locuteurs, rapport qualité — `docx_report.py:1478-1481`). En profil srt_express
  sans diarisation, la section locuteurs doit disparaître proprement. À tester par
  profil.

### 5.2 Synthèse non resynchronisée après édition du SRT — **P2, effort S**

**État actuel.** Après un save de l'éditeur, le DOCX téléchargé reflète le SRT
édité (régénération au download) **sauf la section Synthèse**, qui reste celle
d'avant édition tant que l'utilisateur n'a pas cliqué la resynchronisation LLM
(`editor_routes.py:326-333,380-414`). Le système *suggère* la resync
(`summary_update_suggested`) mais rien ne signale, au moment du téléchargement,
que la synthèse est potentiellement périmée.

**Piste.** Marquer l'état « synthèse périmée » de façon persistante (un flag posé
au save si `_sync_summary_available`) et l'afficher à deux endroits : la page
résultat ET une mention dans le DOCX lui-même (« synthèse antérieure à la dernière
édition du verbatim »). Coût minime, lève une ambiguïté réelle.

### 5.3 ZIP servi périmé en backend fichiers local — **P2, effort S**

**État actuel.** En backend `pg`, le ZIP est reconstruit au téléchargement s'il est
périmé (`downloads_api.py:82-94`). En backend fichiers local, il est servi **tel
quel** (`downloads_api.py:95-99`) et dépend des reconstructions best-effort au save
éditeur / refine (`editor_routes.py:313-317`) — qui peuvent échouer silencieusement.

**Piste.** Unifier : appliquer le même test de fraîcheur (mtime des artefacts
sources vs mtime du zip) au backend local. Le code de comparaison existe déjà pour
`pg` (`newest_synced_mtime_ns`) ; en local, un simple `max(mtime des fichiers du
manifeste)` suffit.

### 5.4 L'utilisateur n'a aucune visibilité sur la file — **P1, effort S**

**État actuel.** La position dans la file et l'estimation d'attente calibrée
machine **existent et sont calculées** (`queue/wait_estimate.py:16-56`) mais ne
sont affichées que sur `/admin/queue`, page réservée aux admins et sans
auto-refresh (`queue/routes.py:73-81`, `templates/queue.html:109-112`).
L'utilisateur du wizard, lui, voit un message statique (`wizard_api.py:55-68`) et
un poller à 4 s (`wizard.js:995`).

**Piste.** Ajouter `queue_position` et `wait_estimate` à la réponse de
`GET /api/jobs/<id>/status` (`processing_api.py:279-293`) quand le job est en file
— le calcul existe, c'est un branchement. Afficher « Position 3 — démarrage estimé
dans ~12 min » dans la bannière du wizard. Attention : cette route est un contrat
⭐ `@api_stable` (`processing_api.py:281`) — l'ajout de champs est **additif**, donc
compatible, mais il engage : une fois publiés, `queue_position`/`wait_estimate`
font partie du contrat.

**Oui, mais…** L'estimation est calibrée sur l'historique machine
(`JobTimingStore`) : elle sera fausse au début et sur les jobs atypiques. Afficher
une fourchette ou « ~ » suffit ; une estimation approximative bat un message
statique.

### 5.5 Micro-irritants i18n — **P3, effort S (quick win)**

3 traductions EN marquées `fuzzy` et donc fausses à l'affichage
(`en/messages.po:1489-1509` : « Mise à jour en cours… » → « Exporting… »,
« Télécharger le DOCX à jour » → « Download SRT », « Voir le détail dans le chat »
→ « See technical details ») + 1 msgstr vide (placeholder d'exemple, ligne 4019).
Quinze minutes de travail, à faire au prochain passage sur l'i18n.

### 5.6 Le wizard exécute du GPU en synchrone au milieu du parcours — **P2, effort M**

**État actuel.** Deux étapes du wizard déclenchent du GPU avant la soumission
finale : le résumé (STT + pyannote + LLM, `wizard_api.py:154-250`) et la détection
locuteurs (`wizard_api.py:531-572`). C'est la conception assumée (l'utilisateur
valide des champs pré-remplis par le LLM) — mais c'est aussi là que l'attente VRAM
frappe l'utilisateur en pleine saisie.

**Piste.** Démarrer la phase résumé **dès la fin de l'upload** (en tâche de fond,
au lieu d'attendre que l'utilisateur atteigne l'étape 3) : les étapes 1-2 (analyse,
choix profil) durent le temps que le résumé se calcule, l'attente perçue fond.
Combiné à 2.1 (résumé rapide) et 4.3 (pré-lancement LLM), l'étape résumé devient
quasi instantanée dans le cas nominal.

**Oui, mais…** Si l'utilisateur abandonne après l'upload, on a dépensé du GPU pour
rien — arbitrage acceptable si le résumé est devenu bon marché (2.1) ; sinon,
déclencher au passage de l'étape 2 plutôt qu'à l'upload.

---

## 6. Système et maintenance

### 6.1 Sauvegarde de la base seule — **P1, effort S**

**État actuel.** `maintenance backup` produit une archive **monolithique**
base + jobs + voix + prompts + config (`maintenance/backup.py:130-194`) ; il
n'existe ni `--db-only` ni `--files-only` (vérifié). La base y est déjà capturée
proprement (PostgreSQL `pg_dump -Fc`, `backup.py:130-147` ; SQLite API backup à
chaud, `backup.py:113-128`). La restauration est tout-ou-rien.

**Réponse à la question posée** (« a-t-on un backup possible de la base seule,
est-il rapide ? ») : *non en l'état* — la base n'est extractible qu'en sauvegardant
aussi `jobs/`, dont le poids (audio compris) domine l'archive et le temps
d'exécution. Le dump `-Fc` de la base seule, lui, se compte en secondes (schéma
19 tables, volumétrie faible hors `job_files` en backend pg).

**Piste.**
- `backup --db-only` : n'archive que le dump + manifeste. Sauvegarde quotidienne
  en secondes, rotation `--keep` existante réutilisable telle quelle.
- `backup --files-only` : le complément (jobs/voix/prompts), pour les stratégies à
  deux fréquences (base : quotidien ; fichiers : hebdomadaire).
- `restore --db-only` symétrique, avec la même garde anti-restauration à chaud
  (`cli.py:91-116`).
- Cas particulier backend `pg` avec `job_files` en base : `--db-only` embarque
  alors *aussi* les artefacts (ils sont en base) — le documenter, c'est un
  avantage (une seule archive) autant qu'un piège (dump plus lourd).

**Oui, mais…** Une base restaurée seule peut référencer des jobs dont les fichiers
n'existent plus (backend local). La restauration `--db-only` doit l'annoncer et le
`doctor` sait déjà détecter l'incohérence storage (`doctor.py:645`).

### 6.2 La purge n'est pas planifiée — **P2, effort S**

**État actuel.** La purge de rétention tourne **au chargement de la page
d'accueil** (`pages_routes.py:479-488`) ou à la main (CLI/admin). Le seul timer
systemd du produit couvre la sauvegarde (`maintenance/schedule.py:66-104`). Sur une
instance sans visite de l'accueil, rien ne purge ; `jobs/` peut croître sans borne
(le doctor surveille le disque — fail < 2 Go — mais n'agit pas,
`doctor.py:666-697`).

**Piste.** Étendre `maintenance schedule` pour générer un second timer
`transcria-purge.timer` (quotidien, `purge` existant). Ajouter en P3 un garde-fou
de quota : refus d'upload (avec message clair) sous un seuil de disque libre — le
préflight d'upload connaît déjà la taille du fichier.

### 6.3 Pas de chemin de retour de migration — **P3, effort S (documentation)**

**État actuel.** `alembic upgrade head` est outillé partout (start.sh, upgrade,
job migrate) ; **aucun downgrade** n'est outillé ni documenté, les migrations sont
déclarées additives (`docs/UPGRADE.md`). C'est un choix défendable ; ce qui manque
est une phrase : *le retour arrière officiel, c'est la restauration de la
sauvegarde prise par `maintenance upgrade`* (qui la prend déjà automatiquement,
`maintenance/upgrade.py:50-51`). À écrire dans UPGRADE.md, rien de plus.

### 6.4 Pas de reset admin hors UI — **P2, effort S**

**État actuel.** Le premier admin est créé au bootstrap si la base est vide
(`auth/store.py:83-93`) ; ensuite, le mot de passe ne se change que connecté, via
l'UI. Un admin qui perd son mot de passe sur une instance mono-admin est **bloqué**
(il faut manipuler la base à la main).

**Piste.** `maintenance reset-admin-password <username>` (exécution locale
uniquement, génère un mot de passe temporaire à changer au premier login, audité).
Petit, mais c'est le genre d'outil dont on a besoin précisément le jour où on ne
peut pas attendre.

### 6.5 Divers exploitation (P3)

- **Rate limiting mono-process** (`auth/rate_limit.py:1-72`, en mémoire) : correct
  aujourd'hui (un seul process web) ; à revisiter seulement si multi-worker.
- **Métriques** : `/metrics` expose l'état de la file et des workers
  (`health_routes.py:42-96`) mais aucun timing de phase ni métrique GPU. Brancher
  `JobTimingStore` (P50/P95 par étape) et un gauge VRAM par carte donnerait un
  tableau de bord de capacité réel. Effort M.
- **Alerte VRAM admin par e-mail uniquement** (`admin_alerts.py:30-56`) : si les
  e-mails sont désactivés (défaut), l'alerte est invisible — la journaliser aussi
  en niveau WARNING est déjà fait ; l'afficher dans `/system` serait mieux.
- **Temps de build des images CI** : le rebuild complet de l'image slim prend
  ~3 h 40 sur runner GitHub (mesuré le 2026-07-17, cache invalidé par une
  modification des couches apt). Les caches étagés existent déjà
  (`buildcache-llama`, `buildcache-runtimes` sur GHCR) et amortissent les builds
  courants ; le seul levier restant serait de compiler llama.cpp pour moins
  d'architectures CUDA (5 aujourd'hui : 75-90). À ne toucher que si la liste des
  GPU supportés se réduit un jour — le coût n'est payé qu'aux changements de
  Dockerfile.

### 6.6 Installation : où part le temps, et les leviers — **P2/P3**

**État actuel.** `install.sh` orchestre des phases Python
(`transcria/installer/*`), chacune horodatée (`result.record(...)`,
`python_env.py:114`). Les deux postes dominants d'une installation nue :
1. **pip** : `python -m pip install` standard sur le venv (`python_env.py:112-121`),
   dont le plan torch CUDA (`torch_env.build_install_plan`) — plusieurs Go de
   wheels (torch cu126 + bibliothèques NVIDIA), résolution pip classique.
2. **Modèles** : `snapshot_download` de huggingface_hub, un modèle à la fois,
   chacun dans un sous-process dédié avec suivi de progression
   (`models_download.py:73-128`, `maintenance/cli.py:288-294`). **`hf_transfer`
   n'est pas activé** (vérifié : aucune référence) : les téléchargements HF
   plafonnent au débit d'un client Python mono-flux.

**Pistes, par rentabilité décroissante.**
- **Activer `hf_transfer`** (`HF_HUB_ENABLE_HF_TRANSFER=1` + le paquet éponyme) :
  téléchargements HF multi-flux en Rust, gain typique ×3-5 sur les gros poids
  (le LLM d'arbitrage seul pèse plusieurs Go). Oui, mais : barres de progression
  moins fines (le suivi actuel par taille de répertoire, `models_download.py:38-46`,
  continue de fonctionner) ; une dépendance de plus, à mettre dans requirements.
  **Effort S, quick win.**
- **Paralléliser les téléchargements de modèles entre eux** : les sous-process
  existent déjà, ils sont simplement lancés en séquence. Deux à trois flux
  concurrents saturent la liaison sans complexifier le suivi (chaque modèle a déjà
  son fichier de statut). Oui, mais : sur les liaisons lentes, le parallélisme
  n'apporte rien (le lien est le goulot) et brouille l'affichage — le borner à 2-3.
  Effort S/M.
- **`uv` à la place de pip** : résolution et installation nettement plus rapides.
  Oui, mais : un outil de plus à installer *avant* le venv (bootstrap), un
  comportement proxy/miroir d'entreprise à re-valider (l'épisode des index apt
  périmés de la 0.3.7 a montré la sensibilité de ces environnements), et
  `install.sh` garde sa garantie « python système nu » — à évaluer, pas urgent.
  Effort M.
- **Le vrai raccourci existe déjà : les images Docker.** L'image bundled est
  précisément la réponse « installation en minutes » (pull & run, tout embarqué) ;
  l'installation native reste la voie flexible. La doc d'installation gagnerait à
  ouvrir sur ce choix (« pressé ? → bundled ; sur-mesure ? → install.sh ») plutôt
  que de le mentionner en passant. Effort S (doc).
- **Mesurer le time-to-first-job** : les phases sont déjà horodatées
  individuellement ; agréger et afficher le total en fin d'install (et le
  consigner dans le résumé d'installation) donnerait la métrique de référence pour
  juger tout le reste. Effort S.

**Non retenu ici** : un mode `--offline` à wheelhouse pré-constituée pour les
sites sans accès internet — l'image bundled couvre déjà ce besoin sans créer un
deuxième artefact d'installation à maintenir.

---

## 7. Pistes envisagées et écartées

| Piste | Raison de l'écarter |
|---|---|
| Réutiliser la transcription du résumé dans le pipeline (§2.2) | Chunking incompatible (VAD vs tours pyannote) ; la re-projection créerait les bugs d'attribution que le découpage par tours élimine. Le levier est de rendre la 1re passe bon marché (2.1). |
| Repli CPU générique de toutes les phases (§3) | Le RTF CPU des gros modèles transforme une attente en heures de calcul ; la contention CPU pénaliserait les jobs GPU en cours. Périmètre restreint retenu à la place. |
| LLM d'arbitrage en CPU | 14,7 Go de poids, minutes par requête en llama.cpp CPU : incompatible avec des passes de correction déjà longues. |
| WebSocket/SSE pour la progression | Le polling à 4 s est fonctionnellement suffisant ; le gain UX réel vient des *informations* affichées (position, estimation — §5.4), pas du canal. À revisiter seulement si la charge de polling devient mesurable. |
| Nouveau moteur de diarisation | Aucun coût mesuré significatif (checkpoint inter-phases) ; la voie intéressante est la suppression de l'étape via MOSS single-pass (§4.1), pas un moteur de plus. |
| Quota de jobs par utilisateur | Aucun signal de besoin (instances = équipes de confiance) ; l'aging de la file assure déjà l'équité (`store.py:345-361`). |

---

## 8. Feuille de route suggérée

**Lot 1 — vite fait, très visible (S, cumulé ~2-3 jours)**
1. §5.4 position de file + estimation dans le statut job (l'existant, exposé).
2. §6.1 `backup --db-only` / `--files-only`.
3. §2.5 dé-doublonnage du préflight.
4. §2.7-1 instrumentation du bloc preprocess (préalable aux décisions chiffrées).
5. §5.5 les 4 chaînes i18n.
6. §6.2 timer de purge.
7. §6.6 `hf_transfer` + time-to-first-job affiché en fin d'install.

*Critères de succès : l'utilisateur en file voit position + estimation ; un backup
base seule < 30 s ; chaque étape preprocess a un P50/P95 mesuré sur jobs réels.*

**Lot 2 — le cœur vitesse (M, à valider par les mesures du lot 1)**
8. §2.1 backend résumé dédié (Kroko par défaut, runtimes C++ en option turbo).
9. §2.3 + §4.3-1 cycle de vie LLM : gardé chaud si file non vide, politique
   indexée sur le backend (seuil d'inactivité long en vLLM), pré-lancement à
   l'upload.
10. §2.6 WAV 16 kHz canonique.
11. §3-b borne d'attente VRAM avec proposition explicite à l'utilisateur.

*Critères de succès : phase résumé < 30 s sur 1 h d'audio (vs minutes aujourd'hui) ;
zéro démarrage LLM sur le 2ᵉ job d'une rafale (CAS A systématique) ; un seul
décodage complet du fichier par job (hors prétraitements actifs).*

**Lot 3 — parcours et produit (M/L)**
12. §5.1 DOCX à la demande pour profils SRT (voire promotion de profil).
13. §5.6 résumé lancé dès l'upload.
14. §5.2/5.3 fraîcheur synthèse + ZIP local.
15. §6.4 reset admin.

*Critères de succès : un utilisateur `srt_express` obtient un DOCX verbatim après
édition sans re-soumettre ; plus aucun téléchargement d'artefact périmé possible.*

**Chantier de fond (L, sur mesures)**
16. §2.4 coût des passes LLM (prompts, fusion correction+relecture, paliers) —
    y compris §4.3-2/3 : palier par phase et réutilisation de préfixe, à valider
    au banc LLM.
17. §4.1 profil MOSS single-pass avec garde-fou de saut silencieux.
18. §2.9 concurrence STT locale (voie servie d'abord).

**Dépendances entre pistes** : 4 (instrumentation) éclaire 10, 16 et 18 — le faire
en premier ; 8 (résumé Kroko) neutralise mécaniquement le reclaim LLM intra-job et
simplifie 9 ; la réutilisation de préfixe (§4.3-3) suppose 9 (LLM non redémarré
entre passes) ; 11 réutilise la reprise par phases qu'exploite aussi 12 (promotion
de profil) — les concevoir ensemble.

---

*Document rédigé le 2026-07-17 sur la base du code v0.3.7 (`9ef95a6`), des mesures
E2E du 2026-07-16 et de `docs/STT_BENCHMARK_REAL_MEETINGS.md`. Méthode : première
version issue d'une reconnaissance du code par domaines, puis trois passes
d'enrichissement successives avec vérification de chaque référence `fichier:ligne`
contre le code (les erreurs des rapports intermédiaires — p. ex. « la diarisation
est refaite deux fois » — ont été corrigées à cette occasion).*
