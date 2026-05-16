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

## Correctifs

Les correctifs de sécurité doivent être accompagnés de :
- tests de non-régression
- mise à jour documentaire si le comportement attendu change
- note de changement dans `CHANGELOG.md`
