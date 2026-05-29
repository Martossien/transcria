# TranscrIA — Feature Spec : Rapport DOCX de transcription

> **Statut v1 :** ✅ Implémentée et en production (2026-05-29)  
> **Statut v2a :** ✅ Implémentée — Extraction structurée universelle + types de réunion étendus (2026-05-29)  
> **Statut v2b :** 🔵 En discussion — Gestionnaire de templates + mode chat LLM (voir chapitre 11)  
> **Auteur :** Martossien  
> **Date spec :** 2026-05-29 | **Dernière mise à jour :** 2026-05-29  
> **Priorité :** Haute (vitrine utilisateur, valeur perçue immédiate)

---

## 1. Contexte et motivation

À la fin du workflow TranscrIA, l'utilisateur télécharge un fichier ZIP contenant tous les artefacts bruts (SRT, JSON, markdown). Ce format convient aux intégrateurs techniques mais pas aux utilisateurs finaux (secrétaires de réunion, managers) qui ont besoin d'un **document propre, lisible, imprimable et partageable**.

La feature consiste à générer automatiquement un rapport `.docx` de haute qualité, prêt à être distribué, à partir de toutes les données déjà produites par le workflow.

**Objectif vitrine :** ce document est la première chose qu'un décideur voit quand on lui présente TranscrIA. Il doit avoir un **effet "wahou"** — mise en page professionnelle, structure claire, données riches.

---

## 2. Sources de données disponibles (job complet)

Toutes les données nécessaires sont déjà produites par le workflow. Aucune étape supplémentaire de traitement n'est requise.

### 2.1 Contexte de la réunion — `context/meeting_context.json`

| Champ | Rempli par | Description |
|---|---|---|
| `title` | User (modifiable) | Titre de la réunion |
| `date` | User (input date) | Date de la réunion (YYYY-MM-DD) |
| `meeting_type` | User (dropdown) | Type : Réunion interne / Projet / Formation / RH / Entretien / Médicale / Autre |
| `service` | User (texte libre) | Service ou département concerné |
| `language` | User (dropdown) | Langue : fr / en / de / it / es |
| `topic` | User (texte) | Sujet principal |
| `objective` | User (textarea) | Objectifs de la réunion |
| `notes` | User (textarea) | Notes / ordre du jour |
| `summary` | User (textarea éditable) | Synthèse — l'utilisateur peut modifier la version LLM |
| `summary_llm` | LLM (auto) | Synthèse brute générée par la LLM (fallback si `summary` vide) |
| `sensitivity` | API (défaut: "normal") | "normal" ou "high" — déclenche le watermark CONFIDENTIEL |
| `speaker_roles_llm` | LLM (auto) | Rôles par locuteur détectés par la LLM |
| `termes_suspects` | LLM (auto) | Termes STT suspects avec contextes et timecodes |

### 2.2 Participants — `context/participants.json`

Par participant :

| Champ | Rempli par | Description |
|---|---|---|
| `name` | User | Nom du participant |
| `function` | User | Fonction / titre professionnel |
| `service` | Modèle de données (pas encore dans UI) | Service / département du participant |
| `role` | User | Rôle dans la réunion |
| `is_animator` | Hardcodé false (UI) | Animateur de séance |
| `comment` | Modèle de données (pas dans UI) | Commentaire libre |

### 2.3 Statistiques locuteurs — `speakers/speaker_stats.json`

Par locuteur détecté :

| Champ | Source | Description |
|---|---|---|
| `mapped_name` | User (mapping) | Nom humain associé au locuteur |
| `speaking_time_seconds` | Diarisation | Temps de parole en secondes |
| `turn_count` | Diarisation | Nombre d'interventions |
| `gender` | Analyse audio | "male" / "female" |
| `validation` | User | "user_validated" ou "auto" |

### 2.4 Transcription corrigée — `metadata/transcription_corrigee.srt`

Format SRT enrichi avec locuteurs :
```
1
00:00:01,012 --> 00:00:03,910
SPEAKER_01(Vendeur / fromager): Podcast francefacil.com
```
Déjà corrigé orthographiquement, locuteurs nommés, prêt à l'emploi.

### 2.5 Rapport qualité — `quality/quality_report.json`

Données utilisées **de manière sélective** (voir section 4.4) :

| Champ | Utilisé | Condition d'affichage |
|---|---|---|
| `quality_score` | Oui | Toujours (pied de page) |
| `low_coverage.ratio` | Oui | Si ratio < 0.85 |
| `audio_problem_segments` | Oui | Si count > 0, avec timecodes |
| `unresolved_lexicon_variants` | Oui | Si count > 0 |
| `overlaps`, `segment_reliability` | Non | Trop technique |

---

## 3. Structure du document (validée)

### 3.1 Page de garde

```
[Logo organisation — slot template]

COMPTE-RENDU DE TRANSCRIPTION

Titre    : [meeting_context.title]
Type     : [meeting_context.meeting_type]
Date     : [meeting_context.date]
Service  : [meeting_context.service]
Langue   : [meeting_context.language]

[Badge "⚠ CONFIDENTIEL" si sensitivity = "high"]

Généré par TranscrIA — [date_generation]
Score qualité : [quality_score]/100
```

### 3.2 Section 1 — Contexte de la réunion

- Sujet (`topic`)
- Objectif (`objective`)
- Notes / Ordre du jour (`notes`)
- Synthèse narrative (`summary` en priorité, fallback `summary_llm`)

> La synthèse est la version **validée et éditée par l'utilisateur**, pas la sortie LLM brute.

### 3.3 Section 2 — Participants & Locuteurs

Tableau fusionné `participants.json` + `speaker_stats.json` :

| Nom | Fonction | Service | Rôle | Tps parole | Interventions | Animateur |
|---|---|---|---|---|---|---|
| Cliente | — | — | pose des questions… | 23,2s (47,5%) | 15 | — |
| Vendeur / fromager | — | — | propose des produits… | 25,7s (52,5%) | 14 | — |

**Règles d'affichage :**
- Colonne "Service" : affichée seulement si au moins un participant l'a renseignée
- Colonne "Animateur" : affichée seulement si `is_animator = true` pour au moins un participant
- Temps de parole : en secondes + pourcentage du total (calculé à la génération)
- Fusion : on joint participants.json (nom/fonction) avec speaker_stats (temps/tours) via `mapped_to` / `id`

### 3.4 Section 3 — Transcription

Format : `HH:MM:SS  [Nom locuteur]  Texte`

```
00:00:05  Cliente              Fais pas chaud ce matin.
00:00:07  Vendeur / fromager   Non, et ils annoncent rien de bon pour la semaine.
00:00:11  Cliente              Mettez-moi un peu d'émental, s'il vous plaît.
…
```

Source : `transcription_corrigee.srt` parsé et reformaté.  
Le nom entre parenthèses dans le SRT (`SPEAKER_01(Vendeur / fromager)`) est extrait et utilisé.

### 3.5 Section 4 — Points à vérifier *(section conditionnelle)*

**Absente du document si aucun des critères n'est rempli** (document entièrement vert).

Critères d'apparition :

```
⚠ Couverture audio : 79% — possible perte de transcription
   [si low_coverage.ratio < 0.85]

🔍 Zone à réécouter : 00:32 → 00:35 (silence détecté)
   [si audio_problem_segments.count > 0, avec chaque timecode]

✎  Terme à valider : émental (variante : émenteal)
   [si unresolved_lexicon_variants.count > 0]
```

### 3.6 Pied de page (toutes les pages)

```
TranscrIA — [title] — [date] | Score qualité : [score]/100 | Page X/Y
```

---

## 4. Implémentation technique

### 4.1 Dépendance

```
python-docx>=1.1
```
À ajouter dans `requirements.txt`.

### 4.2 Arborescence des fichiers

```
transcria/
  export/
    __init__.py
    docx_report.py          ← moteur de génération
    srt_parser.py           ← parsing transcription_corrigee.srt
    templates/
      default.docx          ← template par défaut (styles, couleurs, logo slot)
      [futur] template_entretien.docx
      [futur] template_formation.docx
      [futur] template_medical.docx
```

### 4.3 Module `transcria/export/docx_report.py`

Interface publique :

```python
def generate_docx_report(job_id: str, job_dir: Path, output_path: Path) -> Path:
    """
    Génère le rapport DOCX pour un job terminé.

    Lit :
      - context/meeting_context.json
      - context/participants.json
      - speakers/speaker_stats.json
      - metadata/transcription_corrigee.srt
      - quality/quality_report.json

    Écrit le fichier .docx dans output_path.
    Retourne le chemin du fichier généré.
    """
```

Logique interne :
1. Charger tous les JSON nécessaires via `JobFilesystem` (existant)
2. Sélectionner le template selon `meeting_context.meeting_type` (fallback: default.docx)
3. Ouvrir le template avec `python-docx`
4. Injecter le contenu section par section
5. Calculer les pourcentages de temps de parole
6. Parser le SRT corrigé
7. Appliquer les règles conditionnelles (sensitivity, section qualité)
8. Sauvegarder

### 4.4 Endpoint API

```
GET /api/jobs/<job_id>/export/docx
```

- Vérifie que le job est dans un état terminal (`done` ou équivalent)
- Génère le fichier si absent, ou le retourne depuis le cache
- Fichier stocké dans `jobs/<job_id>/exports/rapport_<job_id>.docx`
- Réponse : `Content-Disposition: attachment; filename="rapport_<titre>.docx"`

### 4.5 Intégration dans le ZIP export

Ajouter le `.docx` automatiquement dans le ZIP existant :
```
transcrIA_job_<id>.zip
  ├── transcription_corrigee.srt
  ├── rapport_<titre>.docx          ← nouveau
  ├── context/
  ├── quality/
  └── …
```

### 4.6 Bouton UI

Dans la page de fin de workflow (step "export") : bouton **"Télécharger le rapport Word"** en plus du bouton ZIP existant.

---

## 5. Template par défaut — Exigences de qualité

> **Objectif :** effet "wahou" — le document doit impressionner au premier coup d'œil un décideur ou un manager qui découvre TranscrIA.

### 5.1 Palette et typographie

- Couleur principale : bleu professionnel (ex. `#1F3864` ou couleur organisation si template custom)
- Couleur accent : gris anthracite pour les tableaux
- Police titre : Calibri Bold 18pt ou Aptos Display
- Police corps : Calibri 11pt, interligne 1.15
- Police transcript : Consolas 9pt (aspect "compte-rendu officiel")

### 5.2 Éléments de qualité visuelle

- **En-tête page de garde** : bande de couleur pleine en haut, titre blanc sur fond bleu
- **Filets de section** : ligne horizontale colorée avant chaque titre de section
- **Tableau participants** : en-tête de colonne fond bleu / texte blanc, lignes alternées gris clair
- **Badge CONFIDENTIEL** : cadre rouge, texte gras, centré sur la page de garde (visible uniquement si `sensitivity = "high"`)
- **Score qualité en pied de page** : pastille colorée (vert ≥ 85, orange 65–84, rouge < 65)
- **Transcription** : style monospace subtil, timestamp en gris clair, nom locuteur en gras coloré
- **Points à vérifier** : encadré avec fond jaune pâle, icône ⚠ — visuellement distinct mais non alarmiste

### 5.3 Slot logo

Emplacement réservé en haut à droite de la page de garde pour un logo organisation. Dans le template par défaut : placeholder "Votre logo" en gris clair. Un admin peut remplacer `default.docx` par son propre template.

---

## 6. Roadmap future — Gestionnaire de templates

> Ces features sont hors scope de la v1 mais doivent être anticipées dans l'architecture.

### 6.1 Menu de gestion des templates (admin)

Interface d'administration pour gérer les templates disponibles :

```
Admin → Templates de rapport
  ├── [Défaut]  default.docx          ← ne peut pas être supprimé
  ├── [Entretien RH] entretien.docx
  ├── [Formation] formation.docx
  └── [+ Importer un nouveau template]
```

Chaque template est associé à un ou plusieurs `meeting_type`. Lors de la génération du rapport, le système sélectionne automatiquement le template correspondant au type de réunion.

**Stockage :** `instance/report_templates/` (hors git, géré par l'admin).

### 6.2 Chat LLM pour personnalisation du document *(vision future)*

Mode avancé accessible depuis la page de fin de workflow :

```
┌────────────────────────────────────────────────────────┐
│  💬 Personnaliser le rapport avec l'assistant          │
│                                                        │
│  Utilisateur : "Ajoute une section résumé des          │
│  décisions prises en début de document"                │
│                                                        │
│  Assistant : "J'ai ajouté la section 'Décisions'       │
│  après la synthèse. Voulez-vous la reformuler ?"       │
│                                                        │
│  [Télécharger]  [Continuer à modifier]                 │
└────────────────────────────────────────────────────────┘
```

**Fonctionnement envisagé :**
- L'utilisateur décrit en langage naturel les modifications souhaitées
- Le système transmet la demande à la LLM locale configurée (via `services.llm`)
- La LLM utilise le **skill `docx` d'OpenCode** pour modifier le document généré ou le template
- Le document modifié est retourné à l'utilisateur

**Ce que le skill docx permet (OpenCode) :**
- Ajouter / supprimer des sections
- Reformuler du contenu (résumés, titres)
- Changer la mise en page d'une section
- Créer un nouveau template à partir du document courant
- Exporter en PDF depuis le docx

**Dépendance :** nécessite OpenCode installé et le skill `docx` actif dans la configuration OpenCode locale.

### 6.3 Templates par type de réunion — différences structurelles

| Type | Spécificités du template |
|---|---|
| Réunion interne | Standard — tous les champs |
| Entretien / RH | Section "Questions / Réponses" formatée en Q&R |
| Formation | Section "Points clés appris", pas de temps de parole % |
| Réunion médicale | Watermark automatique CONFIDENTIEL, anonymisation partielle |
| Podcast / Média | Pas de tableau participants, transcription prioritaire |

---

## 7. Tests

### 7.1 Tests unitaires (`tests/test_docx_report.py`)

- Génération du fichier sans erreur avec le job de test `8ead05eb-c8f7-4c6e-9694-8c6d9c9dc230`
- Présence de toutes les sections dans le document généré
- Absence de la section "Points à vérifier" quand quality_score ≥ 85 et aucun flag
- Présence du badge CONFIDENTIEL quand `sensitivity = "high"`
- Gestion des champs vides (date vide, service vide, etc.) sans erreur

### 7.2 Test visuel (manuel)

Ouvrir le document généré depuis le job TEST1 (`8ead05eb`) dans LibreOffice / Word et vérifier :
- Rendu visuel de la page de garde
- Tableau participants avec pourcentages calculés
- Formatage de la transcription
- Présence / absence correcte de la section qualité

---

## 8. Données du job de référence pour les tests

Job de test disponible en local : `8ead05eb-c8f7-4c6e-9694-8c6d9c9dc230`  
Titre : "Scène de fromagerie — achat de comté et beurre"  
2 locuteurs (Cliente, Vendeur / fromager), 29 segments, durée ~71s  
Score qualité : 80/100 — déclenche la section "Points à vérifier" (coverage 79%)  
Chemin : `transcria/jobs/8ead05eb-c8f7-4c6e-9694-8c6d9c9dc230/`

Ce job couvre tous les cas : champs validés, deux locuteurs mappés, points qualité actifs.

---

## 9. Décisions d'architecture prises

| Décision | Raison |
|---|---|
| `python-docx` (pas WeasyPrint/ReportLab) | Léger, .docx éditable par l'utilisateur après téléchargement, support template nommé |
| Template `.docx` de base (pas génération from scratch) | L'organisation peut injecter son propre template avec logo/couleurs sans toucher au code |
| Sections conditionnelles (pas de section vide) | Document propre — une section absente est plus professionnelle qu'une section avec "Aucun point" |
| Cache du fichier dans `exports/` | Évite de régénérer à chaque téléchargement ; invalidé si le job est modifié (à implémenter) |
| `summary` > `summary_llm` | Respect de la validation utilisateur — l'user a édité, on publie sa version |
| Pas de mots-clés dans v1 | Les données disponibles (lexique STT, termes suspects) ne sont pas des mots-clés sémantiques ; nécessiterait une extraction Cohere dédiée |
| Pas de `gender` dans le tableau | Redondant si le nom est connu, sensible RGPD |

---

## 10. Fichiers impactés au moment de l'implémentation

```
transcria/export/__init__.py          ← nouveau
transcria/export/docx_report.py       ← nouveau
transcria/export/srt_parser.py        ← nouveau (ou intégré dans docx_report)
transcria/export/templates/default.docx  ← nouveau (asset binaire)
transcria/web/routes.py               ← nouvel endpoint GET /api/jobs/<id>/export/docx
transcria/jobs/filesystem.py          ← éventuellement : helper get_export_path()
requirements.txt                      ← ajouter python-docx>=1.1
tests/test_docx_report.py             ← nouveau
```

Aucune modification des modèles de données existants requise.

---

## 11. Évolutions — Système de templates (v2, en discussion)

> **Statut :** Discussion en cours — 2026-05-29  
> Ce chapitre capture les réflexions et les décisions de conception avant implémentation.  
> Les décisions arrêtées sont marquées ✅. Les points encore ouverts sont marqués ❓.  
> Il sera mis à jour au fil des échanges.

---

### 11.1 Décision d'accès — arrêtée

**Seuls les admins globaux et les admins de groupe peuvent gérer les templates.**

Cohérent avec le modèle d'accès existant (lexiques centralisés, voix enregistrées). Un utilisateur ou opérateur utilise le template associé à son type de réunion, sans pouvoir le modifier.

---

### 11.2 Architecture en 3 couches — contrainte de timing ✅

Le système de templates est en réalité **3 couches interconnectées** qui doivent être cohérentes. Mais elles ne s'activent **pas au même moment du workflow** :

```
Étape 3 — Résumé LLM   ← type de réunion INCONNU à ce stade
Étape 4 — Contexte     ← l'utilisateur valide ou choisit le type
...
Étape 9 — Export DOCX  ← type de réunion CONNU, on peut router
```

```
Type de réunion
    │
    ├── Couche 1 : Extraction LLM  ← Étape 3, AVANT que le type soit connu
    │     Prompt UNIVERSEL — extrait TOUT ce qui pourrait être utile
    │     (décisions, actions, votes, blocages, ODJ...)
    │     → stocké dans meeting_context.json → structured_data
    │     → le type de réunion N'influence PAS ce prompt
    │
    ├── Couche 2 : Structure DOCX  ← Étape 9, APRÈS validation du type
    │     Sections affichées selon le type validé ET les données présentes
    │     (votes CSE → section votes si votes détectés ET type=cse)
    │     → double condition : données non vides + type compatible
    │
    └── Couche 3 : Visual template ← Étape 9, même moment
          Styles, couleurs, logo selon le template associé au type
          → le .docx de base avec la mise en forme
```

**Règle clé :** la couche 1 est invariante (un seul prompt pour tous). Les couches 2 et 3 sont routées par `meeting_type` au moment de la génération DOCX.

**Conséquence heureuse :** si l'utilisateur change le type de réunion en étape 4, les données extraites restent valides — seul le rendu DOCX change. Il n'y a pas besoin de relancer le LLM.

---

### 11.3 Types de réunion — liste élargie ✅ (liste fixe dans le code)

**Décision :** liste fixe dans le code. Les types sont des constantes versionnées dans git, avec leur prompt et leur structure DOCX associés. L'extensibilité par l'admin est reportée en v3 si le besoin se confirme.

Liste actuelle : `Réunion interne | Réunion projet | Réunion technique | Formation | Réunion médicale / santé | RH | Entretien | Autre`

**Extension v2 proposée :**

| Clé code | Libellé UI | Sections DOCX spécifiques activées | Sensibilité auto |
|---|---|---|---|
| `cse` | CSE | ODJ numéroté, votes, résolutions, questions élus | Non |
| `cse_extra` | CSE extraordinaire | Objet de la séance, votes, résolutions | Non |
| `codir` | CODIR / COMEX | Décisions stratégiques, actions, points escalade | Non |
| `reunion_client` | Réunion client | Engagements pris, prochaines étapes | Non |
| `point_projet` | Point projet | Actions (qui/quoi/quand), blocages, reportés | Non |
| `crise` | Réunion de crise | Timeline, causes, actions correctives | Non |
| `seminaire` | Séminaire / atelier | Livrables, groupes de travail, restitutions | Non |
| `negociation` | Négociation | Positions, concessions, accord/rupture | Non |
| `entretien_individuel` | Entretien individuel | Objectifs, retours, plan de développement | `high` auto |
| `podcast` | Podcast / média | Transcription prioritaire, pas de tableau participants | Non |

**Sections universelles** (affichées pour tous les types si données présentes) : `decisions`, `actions`, `prochaine_date`.

**Types existants conservés :** `reunion_interne`, `reunion_projet`, `reunion_technique`, `formation`, `medical`, `rh`, `entretien`, `autre` — comportement inchangé (= template par défaut v1).

---

### 11.4 Prompt d'extraction universel — décisions d'architecture ✅

#### Principe : un seul prompt, format volontairement simple

Le LLM est intrinsèquement variable dans ses réponses. La codebase en a fait l'expérience avec `termes_suspects` : plus le format attendu est complexe (objets imbriqués, clés multiples), plus le parseur casse. La règle arrêtée est donc :

> **Des listes de chaînes simples, pas des objets imbriqués.**

```
❌ Complexe et fragile
"actions": [
  {"responsable": "Alice", "action": "Préparer le budget",
   "délai": "15 juin", "priorité": "haute"}
]

✅ Simple et robuste
"actions": [
  "Alice : Préparer le budget pour le 15 juin",
  "Bob : Valider le planning avant vendredi"
]
```

Le LLM est beaucoup plus stable sur des listes de phrases que sur des objets avec des clés précises. Dans le DOCX, les listes se rendent très bien en bullet points — pas besoin de tableau pour commencer.

#### Prompt universel (ajout au prompt de résumé existant)

```
En plus du résumé, extrais les éléments suivants SI présents dans la transcription.
Réponds UNIQUEMENT en JSON valide. Si un élément est absent ou incertain, retourne [].

{
  "decisions":      ["texte de la décision 1", "texte de la décision 2"],
  "actions":        ["Responsable : action à faire (délai si mentionné)", ...],
  "blocages":       ["description du point bloquant", ...],
  "reports":        ["sujet reporté à une prochaine réunion", ...],
  "votes":          ["Sujet : X pour, Y contre, Z abstentions — résultat", ...],
  "resolutions":    ["texte de la résolution adoptée", ...],
  "points_odj":     ["N. Titre du point — résumé en une phrase", ...],
  "prochaine_date": "JJ/MM/AAAA ou '' si non mentionnée"
}
```

**Champs toujours présents dans le JSON (liste vide si rien détecté) :** assure que le parseur n'a jamais de `KeyError`.

**Stockage :** `meeting_context.json` → champ `structured_data` (dict) + `structured_data_parse_status` (`ok` | `partial` | `failed`) + `structured_data_parse_warning` (string ou null).

#### Sélection des sections dans le DOCX

Le DOCX affiche une section uniquement si **deux conditions** sont remplies :
1. Les données correspondantes sont **non vides** dans `structured_data`
2. Le **type de réunion** rend cette section pertinente

```python
# Exemple de logique dans DocxReport
if structured_data.get("actions") and meeting_type in ACTION_TYPES:
    self._section_actions(doc, structured_data["actions"])

if structured_data.get("votes") and meeting_type in ("cse", "cse_extraordinaire"):
    self._section_votes(doc, structured_data["votes"])

# Certaines sections s'affichent pour tous les types si non vides
if structured_data.get("decisions"):
    self._section_decisions(doc, structured_data["decisions"])
```

Constantes dans le code :
```python
ACTION_TYPES = {"point_projet", "codir", "reunion_client", "crise", "seminaire"}
```

---

### 11.4b Stratégie de robustesse du parseur ✅

Pattern identique à `termes_suspects_parse_status` déjà en production.

#### Pipeline de parsing (3 niveaux de fallback)

```
Réponse LLM
      │
      ▼
[1] json.loads() strict
      │ succès → structured_data complet, status = "ok"
      │ échec ↓
      ▼
[2] Extraction regex champ par champ
      │ (cherche "decisions": [...], "actions": [...] etc. individuellement)
      │ résultat partiel → status = "partial", warning = "champs manquants: votes, resolutions"
      │ échec total ↓
      ▼
[3] Fallback gracieux
      structured_data = {} (tous les champs vides)
      status = "failed"
      warning = "Extraction impossible — réponse LLM non parseable"
```

#### Garantie de non-régression

Le workflow **ne plante jamais** à cause de l'extraction. Dans le pire cas (`status = "failed"`), toutes les nouvelles sections sont simplement absentes du DOCX — le document reste identique au comportement actuel de la v1.

```
status = "ok"      → toutes les sections enrichies disponibles
status = "partial" → seules les sections parsées sont affichées
status = "failed"  → document v1 standard sans enrichissement
```

L'UI (page résultat / page job) peut afficher un badge discret si `status != "ok"` pour informer l'utilisateur sans l'alarmer.

---

### 11.5 Interface de gestion des templates (admin)

Accessible depuis `/admin/templates` (admins globaux) et `/admin/group-templates` (admins de groupe).

```
┌─────────────────────────────────────────────────────────────────┐
│  Templates de rapport                                [+ Nouveau] │
├──────────────────┬──────────────┬──────────────┬───────────────┤
│ Nom              │ Type associé │ Portée        │ Actions       │
├──────────────────┼──────────────┼──────────────┼───────────────┤
│ ★ Défaut         │ (tous)       │ Global        │ [Prévisualiser]│
│ CSE Standard     │ CSE          │ Global        │ [↓] [✎] [🗑] │
│ CODIR Mon Org    │ CODIR        │ Groupe "DIR"  │ [↓] [✎] [🗑] │
│ 1-to-1 RH        │ Entretien    │ Groupe "RH"   │ [↓] [✎] [🗑] │
└──────────────────┴──────────────┴──────────────┴───────────────┘

[+ Importer un template .docx]   [+ Créer depuis anciens CR...]
```

**Règles :**
- Le template par défaut (`★`) ne peut pas être supprimé, seulement remplacé.
- Un admin de groupe ne voit que les templates de ses groupes + les templates globaux.
- La portée détermine quels jobs utilisent le template (groupe ou tous).
- Un template peut être associé à plusieurs types de réunion.

**Stockage :** `instance/report_templates/<id>.docx` + table `report_templates` en base (id, name, meeting_types, group_id, path, created_by, created_at).

---

### 11.6 Mode chat LLM — personnalisation du document (en discussion)

Accessible depuis la page de fin de workflow, pour les utilisateurs ayant généré un rapport.

**Maquette UX envisagée :**
```
┌──────────────────────────────────────────────────────────────────┐
│  ✏️  Personnaliser le rapport avec l'assistant                    │
│                                                                  │
│  [Rapport actuel : rapport_Reunion_Q1.docx]  [Télécharger]       │
│  ──────────────────────────────────────────────────────────────  │
│  💬 Utilisateur : "Ajoute un tableau récapitulatif des           │
│     décisions en page 2, avant la transcription."                │
│                                                                  │
│  🤖 Assistant : "J'ai ajouté le tableau 'Décisions prises'       │
│     après la section Contexte. Il liste 3 décisions extraites    │
│     du résumé. Voulez-vous modifier le titre ou le contenu ?"    │
│                                                                  │
│  [📥 Télécharger cette version]  [Continuer à modifier]          │
│  [💾 Enregistrer comme template]                                 │
└──────────────────────────────────────────────────────────────────┘
```

**Mécanique envisagée :**
1. L'utilisateur décrit la modification en langage naturel
2. TranscrIA prépare le contexte : données du job + instruction + DOCX actuel
3. OpenCode (skill `docx`) lit le fichier, applique la modification, sauvegarde
4. La nouvelle version est proposée au téléchargement

**Questions de design ouvertes :**
- Chat persistant (l'historique de la conversation compte) ou one-shot (chaque instruction est indépendante) ?
- Versionnement du document : modifier en place, ou conserver `rapport_v1.docx`, `rapport_v2.docx`... ?
- Bouton "Enregistrer comme template" : l'utilisateur peut-il promouvoir son document personnalisé en template réutilisable (avec validation admin) ?
- Périmètre des modifications autorisées : contenu uniquement, ou aussi structure et mise en page ?

**Dépendance :** OpenCode installé + skill `docx` actif dans la config OpenCode locale.

---

### 11.7 Mode "apprendre depuis mes anciens CR" (en discussion)

Feature innovante : l'organisation a déjà ses propres formats de compte-rendu. Plutôt que de tout recréer, TranscrIA analyse les documents existants et génère un template adapté.

**Flux envisagé :**
```
Admin upload 3 à 5 anciens Comptes-Rendus (DOCX ou PDF)
      ↓
OpenCode (skill docx) analyse les fichiers :
  - Quelles sections apparaissent dans tous les documents ?
  - Quel format pour les actions, participants, décisions ?
  - Quelle mise en page, quels styles typographiques ?
  - Quels champs sont systématiquement présents ?
      ↓
OpenCode génère :
  - Un template .docx vide avec la structure détectée
  - Une description JSON de la structure (noms des sections, ordre)
  - Un rapport "J'ai trouvé X sections communes : ..."
      ↓
Admin prévisualise → nomme le template → l'associe à un type de réunion
      ↓
Template sauvegardé dans instance/report_templates/
Disponible pour tous les jobs du groupe
```

**Ce qui est déjà en place :**
- OpenCode + skill docx
- Module `DocxReport` existant pour la génération
- Modèle de groupes pour la portée

**Questions ouvertes :**
- Formats supportés en entrée : DOCX uniquement, ou aussi PDF (lecture seule) ?
- Nombre de documents minimum pour une analyse fiable (3 ? 5 ?) ?
- L'admin peut-il corriger / affiner la structure détectée avant de sauvegarder ?
- Audit de l'opération (les documents uploadés sont-ils conservés ou supprimés après analyse) ?

---

### 11.8 Tableau des décisions — état courant

| Question | Décision | Status |
|---|---|---|
| Accès gestion templates | Admin global + admins de groupe uniquement | ✅ Arrêté |
| Types de réunion | Liste fixe dans le code, élargie à 10 nouveaux types | ✅ Arrêté |
| Prompt templates | Fichiers dans `configs/prompts/` — un seul prompt universel | ✅ Arrêté |
| Format réponse LLM | Listes de strings simples, pas d'objets imbriqués | ✅ Arrêté |
| Timing prompt / type | Prompt universel en étape 3, routage DOCX en étape 9 | ✅ Arrêté |
| Robustesse parseur | 3 niveaux de fallback + parse_status + dégradation gracieuse | ✅ Arrêté |
| Portée d'un template | Global ou groupe (pas utilisateur individuel) | ✅ Arrêté |
| Versionnement DOCX mode chat | En place vs versions numérotées | ❓ En discussion |
| Format entrée "apprendre depuis CR" | DOCX seul vs DOCX + PDF | ❓ En discussion |
| Sections universelles vs par type | `decisions` + `actions` + `prochaine_date` pour tous | ✅ Arrêté |
| Comportement si parse échoue | Document v1 standard sans enrichissement — pas d'erreur | ✅ Arrêté |

---

### 11.9 Priorité de livraison suggérée (v2)

Les étapes sont ordonnées par dépendance technique — chaque étape s'appuie sur la précédente.

1. **Extraction universelle** — enrichir le prompt résumé existant, parser `structured_data`, stocker dans `meeting_context.json`, tests unitaires du parseur avec les 3 niveaux de fallback
2. **Types de réunion étendus** dans le dropdown + routing des sections DOCX (couche 2)
3. **Templates CSE et CODIR** — les deux types les plus différenciants et les plus demandés en milieu professionnel
4. **Menu gestion templates** (admin) — import .docx custom, association type/groupe, stockage `instance/report_templates/`
5. **Mode chat LLM** — nécessite que les templates et l'extraction soient stables
6. **Apprentissage depuis anciens CR** — feature avancée, après validation du reste
