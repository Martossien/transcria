# Registre des traitements de données (côté produit)

> Chantier C3.10 (docs/archive/RELEASE_0.2.0.md). Document destiné au DPO : quelles données
> TranscrIA conserve, où, combien de temps, qui y accède, et comment elles sont purgées.
> Complète `docs/SECURITY_MODEL.md` (accès) et `docs/UPGRADE.md` (sauvegarde).

## 1. Données traitées et rétention

| Donnée | Emplacement | Rétention par défaut | Clé de configuration |
|---|---|---|---|
| Audio original de réunion | `jobs/<id>/input/` | 365 j (avec le traitement) | `security.retention_days` |
| Livrables (SRT, DOCX, ZIP) | `jobs/<id>/` | 365 j (avec le traitement) | `security.retention_days` |
| Brouillons de l'éditeur | `jobs/<id>/metadata/` | 365 j (avec le traitement) | idem |
| Empreintes vocales (biométrie) | `voices/` | tant que le sujet existe | gestion manuelle (voir §3) |
| Preuves de consentement RGPD | `voices/` | avec l'empreinte | `voice_enrollment.consent` |
| Pistes audio PAR PARTICIPANT | `jobs/<id>/` | 365 j (avec le traitement) | `security.retention_days` |
| Tours de parole en direct | `jobs/<id>/live/captions.jsonl` | 365 j (avec le traitement) | plafonné par `connectors.meetings.max_caption_lines` |
| Référence de réunion et code d'accès | base, **chiffrés** (Fernet) | avec la session | `TRANSCRIA_MEETING_REF_KEY` |
| Journal d'audit | base | 1095 j (par famille) | `security.audit_retention_days` / `audit_retention_by_family` |
| Comptes utilisateurs | base | tant que le compte existe | — |

La purge des **traitements** et de l'**audit** s'exécute automatiquement (au chargement
de la page d'accueil) et peut être forcée en ligne de commande :

```bash
# Compter ce qui serait purgé, sans rien supprimer :
venv/bin/python -m transcria.maintenance.cli purge --dry-run
# Appliquer la politique de rétention :
venv/bin/python -m transcria.maintenance.cli purge
```

Un traitement n'est purgé que dans un **état terminal** (terminé / échoué / annulé) et
au-delà de la rétention ; la purge supprime la ligne en base ET les fichiers du job.

### 1-bis. Connecteurs de réunion (0.4.0) — ce qui change pour le DPO

TranscrIA peut désormais **rejoindre une réunion** ou **récupérer son enregistrement**. Cela
introduit un traitement que ce document doit nommer : des personnes sont enregistrées par un
outil qu'elles n'ont pas installé.

**Transparence — le bot est VISIBLE, par choix.** Il rejoint la réunion sous un nom qui
indique sa fonction *et* qui l'a envoyé (« fonction — initiateur »), il apparaît dans la
liste des participants, et les plateformes affichent leur propre indicateur
d'enregistrement. Un mode discret existe **uniquement pour Visio** (`BOT_HIDDEN=1`, où LiveKit
sait masquer un participant) : **il n'est pas le défaut**, et l'activer relève de la
responsabilité de l'exploitant. Les bots Jitsi et Zoom n'ont pas d'équivalent — ils sont
visibles, sans option contraire. Un enregistrement furtif n'est pas défendable : le produit
ne le facilite pas.

**Google Meet n'envoie personne.** Il récupère, après la réunion, l'enregistrement que la
plateforme a produit à la demande de l'organisateur. Le traitement s'appuie donc sur le
dispositif d'enregistrement *de la plateforme*, avec ses propres avertissements aux
participants.

**Données supplémentaires, et où elles vivent.** Tout ce que produit une réunion atterrit
sous `jobs/<id>/` : les **pistes séparées** (un flux audio par participant, quand la
plateforme le permet) et les **tours en direct** suivent donc la rétention des traitements
et disparaissent avec eux. Les **noms de participants** viennent de la plateforme et servent
à nommer les locuteurs — ils ne sont pas recoupés avec les comptes TranscrIA.

**La référence de réunion et le code d'accès sont chiffrés au repos** (Fernet, clé
`TRANSCRIA_MEETING_REF_KEY`) et ne sont déchiffrés qu'au moment où un exécutant réclame la
session. Sans la clé, le service **refuse de fonctionner** plutôt que de stocker en clair.

**Minimisation.** Les tours en direct sont **provisoires par contrat** : la transcription de
référence est celle du pipeline, et le fichier est plafonné (troncature annoncée). Le
plafond de pistes (`max_tracks`, `max_track_mb`) borne ce qu'une réunion peut déposer.

**Point d'attention à porter au registre :** les participants d'une réunion ne sont pas des
utilisateurs de TranscrIA. Leur information relève de l'organisateur et de la politique de
l'organisation — l'outil rend le bot visible et trace qui l'a envoyé, il ne peut pas recueillir
leur consentement à leur place.

## 2. Base légale et minimisation

- **Biométrie vocale** : donnée sensible. Rien ne s'exécute sans action opérateur ; le
  recueil du consentement est obligatoire (`voice_enrollment.consent.require_active_consent`).
  L'audio source de l'empreinte peut être supprimé après calcul
  (`delete_source_audio_after_embedding`).
- **Minimisation** : l'audio original peut être exclu des sauvegardes (`--exclude-audio`)
  et est purgé avec le traitement.
- **Journalisation** : les accès aux données (consultation, téléchargement, édition), les
  connexions et leurs échecs, et le cycle de vie des jetons d'API personnels
  (`token_create`/`token_revoke`) sont tracés (voir la liste des 60 actions dans
  `audit/models.py`, libellés en français sur la page Audit). Les connexions fédérées
  (OIDC/proxy/LDAP) journalisent la source et le groupe décisif ; un refus de mapping
  journalise les groupes reçus (diagnostic administrateur) — jamais de mot de passe, de
  secret de jeton ni d'email dans les détails d'audit. Événements d'authentification et
  de jetons rangés dans la **famille `auth`** pour la rétention.

## 3. Suppression d'un utilisateur

En 0.2.0, la suppression d'un compte se fait par **désactivation**
(`UserStore.deactivate_user`) : le compte ne peut plus se connecter, mais **ses
traitements, ses empreintes vocales et ses entrées d'audit sont CONSERVÉS** — choix
assumé pour préserver l'intégrité de la piste d'audit et ne pas détruire des livrables
partagés au sein d'un groupe.

Pour un **droit à l'effacement** complet (RGPD art. 17) : désactiver le compte, puis
supprimer manuellement ses traitements (page Traitements) et ses empreintes vocales
(page Voix) ; les entrées d'audit expirent selon leur rétention. Une commande
d'effacement par utilisateur (anonymisation de l'audit incluse) est un candidat 0.2.x.

## 4. Accès aux données

Voir `docs/SECURITY_MODEL.md §1` : seuls les rôles habilités accèdent aux données, un
utilisateur ne voit que les traitements de ses groupes, la page Audit et la page
Système sont réservées aux administrateurs.
