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

## 2. Base légale et minimisation

- **Biométrie vocale** : donnée sensible. Rien ne s'exécute sans action opérateur ; le
  recueil du consentement est obligatoire (`voice_enrollment.consent.require_active_consent`).
  L'audio source de l'empreinte peut être supprimé après calcul
  (`delete_source_audio_after_embedding`).
- **Minimisation** : l'audio original peut être exclu des sauvegardes (`--exclude-audio`)
  et est purgé avec le traitement.
- **Journalisation** : les accès aux données (consultation, téléchargement, édition), les
  connexions et leurs échecs, et le cycle de vie des jetons d'API personnels
  (`token_create`/`token_revoke`) sont tracés (voir la liste des 59 actions dans
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
