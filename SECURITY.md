# Politique de sécurité

## Signaler une vulnérabilité

Ne publiez pas une faille de sécurité directement dans une issue GitHub publique.

Transmettez plutôt :
- le composant concerné
- les conditions de reproduction
- l’impact estimé
- une proposition de mitigation si vous en avez une

En l’absence d’adresse dédiée documentée, utilisez un canal privé avec le mainteneur du dépôt.

## Périmètre sensible

Les zones suivantes doivent être considérées comme prioritaires :
- authentification, permissions et contrôle d’accès propriétaire sur les jobs
- endpoints d’administration et de supervision
- upload de fichiers audio/vidéo
- voix enregistrées : preuves de consentement consultables par admin autorisé, empreintes vocales, fichiers temporaires dans `voices/`
- appels externes vers `opencode`, `ffmpeg`, `ffprobe` et services LLM
- gestion des chemins disque et des exports ZIP
- exposition de secrets dans `config.yaml`, `.env` ou les logs

## Bonnes pratiques attendues

- ne jamais versionner `config.yaml`
- changer immédiatement le mot de passe admin initial
- fixer `TRANSCRIA_SECRET` en production
- limiter l’accès réseau au service web et aux composants auxiliaires
- exécuter le service sous un utilisateur dédié avec des permissions minimales
- superviser les endpoints `/health`, `/ready` et `/metrics`
- protéger strictement `voices/` au niveau système de fichiers et ne jamais inclure les empreintes vocales dans les exports de jobs
- conserver une preuve de consentement active avant toute vectorisation ou suggestion de voix connue

## Dépendances : vulnérabilités connues (analyse `pip-audit`)

Scan `pip-audit` des dépendances de production (`requirements.txt`) — **2026-06-13**.
Trois CVE recensées, **toutes évaluées à exposition faible** : leur chemin de code
vulnérable n'est **pas** exercé par TranscrIA (vérifié par recherche de code), et le
modèle de menace ne les expose pas (les utilisateurs uploadent de l'**audio**, jamais
des modèles/checkpoints ; les modèles — Cohere ASR, pyannote, demucs — sont installés
par l'opérateur, donc fiables).

| Paquet | CVE | Chemin vulnérable | Exposition TranscrIA |
|---|---|---|---|
| `torch` 2.12.0 | CVE-2025-3000 | `torch.jit.script` (corruption mémoire, attaque locale) | **Nulle** — `torch.jit.script` non utilisé ; **pas de version corrigée** publiée |
| `transformers` 4.57.6 | PYSEC-2025-217 | conversion de checkpoint **X-CLIP** (RCE désérialisation) | **Nulle** — X-CLIP non utilisé (STT uniquement) ; **pas de version corrigée** publiée |
| `transformers` 4.57.6 | CVE-2026-1839 | `Trainer._load_rng_state` → `torch.load()` sans `weights_only` (RCE via checkpoint) | **Nulle** — pas d'entraînement (`Trainer` non utilisé), inférence seule ; corrigé en `5.0.0rc3` |

Faits vérifiés : `torch.jit.script`, `transformers.Trainer`/`_load_rng_state`, la conversion
X-CLIP et `torch.load()` **ne sont appelés nulle part** dans `transcria/` ni
`inference_service/`.

**Décision :** risque **assumé et tracé**. La seule correction disponible (transformers
`5.0.0rc3`) est un **changement de version majeure (release candidate)** qui romprait
probablement l'intégration STT/diarisation — pour une CVE **non exposée**, migrer vers un
RC introduirait **plus** de risque qu'il n'en retire. Les deux autres CVE n'ont **aucune
version corrigée**. À **réévaluer** quand une version stable corrigée paraît, ou si un
chemin vulnérable venait à être introduit.

**Hygiène :** relancer `pip-audit -r requirements.txt` périodiquement (idéalement en CI)
et ré-arbitrer toute nouvelle CVE selon le même critère (chemin de code réellement exercé
× modèle de menace).

## Correctifs

Les correctifs de sécurité doivent être accompagnés de :
- tests de non-régression
- mise à jour documentaire si le comportement attendu change
- note de changement dans `CHANGELOG.md`
