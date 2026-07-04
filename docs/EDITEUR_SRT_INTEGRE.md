# Éditeur de transcription intégré — analyse et plan

> **Statut : 🟢 LIVRÉ (lots A→E, 2026-07-03) — validé par bancs Playwright réels à
> chaque lot + premier test utilisateur en cours de chantier (retours tracés §11 bis).
> E2E GPU RÉEL GLOBAL VALIDÉ 15/15 (2026-07-03 : pipeline complet test2.mp3 →
> 3 personas pilotés navigateur → Word avec stats recalculées → restauration au
> fichier près). Reste : session secrétaires (peut suivre le push — arbitrage utilisateur).**
> Dernière feature avant la 0.2.0 (stabilisation puis publicité du projet). Demande
> utilisateurs : corriger la transcription (texte, découpage, locuteurs, timings) SANS
> quitter TranscrIA — le fork externe `srt-editor-pro-fr-easy` est jugé contraignant et
> son SRT édité **ne revient jamais dans le job** (livrables Word/ZIP jamais à jour).
> Ce document est la source de vérité du chantier ; on coche les lots au fur et à mesure.

---

## 0. Décisions verrouillées (utilisateur, 2026-07-03)

| # | Décision | Conséquence |
|---|---|---|
| D1 | **Éditeur NATIF dans TranscrIA** — pas d'intégration du fork (monolithe HTML 8 336 lignes + 4 variantes abandonnées, non maintenable) ; le fork = spec UX validée, pas une base de code | Page dédiée, vanilla JS, design system existant |
| D2 | **Sauvegarde à 3 filets** : brouillon SERVEUR continu (leçon du crash « secrétaire furieuse » : les 1ʳᵉˢ versions du fork sauvaient en localStorage navigateur) + bouton « Enregistrer une version » (restaurable) + undo/redo local | Brouillon dans le job dir (préfixe synchronisé), versions via RefineStore |
| D3 | **Pause automatique de l'audio à la frappe**, reprise à la validation | Mode relecture clavier au centre du design |
| D4 | **Waveform en v1** — pics calculés CÔTÉ SERVEUR (le fork décode dans le navigateur : intenable à 3 h 30-4 h 30) | Génération ffmpeg, cache dans le job |
| D5 | **Le fork est retiré** (« il faut qu'on fasse mieux que l'existant ») | Ménage complet — inventaire exact §8 |
| D6 | Chevauchements de temps : **autorisés à l'édition, signalés visuellement** (présents dans les vraies données de dev : « Chevauchements : 4 » en review_points) | Jamais bloquer un geste ; garde d'intégrité sur la forme, pas sur l'ordre strict |
| D7 | **UI très étudiée, belle et facile** — c'est LA feature de démarcation du projet | §7 = section normative, captures revues à chaque lot |

**Volumétrie cible** : 80 % ≤ 1 h (~600-900 chunks), 10 % < 2 h, max 4 h 30 (~3 000
chunks, souvent scindées matin/après-midi) ⇒ **liste virtualisée, lanes en canvas et
pics serveur dès la v1**.

**Personas** (verbatims utilisateurs) :
- **La vérificatrice intégrale** (secrétaires) : écoute TOUT et corrige au fil de
  l'eau ; préfère déjà le fork au casque + réécriture intégrale ; a perdu une session
  presque terminée (crash machine).
- **Le chirurgien ciblé** : vérifie UNE phrase d'UN locuteur, ou traite les points à
  vérifier du rapport qualité.
- **La réparatrice de diarisation** : quand pyannote mélange deux locuteurs, elle
  réécoute AUJOURD'HUI toute la réunion pour les séparer — gisement de valeur n°1.

**Questions d'arbitrage ouvertes** (à trancher à la revue de ce document) :

| # | Question | Ma recommandation |
|---|---|---|
| A1 | **Audio parfois indisponible post-complétion** (§1.6, précisé après vérification) : en split, la purge terminale ne supprime que les blobs EN BASE — l'original reste sur le disque de la frontale d'ORIGINE. L'audio manque donc seulement : autre frontale, disque nettoyé, frontale remplacée | **Mode dégradé sans audio en v1** (toutes les éditions restent possibles, bandeau explicite) — filet pour ces cas ; le cas courant (frontale d'origine, et toute install `fs`) A l'audio. Option v1.1 : rétention/re-push configurable |
| A2 | **Cohérence stats/participants après édition des locuteurs** (§1.5) : le DOCX calcule les temps de parole depuis `speakers/speaker_stats.json` — éditer les locuteurs du SRT sans recalcul rendrait le Word faux | **Recalculer les stats depuis les chunks à chaque « Enregistrer une version »** + ajouter les nouveaux locuteurs au mapping ; les deux fichiers entrent dans le snapshot (restauration cohérente) |
| A3 | **Relance d'un traitement après éditions manuelles** : une re-correction LLM réécrit `transcription_corrigee.srt` | Comportement conservé + **avertissement explicite avant relance** s'il existe des versions d'édition (le filet = les versions) ; à documenter utilisateur |

---

## 1. Analyse de l'existant (vérifiée dans le code, 2026-07-03)

### 1.1 Inventaire des surfaces concernées

| # | Quoi | Où (vérifié) | Impact éditeur |
|---|---|---|---|
| S1 | SRT brut / corrigé | `metadata/transcription.srt` / `metadata/transcription_corrigee.srt` | L'éditeur lit l'EFFECTIF (corrigé sinon brut, helper `_effective_srt`, `web/routes.py`), écrit TOUJOURS le corrigé — le brut reste la source intacte |
| S2 | Format des chunks (réel, jobs de dev) | `1\n00:00:01,012 --> 00:00:03,910\nSPEAKER_01(Vendeur / fromager): Podcast francefacil.com` | Le locuteur est un PRÉFIXE TEXTUEL à parser/reformater ; certains chunks n'en ont pas (à tolérer) |
| S3 | Garde d'intégrité SRT | `WorkflowRunner._corrected_srt_integrity_error` (`workflow/runner.py:1860`) | Réutilisée à la sauvegarde, ASSOUPLIE : l'humain a le droit de supprimer/fusionner (la garde vérifie la forme — index/timestamps/encodage — pas le volume) |
| S4 | Locuteurs du job | `speakers/speaker_mapping.json` (noms validés), `speakers/speaker_stats.json` (temps de parole, tours) | Menu de réattribution ; lanes triées par temps de parole ; **cohérence à maintenir (A2)** |
| S5 | Consommation des stats | `exports/docx_report.py:349-415` (`speaking_time_seconds` → tableau participants + %) | Si l'édition change les locuteurs, les stats DOIVENT suivre (A2) |
| S6 | Audio | `input/original.mp3` seul en pratique (les variantes prétraitées `normalized/denoised/scene_filtered` préservent la timeline quand elles existent) ; route `/download/audio` = `as_attachment`, **sans** `conditional=True` (routes.py:2211) | Streamer l'ORIGINAL ; nouvelle route avec Range (seek) requise ; **purge possible → A1/§1.6** |
| S7 | Points qualité | `quality/review_points.json` — liste de STRINGS, certains portent des timestamps libres (« relire silence 00:32→00:35 ») | Liste guidée §6.2 ; ancrage structuré à ajouter (§3.6) |
| S8 | Versions restaurables | `RefineStore` (`workflow/refine_store.py:86-215`) : `snapshot_artifacts(paths)→N`, `restore_version`, `list_versions`, manifeste `{path, absent}` | Réutilisé tel quel — **pool COMMUN avec le chat d'affinage** (une seule liste « Versions » sur la page résultats, restauration croisée) |
| S9 | Régénération livrables | DOCX régénéré à CHAQUE téléchargement ; ZIP rebuild ; note `#refine-fresh-note` | Sauver le SRT (+stats) suffit — rien d'autre à faire |
| S10 | Synchro topologie split | `metadata/` et `speakers/` ∈ préfixes synchronisés (pg) | Brouillon et écritures voyagent d'office |
| S11 | Audit | `audit_log`, famille `job` ; règle : jamais de contenu transcript dans `details_json` | Nouvelles actions §3.7 |
| S12 | Surface du fork à retirer | `integrations/srt_editor_link.py` (`SrtEditorLink`) ; routes.py:46,1188 + route `push-to-editor` ; `job_result.html:61-62`, `job_wizard.html:12,948-950` ; config `services.srt_editor_easy_url` (loader.py:78, schema:189, example:81) + `workflow.enable_external_srt_editor_link` (loader.py:211, schema:242, example:285) | Inventaire du ménage §8 |

### 1.2 États de job où l'éditeur a un sens

L'éditeur s'ouvre dès qu'un SRT EXISTE et qu'aucun traitement n'est EN COURS sur le
job — pas seulement `completed` :

| État | Éditeur | Pourquoi |
|---|---|---|
| `completed` / `export_ready` | ✅ | Cas nominal (relecture avant diffusion) |
| pipeline terminé d'un profil léger (`srt_express`, diarisation+SRT) | ✅ | LE cas des utilisateurs « j'édite ensuite à la main » — SRT brut édité → sauvé en corrigé |
| `failed` avec SRT présent | ✅ | Sauver ce qui est récupérable |
| phase en cours (`*_running`, entrée de file active) | 🔒 lecture seule (bandeau) | Une écriture concurrente du pipeline écraserait l'édition |
| tour d'affinage LLM actif (`busy` du chat) | 🔒 lecture seule le temps du tour | Même page résultats, même signal de polling |

### 1.3 Le fork — autopsie (spec UX, mécanique, pièges)

**Architecture** : 1 fichier HTML de 8 336 lignes (wavesurfer.js minifié inclus,
172 fonctions) + `server.py` (840 lignes : upload audio/SRT, `/api/save` d'état
complet). Variantes `vtt-editor-v2/2.1/2.2/3` abandonnées à côté.

**À reprendre** (plébiscité ou éprouvé) : icônes rapides PAR chunk (écouter, corriger,
changer locuteur, ajuster timing, couper au curseur, supprimer) ; raccourcis `S`/`E`
(caler début/fin sur la tête de lecture — le geste de retiming le plus rapide qui
soit), `C` (couper au curseur), `M` (marqueur), `Espace`, `G` (aller au temps),
`Ctrl+F` ; « décaler les segments suivants » (cascade) et « décaler tous les
timings » ; batch edit (sélection multiple) ; fresque globale une-ligne + « Vue
globale » minimap ; indicateur d'état de sauvegarde ; copier-coller naturel dans le
texte.

**À proscrire** : panneau « Corriger le segment » FIGÉ en haut à gauche (rejeté — on
édite loin de ce qu'on regarde) ; décodage audio dans le navigateur ; undo plafonné à
20 pas ; toggles experts en barre principale (Snap Grid, 100ms, Allow Overlap) ;
sauvegarde historique en localStorage (l'origine du drame de la secrétaire — la v4 du
fork est passée serveur, preuve que la leçon est déjà payée).

### 1.4 Chevauchements — comment le pipeline les traite

pyannote DÉTECTE la parole superposée ; TranscrIA aplatit volontairement en « tours
exclusifs » (`exclusive_turns`) pour produire un SRT séquentiel. Les données réelles
gardent pourtant des chevauchements résiduels (review_points de dev : « 4 dont 0
≥ 1.0s »). Position (D6) : l'éditeur les AFFICHE (blocs superposés hachurés sur
fresque/lanes, pastille sur les cartes), les AUTORISE à l'édition, et `validate_chunks`
les liste en avertissements non bloquants.

### 1.5 Cohérence locuteurs ↔ stats ↔ livrables (A2 — découvert au cadrage)

Le tableau « Participants & Locuteurs » du DOCX calcule les pourcentages depuis
`speaker_stats.json` (S5). Si l'éditeur réattribue 40 chunks de SPEAKER_03 vers un
nouveau « SPEAKER_07(Mme X) » sans toucher les stats : Word faux, lanes fausses à la
réouverture. Proposition (A2) : à chaque « Enregistrer une version »,
**recalculer** `speaker_stats.json` depuis les chunks édités (temps = Σ durées par
locuteur, tours = nb de chunks) et **compléter** `speaker_mapping.json` des locuteurs
créés ; les DEUX fichiers rejoignent le snapshot de version (restauration cohérente).
Après édition, les stats deviennent « temps de parole transcrit » (vs acoustique
initial) — différence documentée, assumée : c'est ce que voit le lecteur du Word.

### 1.6 Disponibilité de l'audio post-complétion (A1 — vérifié dans le code)

En topologie split (`storage.shared_backend: pg`), la purge terminale
(`artifact_store.purge_input_files`, appelée par `_purge_input_blobs`) supprime les
blobs `input/` **EN BASE uniquement** — docstring explicite : « l'original reste sur
le disque de la frontale (origine) ; un reprocess re-pousse input/ à l'enfilage ».
Conséquences pour l'éditeur :
- **frontale d'origine** (celle qui a reçu l'upload) : l'audio est LÀ sur disque,
  lecture/waveform/S-E fonctionnent — le cas courant du split marche ;
- **autre frontale / disque nettoyé / frontale remplacée** : `input/` non
  matérialisable (blobs purgés) → **mode dégradé** ;
- **tout-en-un `fs`** (la prod actuelle) : jamais concerné.
Position (A1) : le mode dégradé est un état de première classe — sans audio,
l'éditeur reste pleinement fonctionnel pour texte/locuteurs/découpage/fusion (seuls
lecture, `S`/`E` et waveform sont désactivés, bandeau explicite). La rétention/re-push
configurable est notée v1.1, hors périmètre.

### 1.7 Topologies — vérification systématique (all-in-one / frontale+worker / nœud de ressources)

Vérifié dans `transcria/jobs/artifact_store.py:45-69` et les images Docker :

| Maillon | Vérdict | Preuve |
|---|---|---|
| Brouillon `metadata/srt_editor_draft.json` | ✅ voyage | `metadata/` ∈ `SYNCED_PREFIXES` ; écrit via HTTP sur la frontale → push `after_app_request` (fichier ~10-100 Ko toutes les ~5 s : volume acceptable) |
| SRT corrigé + `speaker_stats` + `speaker_mapping` (save) | ✅ voyage | `metadata/` et `speakers/` ∈ `SYNCED_PREFIXES` ; écriture web autorisée (`WEB_WRITE_PREFIXES` = tout sauf `input/`) |
| Snapshots de versions | ✅ voyage | `refine/` ∈ `SYNCED_PREFIXES` (posé au chantier affinage) — pool commun confirmé split-safe |
| Pics `metadata/waveform_peaks.bin` | ✅ voyage (~324 Ko une fois) | `metadata/` synced ; génération sur la frontale : **ffmpeg présent dans l'image runtime multi-rôles** (`Dockerfile:52`) et prérequis hôte d'install.sh |
| Audio | ✅ frontale d'origine / dégradé sinon | §1.6 |
| Nœud de ressources | ✅ non concerné | l'éditeur est 100 % web + fichiers ; aucune inférence |
| Rôle `web` sans GPU | ✅ | aucune dépendance GPU (parse/serialize/pics = CPU) |
| Deux frontales sur le même job | ⚠ P8 | le `revision` du brouillon est vérifié contre le fichier LOCAL (pull throttlé) — fenêtre de course rare, perte bornée à ~5 s de frappe ; assumé v1 (documenté) |

**Rétro-vérification des deux features déjà livrées** (même grille) :
- *Chat d'affinage* : `refine/` ajouté à `SYNCED+INPUT_PREFIXES` à son chantier ✅ ;
  mode `refine` dispatché sans audio ✅ (déjà validé E2E).
- *Types de réunion* : templates + logos en BASE (`meeting_type_templates`) donc
  partagés entre tiers ✅ ; fiche matérialisée dans `context/` (synced) ✅ ; logo
  matérialisé `context/type_logo.png` (synced → le worker qui construit le ZIP/DOCX
  le voit) ✅ ; `preview.docx` = CPU pur sur la frontale ✅ ; nœud de ressources non
  concerné ✅. Aucun correctif nécessaire.

---

## 2. Conception cible — vue d'ensemble

Page dédiée **`/jobs/<id>/editor`** (un ATELIER plein écran — fresque et lanes ont
besoin de la largeur), atteignable depuis : la page **Résultats & affinage** (bouton
principal « Éditer la transcription »), l'étape Export du wizard, et chaque point à
vérifier du rapport qualité (ouvre calé sur le chunk).

```
flux : SRT effectif ──parse──► modèle chunks (start_ms, end_ms, speaker, texte)
       ▲                                   │ gestes (undo/redo delta, ≥200 pas)
       │ restauration (versions,           ▼ debounce ~5 s
       │  pool commun affinage)      brouillon serveur (draft.json, revision++)
       └── RefineStore ◄── « Enregistrer une version » (Ctrl+S) ──┐
             snapshot AVANT write-back de :                       │
             transcription_corrigee.srt + speaker_stats.json      │
             + speaker_mapping.json  ◄────────────────────────────┘
             (sérialisation SPEAKER_XX(Nom): texte, garde de forme, audit)
```

Complémentarité assumée avec le chat d'affinage (même page résultats, même pool de
versions) : **le chat pour les changements globaux, l'éditeur pour la chirurgie** —
argument produit, pas seulement architecture.

---

## 3. Données & API

### 3.1 Modèle serveur — `transcria/workflow/srt_editor.py` (NOUVEAU, pur/testé)

- `parse_srt_chunks(text) -> list[dict]` : `{index, start_ms, end_ms, speaker_id,
  speaker_name, text}` — préfixe `SPEAKER_XX(Nom):` TOLÉRANT (variantes : sans nom
  `SPEAKER_03:`, sans préfixe du tout → `speaker_id=None`) ; jamais d'échec sur un SRT
  lisible.
- `serialize_chunks(chunks) -> str` : renumérotation séquentielle, timestamps SRT
  `HH:MM:SS,mmm`, reconstruction du préfixe. **Round-trip à l'octet près** sur un SRT
  non modifié (test d'or sur les SRT réels de dev).
- `validate_chunks(chunks) -> list[str]` : avertissements NON bloquants
  (chevauchements, durées ≤ 0, textes vides, fin > durée audio si connue).
- `compute_speaker_stats(chunks) -> dict` : recalcul A2 (format identique à
  `speaker_stats.json` existant).

### 3.2 Brouillon — `metadata/srt_editor_draft.json` (schéma v1)

```json
{
  "schema_version": 1,
  "revision": 42,                        // verrou optimiste (§3.5)
  "updated_at": "2026-07-03T15:04:05Z",
  "base_srt_sha256": "…",                // le SRT d'origine de la session (détection de conflit)
  "chunks": [ {"start_ms": 1012, "end_ms": 3910, "speaker_id": "SPEAKER_01",
               "speaker_name": "Vendeur / fromager", "text": "…"} ],
  "new_speakers": [ {"speaker_id": "SPEAKER_07", "speaker_name": "Mme X"} ],
  "markers": [ {"at_ms": 754000, "label": "à réécouter — chiffre"} ],
  "progress": {"listened_ranges_ms": [[0, 1830000]], "visited_chunk_count": 412}
}
```

Purgé à « Enregistrer une version », à la restauration d'une version, et avec le job.
Si `base_srt_sha256` ne correspond plus au SRT courant à la réouverture (un affinage
LLM est passé entre-temps) : proposer « reprendre le brouillon (écrasera ces
changements) » vs « repartir du SRT actuel » — jamais de fusion silencieuse.

### 3.3 Pics de waveform — `metadata/waveform_peaks.bin` (+ méta)

ffmpeg → PCM mono 8 kHz → max-abs par fenêtre de 50 ms (20 pics/s) → **Int8** binaire ;
en-tête JSON séparé `waveform_peaks.json` `{version, sample_rate, window_ms, count,
duration_ms}`. 4 h 30 ≈ 324 000 octets. Génération PARESSEUSE à la première ouverture
(tâche courte côté serveur, spinner discret), best-effort : sans pics ni audio,
l'éditeur fonctionne en blocs (A1).

### 3.4 Routes — blueprint `transcria/web/editor_routes.py` (NOUVEAU)

| Route | Méthode | Contrat |
|---|---|---|
| `/jobs/<id>/editor` | GET | Page atelier (owner/admin ; 404 sans SRT ; lecture seule si traitement actif §1.2) |
| `/api/jobs/<id>/editor/state` | GET | `{chunks, speakers (mapping+stats), markers, draft: {exists, updated_at, revision, conflict}, review_points (+anchors), audio: {available, duration_ms, peaks_ready}, readonly}` |
| `/api/jobs/<id>/editor/draft` | PUT | Corps = schéma §3.2 ; `revision` périmée → **409** `{server_revision}` ; sinon 204. JAMAIS d'autre erreur bloquante (le brouillon est un filet) |
| `/api/jobs/<id>/editor/draft` | DELETE | Abandon explicite du brouillon |
| `/api/jobs/<id>/editor/save` | POST | Corps = chunks (+new_speakers) ; garde de forme → 422 avec raisons ; snapshot RefineStore (SRT + stats + mapping) → write-back → recalcul stats (A2) → purge brouillon → audit ; réponse `{version, warnings}` |
| `/api/jobs/<id>/audio/stream` | GET | Audio ORIGINAL inline, **`conditional=True`** (Range/206 pour le seek) ; 404 propre si purgé (A1) ; audité 1×/session d'édition |
| `/api/jobs/<id>/editor/peaks` | GET | Binaire §3.3 (génère au 1ᵉʳ appel ; 202 « en cours » puis 200) |

La restauration passe par la route de versions EXISTANTE du pool commun.

### 3.5 Concurrence

Verrou OPTIMISTE par `revision` du brouillon : deux onglets/personnes → le second PUT
reçoit 409 + bandeau « Ce job est édité ailleurs (il y a X s) — recharger ». Pas de
verrou pessimiste en v1 (cas rare, jamais de perte : le premier arrivé garde la main).

### 3.6 Points à vérifier cliquables

`review_points.json` reste une liste de strings (compat totale). Le QualityReporter
écrit EN PLUS `quality/review_points_anchors.json` : `[{text, start_ms?, end_ms?,
kind}]` pour les points qui connaissent leurs bornes (chevauchements, zones audio,
segments suspects). L'éditeur matche par texte : ancré quand il peut, simple entrée de
liste sinon. Progression « traité » cochable, persistée dans le brouillon.

### 3.7 Audit & RGPD

`job_srt_edit_save` (métadonnées : nb chunks, nb modifiés, locuteurs créés, version),
`job_srt_edit_revert` (déjà couvert par revert commun), `job_download` pour le
stream audio (1×/session). Jamais un extrait de texte dans `details_json` (règle S11).

---

## 4. Sauvegarde — les trois filets (D2)

| Filet | Quand | Où | Récupération |
|---|---|---|---|
| Undo/redo | chaque geste | mémoire navigateur — pile de DELTAS `{op, chunk_ref, avant, après}` (édition texte regroupée par chunk ; cible ≥ 200 pas) | `Ctrl+Z` / `Ctrl+Y` |
| **Brouillon serveur** | debounce ~5 s après un changement | §3.2, dans le job (voyage en topologie split) | à l'ouverture : « Reprendre où vous en étiez (il y a X min, N modifications) » / « Repartir de la dernière version » |
| **Version** | bouton explicite + `Ctrl+S` | snapshot RefineStore (SRT + stats + mapping) puis write-back | liste Versions de la page résultats (commune avec l'affinage), restauration au fichier près |

Indicateur permanent et honnête dans l'en-tête : `● enregistré il y a 3 s` /
`● enregistrement…` / `⚠ hors ligne — nouvelle tentative dans 5 s` (retry
exponentiel, la frappe n'est jamais bloquée). La confiance de la vérificatrice
intégrale se gagne sur cet indicateur.

---

## 5. Timeline & waveform (D4)

Trois étages, trois coûts :

1. **Fresque globale une-ligne** (toujours présente) : blocs SRT colorés par locuteur,
   dérivés du MODÈLE ÉDITÉ (jamais désynchronisée), tête de lecture, marqueurs,
   surlignage des résultats de recherche. Rendu **canvas** (3 000 blocs sans DOM).
2. **Lanes par locuteur** (repliables) : une piste/intervenant triée par temps de
   parole décroissant, en-tête `nom · temps · nb prises · [🎧 solo]`, clic sur un
   bloc = sélection + scroll + lecture, glisser sur une lane = sélection multiple
   (lasso). Canvas également (P3).
3. **Bande zoomée** autour de la tête de lecture : vraie forme d'onde (pics §3.3)
   + bornes du chunk actif manipulables à la souris ET par `S`/`E`. C'est ici que le
   retiming à l'oreille devient précis.

Chevauchements : hachures sur les zones superposées (fresque + lanes) + pastille sur
les cartes concernées (D6). Zoom : molette sur la bande, `+`/`-`, « Ajuster ».

---

## 6. Modes de travail (les personas deviennent des fonctions)

### 6.1 Relecture continue (vérificatrice intégrale)
Lecture au fil de l'eau, carte active surlignée et suivie (scroll doux), **pause auto
à la frappe** (D3), `Entrée` = valider + reprendre, `Tab` = chunk suivant sans
valider. **Jauge de vérification** (union des plages écoutées + chunks visités,
persistée au brouillon §3.2) : elle sait où elle en est après une interruption, et le
statut « vérifié à N % » devient un argument qualité du livrable.

### 6.2 Chirurgie ciblée
Recherche plein-texte (`Ctrl+F`, hits dans la liste ET sur la fresque), aller au temps
(`G`), **points à vérifier** en onglet latéral avec compteur « 12/27 traités » —
chaque clic ouvre le chunk, cale l'audio, prêt à corriger : la relecture devient une
liste de tâches, plus une écoute de 3 heures.

### 6.3 Réparation de diarisation
**Écoute solo** d'un locuteur (ne joue QUE ses segments, enchaînés — une rupture de
voix s'entend en minutes) ; **sélection multiple** (clic-shift dans la liste, lasso
sur lane) → barre d'actions flottante « Attribuer à… ▾ / Fusionner / Supprimer »,
avec **création d'un locuteur à la volée** (nom libre → `SPEAKER_XX(Nom)` nouveau,
propagé au mapping à la sauvegarde — A2) ; marqueurs `M` (signets nommables, listés,
cliquables) ; « décaler les segments suivants » (cascade, repris du fork).
*(v2 notée, architecture ouverte : suggestion automatique de séparation via
`speakers/speaker_embeddings.json`.)*

---

## 7. UI — section normative (D7 : « très étudiée, belle et facile »)

### 7.1 Parti pris visuel

Un **atelier**, pas un formulaire : plein écran, chrome minimal, la matière (audio +
texte) occupe tout. Tokens transcria.css conservés mais registre propre : fond
assombri d'un cran par rapport au portail (les couleurs de locuteurs portent
l'information, elles doivent chanter), cartes calmes au repos / expressives au survol,
AUCUNE modale pour les gestes courants, micro-transitions ≤ 150 ms (surlignage de la
carte active, apparition de la barre d'actions). **Palette locuteurs** : cycle de 12
teintes calibrées (contraste AA sur le fond atelier, distinctes en vision daltonienne
pour les 8 premières), affectation STABLE (même couleur en lane, carte, fresque,
badge, DOCX sans lien — l'éditeur fait référence). Cible : **≥ 1280 px desktop-first**
(métier sur poste fixe) ; en dessous, lanes repliées d'office et fresque seule —
jamais illisible, jamais optimisé mobile (assumé).

### 7.2 Plan de l'écran

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│ ◄ Résultats │ Réunion budget — 1 h 02 │        ● enregistré il y a 3 s │ [💾 Enre- │
│             │                          │                                gistrer   │
│             │                          │                                une       │
│             │                          │                                version]  │
├──────────────────────────────────────────────────────────────────────────────────┤
│ ▶ 00:12:34 / 1:02:10  ⏪10s ⏩10s  ×1 ▾  🔊──── │ BANDE ZOOMÉE (waveform + bornes  │
│                                                │ du chunk actif, S/E, molette)    │
│ FRESQUE GLOBALE  ▮▮▮▮▮▮▮▮▮▮▮▮▮▮│▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮▮  (minimap, marqueurs ▾, hits) │
│ ── Mme Dupont  1 h 12 · 214 ▮▮▮  ▮   ▮▮▮▮▮        [🎧] ──────────────── (lanes    │
│ ── M. Martin     43 m · 156   ▮▮▮▮ ▮▮   ▮▮▮       [🎧]     repliables) ─────────  │
├──────────────┬────────────────────────────────────────────────────────────────────┤
│ PANNEAU      │  LISTE DES CHUNKS (virtualisée, suit la lecture)                   │
│ (onglets)    │ ┌────────────────────────────────────────────────────────────────┐ │
│ ○ À vérifier │ │ ● Mme Dupont   00:12:31 → 00:12:36                    #214  ⚑  │ │
│   12/27 ✓    │ │ Le budget prévisionnel est adopté à l'unanimité│                │ │
│ ○ Marqueurs 3│ │ ▶ écouter  🗣 locuteur ▾  ✂ couper  ⧉ fusionner  ⏱ timing  ⋯   │ │
│ ○ Recherche  │ └────────────────────────────────────────────────────────────────┘ │
│ ○ Locuteurs  │ ┌ M. Martin  00:12:37 → 00:12:41 … (carte calme, icônes au survol)┐│
└──────────────┴────────────────────────────────────────────────────────────────────┘
   sélection multiple active ⇒ barre flottante : « 12 segments · Attribuer à ▾ ·
   Fusionner · Supprimer · Annuler »
```

États remarquables, tous maquettés et capturés au lot correspondant :
- **Reprise de brouillon** (ouverture) : carte centrale sobre « Reprendre où vous en
  étiez — il y a 18 min, 37 modifications » / « Repartir de la dernière version ».
- **Audio indisponible** (A1) : bandeau ambre unique, boutons audio désactivés avec
  tooltip, tout le reste vivant.
- **Lecture seule** (§1.2) : bandeau bleu « Un traitement est en cours — l'éditeur
  rouvrira en écriture à la fin ».
- **Solo actif** : chip persistante « 🎧 Solo : Mme Dupont — quitter ».

### 7.3 La carte de chunk (le cœur — remplace le panneau figé rejeté)

- **Le texte EST le champ** (contenteditable, collage forcé en texte brut — P1) :
  cliquer = éditer, aucun mode. Sélection/copier-coller natifs (plébiscités). La
  frappe met l'audio en pause (D3) ; `Entrée` valide et reprend la lecture.
- Rangée d'icônes AU SURVOL/FOCUS (carte calme sinon) : ▶ écouter ce segment ·
  🗣 locuteur (menu : locuteurs du job, « ➕ nouveau… ») · ✂ couper au curseur ·
  ⧉ fusionner avec le précédent · ⏱ tiroir timing (début/fin éditables, ±100 ms/±1 s,
  rappel `S`/`E`, « décaler les suivants ») · ⋯ (supprimer, insérer avant/après).
- États visibles : active (liseré couleur locuteur + fond léger), éditée non
  sauvegardée (point ambre), vérifiée (coche discrète — jauge), en chevauchement
  (pastille hachurée), hit de recherche (surlignage du terme).
- « Changer le locuteur d'une phrase » = sélectionner la phrase → `C` (coupe aux
  bornes de la sélection) → 🗣 sur la nouvelle carte. Deux gestes, zéro dialogue.

### 7.4 Raccourcis (aide `?` en overlay — appris du fork)

`Espace` lecture/pause · `Entrée` valider + reprendre · `Tab`/`Maj+Tab` chunk
suivant/précédent · `S`/`E` caler début/fin sur la tête de lecture · `C` couper au
curseur · `M` marqueur · `G` aller au temps · `Ctrl+F` recherche · `Ctrl+Z`/`Ctrl+Y`
undo/redo · `Ctrl+S` enregistrer une version · `Échap` quitte sélection/solo.
Interceptés SEULEMENT hors saisie de texte (sauf `Entrée`/`Échap`/`Ctrl+…`) ; tout est
doublé à la souris — le clavier accélère, il n'est jamais requis.

### 7.5 Qualité perçue (les détails qui font « beau et facile »)

Squelettes de chargement (jamais d'écran blanc) ; états vides en langage métier
(« Aucun point à vérifier — beau travail ») ; confirmation UNIQUEMENT pour le
destructif (suppression de chunks non vides) ; tooltips français concis partout ;
focus clavier visibles (AA) ; position de liste STABLE au re-rendu (virtualisation) ;
`aria-live` sur l'indicateur de sauvegarde. **Budgets** : ouverture < 2 s (1 h) /
< 5 s (4 h 30, pics en tâche de fond) ; frappe sans latence perceptible ; scroll 60 fps
sur 3 000 chunks ; seek audio < 300 ms. **Chaque état du §7.2 est capturé par
Playwright et revu comme du code à chaque lot.**

---

## 8. Ménage (D5) — inventaire exact

Retraits (surface S12) : bouton `job_result.html:61-62` et bloc
`job_wizard.html:948-950` (+ `data-editor-url` ligne 12) ; route
`/api/jobs/<id>/push-to-editor` + import `SrtEditorLink` (routes.py) ; module
`transcria/integrations/srt_editor_link.py` ; clés `services.srt_editor_easy_url` et
`workflow.enable_external_srt_editor_link` (loader, schema, example — dépréciation
douce : clé présente ignorée + warning UNE version, retrait du schéma à la 0.2.0) ;
tests associés adaptés ; docs (INSTALL, TECHNICAL, CONFIG_REFERENCE, READMEs)
nettoyées. L'action d'audit `job_external_push` reste dans l'historique (données).

---

## 9. Exclusions assumées (v1 — noir sur blanc)

Pas d'édition multi-pistes ; pas de co-édition temps réel (verrou optimiste seulement) ;
pas de retranscription IA partielle depuis l'éditeur (« retranscrire ce segment » = v2) ;
pas de suggestion automatique de séparation de locuteurs (embeddings = v2) ; pas de
nouveaux formats d'export (SRT/VTT existants inchangés) ; pas d'optimisation mobile ;
pas d'édition de la waveform elle-même (pas de « redessiner l'audio ») ; la rétention
d'audio post-complétion reste la politique actuelle (A1, évolution v1.1 notée).

---

## 10. Découpage en lots (chaque lot : gates CI exacts + captures revues)

- [x] **Lot A — Fondations (serveur)** (2026-07-03 — 3 204 tests, cov 80,98 %) : `srt_editor.py` (parse/serialize/validate/
  compute_stats), blueprint routes §3.4, brouillon (schéma, revision, purge), save
  (garde assouplie + snapshot pool commun + recalcul A2 + audit), stream Range, pics
  ffmpeg. **Critères** : round-trip à l'octet sur les SRT réels de dev ; 409 de
  revision ; restauration croisée éditeur↔affinage verte ; stream 206 ; job sans
  audio → `audio.available=false` propre. **Réalisé conforme + notes** : round-trip
  validé sur les 14 SRT réels (découverte : les SRT écrits par la correction LLM n'ont
  pas de saut de ligne final → normalisation unique documentée) ; pics vectorisés
  numpy (0,33 s pour 73 s d'audio) ; restauration croisée éditeur↔affinage testée ;
  31 tests (module + API).
- [x] **Lot B — L'atelier v1** (2026-07-03 — 3 204 tests, cov 80,96 %) : page, liste virtualisée,
  carte complète §7.3 (texte-champ, icônes, couper/fusionner/locuteur/timing),
  lecteur + synchro carte active, pause auto à la frappe, undo/redo, brouillon/reprise,
  enregistrer une version, états lecture-seule et sans-audio. **Critères** : les 3
  gestes utilisateurs de base (corriger un mot, couper+réattribuer, retimer) en < 10 s
  chacun au chrono Playwright ; budgets §7.5 sur 1 h. **Réalisé conforme** : banc
  Playwright réel 14/14 (les 3 gestes < 1 s chacun, ouverture 0,6 s, reprise de
  brouillon après rechargement, version v1 dans le pool commun, création de locuteur
  à la volée, zéro erreur JS) ; rendu paresseux natif `content-visibility` (pas de
  virtualisation JS) ; captures revues (compteur d'en-tête figé attrapé et corrigé).
  Piège de banc noté : attendre ~250 ms après un commit avant d'asserter le DOM.
- [x] **Lot C — Timeline** (2026-07-03, banc réel 6/6 + captures revues) : fresque canvas, lanes locuteurs (lasso au lot D), bande zoomée
  waveform, minimap, marqueurs, chevauchements hachurés, `S`/`E`/`G`, zoom.
  **Critères** : 60 fps de scroll/zoom sur le jeu 3 000 chunks ; lasso → sélection
  multiple exacte. **Réalisé** : bascule fresque ↔ lanes (UN contrôle, arbitrage
  utilisateur), zoom ÉVIDENT (boutons − / Ajuster / + + molette), cadre de fenêtre sur
  la fresque, forme d'onde par pics serveur (202→200), poignées de retiming glissables
  sur le segment actif, repères M (chips cliquables, persistés au brouillon),
  chevauchements hachurés (fresque) + pastille sur les cartes, G = aller au temps,
  clic-lane → prise de parole la plus proche du locuteur. Lasso et perf 3 000 chunks
  → mesurés au lot D avec la sélection multiple.
- [ ] **Lot D — Modes puissance** : relecture continue + jauge, recherche, points à
  vérifier cliquables (+ `review_points_anchors.json` côté QualityReporter), écoute
  solo, barre d'actions de sélection multiple + création de locuteur, décalage en
  cascade. **Critères** : scénario « réparer une diarisation mélangée » complet au
  walkthrough ; ancres qualité cliquées → chunk calé.
- [x] **Lot E — Intégrations & ménage** (2026-07-03, walkthrough 41/41) : liens (résultats, wizard, points qualité),
  retrait du fork (§8), walkthrough étendu, docs (READMEs, TECHNICAL, DATA_MODEL,
  CONFIG_REFERENCE, AGENTS, présentation direction), CHANGELOG. **Critères** : plus
  aucune référence au fork hors CHANGELOG ; parcours complet capturé. **Réalisé** :
  fork retiré intégralement (UI wizard/résultats, route push-to-editor, module
  SrtEditorLink, clés de config — dépréciation douce : ignorées avec warning,
  retrait définitif à la 0.2.0) ; l'étape Export du wizard pointe vers l'éditeur
  intégré ; walkthrough CI +3 checks (atelier sans-audio, texte-champ, version) ;
  bonus né d'un retour utilisateur réel : **cache-busting des statiques**
  (`asset_url`, ?v=mtime) — les navigateurs ne servent plus de CSS/JS périmés.

Fin de chantier : **E2E GPU réel** (job réel → scénarios des 3 personas → version →
DOCX/SRT téléchargés conformes, stats recalculées visibles dans le Word → restauration)
+ **session avec tes secrétaires** avant push (leur métier, leur verdict).

---

## 11. Stratégie de test

| Niveau | Quoi |
|---|---|
| Unitaires | round-trip À L'OCTET (SRT réels de dev) ; préfixes dégradés (sans nom, sans préfixe, accents) ; validate sur chevauchements réels ; compute_stats vs stats existantes ; pics (durées 1 min/4 h 30, mono/stéréo) |
| API | state (readonly, audio absent) ; draft (revision 409, conflit base_srt_sha256) ; save (422 forme, snapshot 3 fichiers, purge brouillon, audit sans contenu) ; stream (206, 404 purgé) ; peaks (202→200, cache) ; RBAC complet |
| UI (walkthrough CI, GPU-free, job seedé SANS audio) | ouverture, édition texte, couper, réattribuer, version, reprise de brouillon, bandeau sans-audio |
| Captures | tous les états §7.2/§7.5, à chaque lot |
| Perf | jeu synthétique 3 000 chunks / 17 locuteurs : budgets §7.5 chronométrés en CI (tolérance machine) |
| E2E GPU réel | scénarios personas sur audio réel (dont solo + réattribution en masse + Word final conforme) |

---

## 11 bis. Retours du premier test utilisateur (2026-07-03) — traçabilité

| Retour | Traitement |
|---|---|
| ▶ d'un chunk sans pause (reclic = redémarrage) | **Corrigé immédiatement** : reclic = pause, clic suivant = reprise où on en était (validé Playwright) |
| La fresque « reste en haut » en scrollant | **Corrigé immédiatement** : lecteur + fresque COLLANTS sous la barre (validé Playwright) |
| Déplacer un chunk sélectionné (drag) ? | **Décision : NON en v1** — l'ordre du SRT EST le temps ; « déplacer » un chunk = changer ses timestamps (tiroir ⏱, `S`/`E`), jamais sa position dans la liste. Un tri-par-temps à la sauvegarde est envisageable si le retiming croise des voisins (v2) |
| « La gestion du copier-coller n'est pas bonne » | **À préciser avec l'utilisateur** : le copier-coller de TEXTE dans un chunk fonctionne (collage brut forcé) ; le couper-coller de CHUNKS entiers n'existe pas (geste équivalent : ✂/⧉ + lot D sélection multiple). Clarifier le geste attendu avant de concevoir |
| Fusionner une SÉLECTION de chunks (pas seulement le précédent) | **Lot D confirmé** : sélection multiple (shift-clic, lasso) → barre flottante « Fusionner / Attribuer à… / Supprimer » |
| Bascule fresque ↔ timeline par locuteur | **Validé, lot C** : un seul contrôle alterne fresque compacte / lanes par locuteur (repliables), plutôt que d'empiler les deux |
| Fresque très serrée → sur plusieurs lignes ? | **Avis : non** — une fresque multiligne casse la continuité temporelle du clic-navigation ; la réponse est la BANDE ZOOMÉE (lot C) + les lanes + la minimap. À revisiter si le zoom ne suffit pas à l'usage |

## 12. Risques & points ouverts

| # | Point | Position |
|---|---|---|
| P0 | **Audio purgé post-complétion en split** (§1.6) | Mode dégradé de première classe (A1) — décision à confirmer |
| P1 | `contenteditable` (collage riche, IME, undo natif parasite) | collage texte brut forcé ; `beforeinput` maîtrisé ; repli `<textarea>` auto-dimensionné si les tests IME du lot B échouent — décision AU lot B, pas après |
| P2 | Édition pendant un tour d'affinage LLM | lecture seule le temps du tour (signal `busy` déjà poll é par la page résultats) + conflit détecté par `base_srt_sha256` au brouillon |
| P3 | Lanes/fresque à 17 locuteurs × 3 000 chunks | canvas obligatoire (pas de DOM par bloc), redraw sur rAF — validé par le jeu de perf |
| P4 | Ancrage des points à vérifier (strings libres existants) | jumeau `anchors` en AJOUT (compat totale), matching texte heuristique, jamais bloquant |
| P5 | Relance d'un traitement après éditions manuelles (A3) | avertissement avant relance si versions d'édition présentes ; versions = filet ; documenté utilisateur |
| P6 | Undo/redo vs brouillon serveur (le brouillon capture un état, pas la pile) | assumé : la pile d'annulation est locale à la session ; le brouillon restaure l'ÉTAT — affiché honnêtement dans la carte de reprise (« 37 modifications ») |
| P7 | Deux SRT très différents brut/corrigé (édition du brut sur profil léger puis correction LLM relancée) | même réponse que P5/A3 : le corrigé est LA sortie ; avertissement + versions |
| P8 | Deux frontales éditent le même job (split multi-web) : le `revision` du brouillon est vérifié contre un fichier local potentiellement en retard de pull | fenêtre de course ≈ throttle du pull, perte bornée au dernier debounce (~5 s) ; assumé v1, documenté — verrou partagé en base = v2 si le besoin apparaît |

---

*Références : `docs/TYPES_REUNION_PERSONNALISES.md` (rituel), fork
`/home/admin_ia/srt-editor-pro-fr-easy` (autopsie §1.3), `workflow/refine_store.py`
(versions), `workflow/runner.py:1860` (garde), `docs/archive/RELEASE_0.2.0.md` (contexte).*
