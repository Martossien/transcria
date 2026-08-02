"""Garde des scripts déclarés en configuration — passe sécurité S1.6.

Trois valeurs de configuration désignent un fichier que TranscrIA lance avec `bash` :
`services.arbitrage_script`, `services.stop_script` et `resource_node.engines[].script`. Le service
tourne en root sur le déploiement documenté, et `/admin/config` propose un mode **YAML
brut** : un administrateur applicatif pouvait donc désigner n'importe quel fichier du
disque et le faire exécuter en root.

Sur le déploiement de référence, cet administrateur EST le propriétaire de la machine — le
trajet ne lui apporte rien. Mais TranscrIA est un projet public : ailleurs, « administrateur
du portail » peut être un rôle métier confié à quelqu'un sans aucun accès système. La
permission `MANAGE_CONFIG` ne doit pas valoir shell root.

**Ce que cette garde N'EST PAS.** Elle ne remplace pas le passage des services en non-root,
qui est la correction propre — mais un chantier d'installation entier (unités systemd,
droits sur `jobs/`, cache des modèles, accès GPU, images Docker) pour un gain nul sur le
déploiement de référence. La racine allowlistée ferme l'essentiel du trajet pour quelques
lignes ; c'est un arbitrage assumé, écrit dans `docs/PASSE_SECURITE_2026-08.md`.

**Ce qu'elle vérifie**, et pourquoi chaque règle existe :

- le chemin **résolu** (liens symboliques suivis) est sous une racine autorisée — vérifier
  le chemin *écrit* laisserait passer `scripts/piege.sh -> /tmp/charge.sh` ;
- c'est un **fichier régulier** qui existe ;
- il n'est **pas dans une zone où l'application écrit** (`workflow.prompts_dir`,
  `storage.jobs_dir`) — voir `_ZONES_INSCRIPTIBLES` ;
- il n'est **pas inscriptible par tous** (`o+w`) : un script que n'importe quel compte de
  la machine peut réécrire est un shell root offert, et la racine ne protégerait de rien.

L'écriture par le **groupe** est tolérée : `775` est courant sur un dépôt d'équipe, et une
garde qui gêne l'exploitant normal finit désactivée — ce qui ne protège plus personne.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

#: Racine implicite : les scripts versionnés du dépôt. C'est là que vivent
#: `launch_arbitrage.sh`, `stop_stt.sh` et les lanceurs de moteurs — donc le défaut couvre
#: toutes les installations, dont aucune ne pose `TRANSCRIA_SCRIPT_ROOTS`.
_RACINE_DEPOT = Path(__file__).resolve().parents[2] / "scripts"

#: Les racines supplémentaires viennent de l'ENVIRONNEMENT, jamais de la configuration.
#:
#: Première version de cette garde : elles étaient lues dans `security.allowed_script_roots`.
#: Défaut de conception — `/admin/config` propose un mode YAML brut, donc l'administrateur
#: applicatif réglait lui-même les bornes censées le contraindre. Il lui suffisait d'ajouter
#: `/tmp` pour retrouver son shell root, et la chaîne était complète : le répertoire des
#: prompts est aussi configurable et leur CONTENU est libre — écrire un fichier choisi, le
#: désigner comme script, autoriser sa racine.
#:
#: Une garde dont l'acteur visé fixe les limites ne protège de rien. L'environnement d'un
#: service (unité systemd, docker-compose) est hors de portée du formulaire d'administration :
#: c'est la bonne place. Séparateur `:`, comme un PATH.
CLE_ENV_RACINES = "TRANSCRIA_SCRIPT_ROOTS"

#: Zones où l'APPLICATION écrit, déclarées en configuration. Un exécutable ne doit jamais
#: y vivre — sinon la racine autorisée ne protège de rien.
#:
#: C'est le maillon qui manquait après le passage à l'environnement. La chaîne complète,
#: montrée par un troisième audit : l'administrateur pose `workflow.prompts_dir` sur
#: `<dépôt>/scripts` (une racine autorisée), enregistre un prompt — le NOM est dans une
#: liste fermée, le CONTENU est libre — puis désigne le fichier obtenu comme script
#: d'arbitrage. Le pré-lancement LLM l'exécute. Déplacer l'allowlist hors de portée de
#: l'admin ne suffisait pas : il pouvait écrire À L'INTÉRIEUR.
_ZONES_INSCRIPTIBLES = (
    ("workflow", "prompts_dir"),     # contenu libre, écrit depuis /admin/config
    ("storage", "jobs_dir"),         # reçoit des fichiers d'utilisateurs
)


class ScriptRefuse(RuntimeError):
    """Chemin de script refusé — le message porte le motif ET le remède."""


def allowed_script_roots(config: dict | None = None) -> list[Path]:
    """Racines autorisées : celle du dépôt, plus celles de l'ENVIRONNEMENT.

    `config` n'est plus lue — le paramètre subsiste pour ne pas casser les appelants, et
    parce que le supprimer masquerait le point important : **ce n'est pas un oubli, c'est
    la correction**. Voir `CLE_ENV_RACINES`.

    Les racines de l'environnement **s'ajoutent** à celle du dépôt : un exploitant qui range
    ses lanceurs ailleurs ne doit pas perdre ceux du dépôt au passage.
    """
    racines: list[Path] = []
    depuis_env = [p for p in os.environ.get(CLE_ENV_RACINES, "").split(":") if p.strip()]
    with_default = [_RACINE_DEPOT, *depuis_env]
    for item in with_default:
        try:
            racines.append(Path(str(item)).resolve())
        except (OSError, RuntimeError):
            continue
    return racines


def safe_script_path(raw: str, config: dict) -> Path:
    """Chemin de script VÉRIFIÉ, prêt à être exécuté — ou ``ScriptRefuse``."""
    brut = str(raw or "").strip()
    if not brut:
        raise ScriptRefuse("chemin de script vide")

    try:
        resolu = Path(brut).resolve()
    except (OSError, RuntimeError) as exc:
        raise ScriptRefuse(f"chemin de script illisible : {brut!r} ({exc})") from exc

    racines = allowed_script_roots(config)
    if not any(resolu == r or r in resolu.parents for r in racines):
        raise ScriptRefuse(
            f"script hors des racines autorisées : {resolu}. Rangez-le sous "
            f"{_RACINE_DEPOT}, ou déclarez sa racine dans la variable d'environnement "
            f"{CLE_ENV_RACINES} du service (PAS dans config.yaml : ce serait laisser "
            f"l'administrateur applicatif fixer ses propres limites)."
        )

    if not resolu.is_file():
        raise ScriptRefuse(f"script introuvable ou non régulier : {resolu}")

    for section, cle in _ZONES_INSCRIPTIBLES:
        brut_zone = (config.get(section, {}) or {}).get(cle)
        if not brut_zone:
            continue
        try:
            zone = Path(str(brut_zone)).resolve()
        except (OSError, RuntimeError):
            continue
        if resolu == zone or zone in resolu.parents:
            raise ScriptRefuse(
                f"script situé dans une zone inscriptible par l'application : {resolu} "
                f"est sous {section}.{cle} ({zone}). Un exécutable ne vit pas là où "
                f"l'application écrit — sinon il suffit d'y déposer un fichier."
            )

    mode = resolu.stat().st_mode
    if mode & stat.S_IWOTH:
        raise ScriptRefuse(
            f"script inscriptible par tous ({oct(mode & 0o777)}) : {resolu}. "
            f"N'importe quel compte de la machine pourrait changer ce que root exécute. "
            f"Corrigez avec : chmod o-w {resolu}"
        )
    return resolu


def executable_script(raw: str, config: dict) -> str:
    """Comme `safe_script_path`, mais rend une chaîne — la forme attendue par `subprocess`."""
    return str(safe_script_path(raw, config))


def script_est_lisible(raw: str, config: dict) -> bool:
    """Vrai si le script passerait la garde. Pour les contrôles (doctor) qui doivent
    signaler sans lever."""
    try:
        chemin = safe_script_path(raw, config)
    except ScriptRefuse:
        return False
    return os.access(chemin, os.R_OK)
