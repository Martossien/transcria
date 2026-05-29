# TranscrIA — Présentation utilisateur et direction

## Synthèse

TranscrIA est un portail web de transcription et de valorisation de réunions. Il transforme un enregistrement audio ou vidéo en livrables exploitables : transcription SRT, résumé structuré, contexte de réunion, participants, lexique métier, rapport qualité et package ZIP final.

l'objectif est de proposer une interface simple à des utilisateurs non techniciens, tout en orchestrant en arrière-plan des traitements avancés : transcription automatique Cohere/Whisper selon la qualité audio, détection des locuteurs, correction par IA, prise en compte du vocabulaire métier et contrôle qualité.

Le projet vise un usage professionnel : comptes rendus de réunion, préparation de relecture, archivage, sous-titrage, analyse de contenus audio et sécurisation d'un processus qui serait sinon long, manuel et difficile à homogénéiser.

---

## Pourquoi TranscrIA

Les réunions produisent beaucoup d'information, mais cette information reste souvent difficile à exploiter :

- les enregistrements sont longs à réécouter ;
- les comptes rendus dépendent fortement du temps disponible et de la personne qui les rédige ;
- les noms propres, acronymes, projets internes et termes métier sont souvent mal reconnus par les outils de transcription classiques ;
- l'identification des intervenants demande une relecture attentive ;
- les livrables produits ne sont pas toujours homogènes d'une réunion à l'autre.

TranscrIA répond à ce problème avec un workflow guidé. L'utilisateur dépose un fichier, complète ou valide les informations importantes, puis récupère un package final prêt à relire, corriger, transmettre ou archiver.

---

## Publics cibles

### Utilisateurs opérationnels

Les utilisateurs principaux sont les personnes chargées de produire, relire ou exploiter des comptes rendus :

- secrétaires de réunion ;
- assistants de direction ;
- chefs de projet ;
- responsables d'équipe ;
- utilisateurs métiers ayant besoin d'une transcription exploitable.

L'interface est pensée pour éviter les manipulations techniques : pas de ligne de commande, pas de gestion manuelle des modèles, pas de fichiers intermédiaires à assembler.

### Direction et pilotage

Pour une direction, TranscrIA apporte un cadre :

- standardiser la production des livrables de réunion ;
- réduire le temps passé sur la transcription brute ;
- améliorer la qualité des comptes rendus grâce au lexique métier ;
- conserver une traçabilité des traitements ;
- administrer les accès par rôles ;
- maîtriser les données dans une infrastructure locale.

---

## Ce Que Produit Le Portail

À la fin d'un traitement, TranscrIA peut fournir :

| Livrable | Utilité |
|---|---|
| Transcription SRT | Sous-titres horodatés, utilisables dans un éditeur SRT ou un lecteur compatible |
| SRT corrigé | Version enrichie par correction IA : locuteurs, lexique, orthographe |
| Résumé structuré | Synthèse de contrôle, sujets abordés, participants probables, termes suspects |
| Contexte de réunion | Métadonnées saisies ou suggérées : titre, type, objectif, sujet, notes |
| Participants | Liste des personnes attendues ou identifiées |
| Mapping locuteurs | Association entre `SPEAKER_XX` et les participants réels |
| Lexique de session | Termes métier, acronymes, noms propres et corrections attendues |
| Rapport qualité | Points à vérifier : segments vides, trous temporels, locuteurs non mappés, couverture audio, diagnostic ASR/VAD |
| **Rapport Word (.docx)** | **Document professionnel prêt à distribuer : page de garde, contexte validé, tableau participants avec temps de parole, transcription formatée, points à vérifier. Téléchargeable directement ou inclus dans le ZIP.** |
| Package ZIP | Ensemble des fichiers utiles pour archivage, relecture ou transmission (inclut le rapport Word) |

---

## Parcours Utilisateur Dans La WebUI

La WebUI guide l'utilisateur en 9 étapes. Chaque étape correspond à une action claire, avec un état visible du traitement.

### 1. Connexion

L'utilisateur se connecte avec un compte applicatif. Les permissions dépendent du rôle attribué :

- `viewer` : consultation et téléchargement ;
- `operator` : création de traitements et accès aux rapports qualité ;
- `manager` : droits étendus de suivi et retry ;
- `admin` : administration, suppression, configuration et système.

### 2. Création d'un traitement

Depuis la page d'accueil, l'utilisateur crée un nouveau traitement et lui donne un titre. Ce titre est conservé pendant le workflow, sauf si le job est créé avec le titre par défaut, auquel cas le nom du fichier peut servir de base lisible.

### 3. Upload du fichier

L'utilisateur dépose un fichier audio ou vidéo. Les formats acceptés sont configurables, avec les formats courants prévus par défaut :

- `.mp3`
- `.wav`
- `.m4a`
- `.mp4`
- `.flac`
- `.ogg`

Le portail vérifie le format avant de lancer le traitement.

### 4. Analyse du média

TranscrIA analyse le fichier avec `ffprobe` :

- durée ;
- codec ;
- nombre de canaux ;
- fréquence d'échantillonnage ;
- besoin éventuel de conversion ;
- estimation du temps de traitement.

Cette étape donne à l'utilisateur une première vision de la complexité du fichier.

### 5. Résumé de contrôle

Le système lance une transcription rapide avec Cohere ASR, applique un VAD adaptatif, puis peut exécuter pyannote pour détecter les locuteurs. Les diagnostics produits orientent les contrôles qualité et peuvent déclencher un forçage backend uniquement si cette règle est explicitement configurée. Ces informations alimentent ensuite un résumé structuré généré via opencode et la LLM d'arbitrage configurée.

Le résumé aide l'utilisateur à comprendre rapidement le contenu avant de compléter les champs métier :

- titre suggéré ;
- type de réunion ;
- sujet principal ;
- objectif probable ;
- participants probables ;
- termes suspects ou vocabulaire à surveiller.

### 6. Contexte de réunion

L'utilisateur valide ou corrige les informations proposées :

- titre ;
- date ;
- type de réunion ;
- langue ;
- service ;
- sujet ;
- objectif ;
- notes ;
- niveau de sensibilité.

Cette étape permet de contextualiser la correction IA et les exports.

### 7. Participants et locuteurs

La WebUI présente les locuteurs détectés par pyannote, par exemple `SPEAKER_00`, `SPEAKER_01`, avec des indicateurs utiles :

- temps de parole ;
- nombre de tours de parole ;
- extraits audio d'écoute quand disponibles ;
- champs de correspondance vers un participant réel.

L'utilisateur peut associer chaque locuteur à une personne ou compléter les participants manquants.

### 8. Lexique de session

L'utilisateur peut définir les termes à surveiller ou à corriger :

- noms propres ;
- acronymes ;
- projets ;
- applications ;
- services ;
- termes techniques ou métier ;
- variantes mal reconnues ;
- forme corrigée attendue.
- contextes proposés par l'IA, avec écoute audio courte pour valider les cas douteux.

Cette étape est essentielle pour les environnements métiers où un nom ou un acronyme mal transcrit peut changer le sens du compte rendu.

### 9. Traitement final, qualité et export

Le traitement final produit la transcription complète, applique les locuteurs, lance la correction IA, exécute les contrôles qualité et construit le package ZIP.

Selon le mode choisi, le pipeline peut inclure une diarisation supplémentaire et une correction plus approfondie. Le résultat final est téléchargeable depuis l'interface.

---

## Administration

La WebUI intègre plusieurs fonctions d'administration.

### Gestion des utilisateurs

Un administrateur peut :

- créer des comptes ;
- modifier les informations utilisateur ;
- changer les rôles ;
- désactiver un compte ;
- réinitialiser un mot de passe.

Les utilisateurs connectés peuvent aussi changer eux-mêmes leur mot de passe depuis la barre de navigation. En cas d'oubli, la procédure actuelle passe par une réinitialisation par l'administrateur ; le reset par email est volontairement reporté tant que l'infrastructure mail et les tokens temporaires ne sont pas configurés.

### Gestion de la configuration

Une page d'administration permet d'éditer la configuration YAML de l'application.

Cette configuration couvre notamment :

- URL des services externes ;
- chemins de stockage ;
- paramètres des modèles ;
- options du workflow ;
- politique de suppression ;
- extensions autorisées.

Les secrets sensibles comme le mot de passe initial admin sont masqués dans l'interface. Certains paramètres nécessitent un redémarrage complet pour être pris en compte, notamment le port serveur et l'URL de base de données.

### État système

La page système permet de consulter les informations remontées par le dashboard LLM :

- état CPU/RAM ;
- état GPU ;
- services ;
- processus GPU.

Cette vue aide à diagnostiquer les traitements longs et les contraintes de mémoire vidéo.

---

## Sécurité Et Gouvernance

TranscrIA applique plusieurs principes de gouvernance :

- authentification obligatoire ;
- mots de passe hashés ;
- rôles et permissions ;
- cloisonnement des jobs par propriétaire ;
- accès complet réservé aux administrateurs ;
- extensions de fichiers contrôlées ;
- limite d'upload configurée ;
- suppression des jobs conditionnée par une permission et une configuration ;
- purge des anciens jobs terminaux selon une durée de rétention.

Le MVP est conçu pour un déploiement local ou maîtrisé. Les modèles et les traitements lourds sont orchestrés sur l'infrastructure serveur, ce qui limite la dépendance à des services externes non contrôlés.

---

## Architecture Fonctionnelle Simplifiée

```text
Utilisateur
  ↓
WebUI TranscrIA
  ↓
Workflow guidé en 9 étapes
  ↓
Analyse audio + transcription + diarisation + correction IA
  ↓
Contrôles qualité
  ↓
Package final
```

En arrière-plan, TranscrIA coordonne plusieurs briques :

- Flask pour le portail web ;
- SQLite pour la base applicative ;
- Cohere ASR pour la transcription ;
- pyannote pour la diarisation ;
- LLM d'arbitrage via opencode pour le résumé et la correction ;
- un dashboard LLM pour surveiller les ressources GPU ;
- SRT Editor EASY pour la relecture externe si activée.

---

## Apports Pour Une Direction

### Gain de temps

TranscrIA automatise la partie la plus longue : obtenir une première transcription horodatée et exploitable. L'utilisateur se concentre ensuite sur la validation, la correction métier et la qualité finale.

### Homogénéisation

Le workflow impose une structure commune :

- contexte ;
- participants ;
- lexique ;
- transcription ;
- qualité ;
- export.

Cela facilite la comparaison, l'archivage et la transmission des livrables.

### Qualité métier

Le lexique de session permet de corriger les termes critiques qui sont souvent les plus importants :

- noms de personnes ;
- noms de projets ;
- acronymes internes ;
- applications ;
- termes techniques.

### Traçabilité

Chaque traitement possède un état, un propriétaire, des fichiers produits et un package final. Les erreurs sont conservées dans l'état du job et les rapports qualité documentent les points à vérifier.

### Maîtrise opérationnelle

La gestion GPU, le lancement des modèles et les étapes techniques sont masqués à l'utilisateur. L'administration garde toutefois une visibilité sur l'état système et les paramètres du portail.

---

## Limites Actuelles

Le MVP est fonctionnel, mais certaines limites restent importantes à connaître lors d'une présentation :

- la qualité finale dépend de la qualité de l'audio d'origine ;
- les noms propres et acronymes doivent souvent être validés dans le lexique ;
- l'identification automatique des locuteurs reste une aide, pas une vérité absolue ;
- la correction IA doit être relue avant diffusion officielle ;
- certains paramètres de configuration nécessitent un redémarrage ;
- les traitements longs dépendent fortement de la disponibilité GPU ;
- le mode sans authentification n'est volontairement pas supporté.

Ces limites sont connues pour un outil de production assistée : l'objectif n'est pas de supprimer la relecture humaine, mais de réduire fortement le travail préparatoire et de rendre la relecture plus fiable.

---

## Axes D'amélioration Identifiés

Les prochaines évolutions peuvent renforcer la précision et l'expérience utilisateur.

### Amélioration de la transcription

Améliorations apportées :

- filtrage VAD Silero en pré-transcription pour supprimer les segments générés sur silence ou bruit (phase summary) ;
- chunking par tours de parole pyannote (`exclusive_speaker_diarization`) pour la transcription finale, attribution locuteur 100 % fiable ;
- fallback 30s transparent si pyannote indisponible ;

Améliorations restantes :

- rapprochement avec le découpage natif recommandé par Cohere ;
- tests A/B sur des réunions longues.

### Chunking basé sur les tours de parole

**Implémenté.** La transcription finale utilise les tours pyannote exclusifs pour découper l'audio par locuteur. Résultat :

- segments naturellement mono-locuteur ;
- timestamps hérités du découpage pyannote ;
- attribution `SPEAKER_XX` 100 % fiable (pas d'overlap matching) ;
- meilleure lisibilité dans l'éditeur SRT.

### Expérience utilisateur

Des améliorations possibles côté WebUI :

- retours de progression plus détaillés pendant les étapes longues ;
- aide contextuelle sur les champs sensibles ;
- vue de comparaison entre SRT brut et SRT corrigé ;
- validation assistée des termes suspects ;
- tableau de bord de suivi des traitements.

---

## Message De Présentation Court

TranscrIA est un portail web qui transforme un enregistrement de réunion en livrables exploitables : transcription, résumé, participants, lexique, rapport qualité et package final. L'utilisateur suit un workflow simple, tandis que l'application orchestre automatiquement la transcription, la diarisation, la correction IA et la gestion GPU. Pour une organisation, le projet apporte un gain de temps, une homogénéisation des comptes rendus, une meilleure prise en compte du vocabulaire métier et une gouvernance des traitements via rôles, traçabilité et administration.

---

## Conclusion

TranscrIA démontre qu'il est possible de rendre un pipeline IA complexe accessible depuis une interface web claire. Le projet ne remplace pas la validation humaine, mais il accélère fortement la production d'une base fiable : transcription horodatée, résumé structuré, locuteurs, lexique, contrôle qualité et package complet.

Pour une direction, la valeur se situe dans la standardisation, la réduction du temps de traitement, la maîtrise locale des données et la possibilité de faire évoluer progressivement le niveau de qualité.


### Qualité audio et transcription renforcée

Le workflow garde Cohere comme backend principal par défaut. Le mode qualité active surtout le traitement complet : diarisation, correction, contrôles renforcés et package final. Whisper large-v3 reste disponible pour des tests, fallbacks ou campagnes ciblées, mais n'est plus assimilé automatiquement à la meilleure qualité.
