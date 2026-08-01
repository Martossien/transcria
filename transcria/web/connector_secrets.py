"""Dépôt des fichiers d'identités de connecteur téléversés depuis l'interface.

POURQUOI CE MODULE : certaines identités ne sont pas une chaîne mais un FICHIER — la clé
JSON d'un compte de service Google, par exemple. Deux façons de la fournir cohabitaient,
toutes deux mauvaises pour l'administrateur :

  · coller le contenu dans le formulaire → la CLÉ PRIVÉE atterrit dans `config.yaml`,
    c'est-à-dire dans le répertoire du dépôt, lisible par tout ce qui lit la configuration ;
  · saisir un chemin à la main → suppose un accès shell à la machine, ce que la règle
    « l'administrateur ne touche que l'interface » exclut.

Le téléversement résout les deux : le fichier est écrit hors configuration, en 0600, et
c'est son CHEMIN qui est stocké. La validation est faite ICI, à l'arrivée, plutôt qu'au
premier appel réseau : télécharger le mauvais JSON depuis la console Google (le fichier
« client OAuth » au lieu de la clé de compte de service) est l'erreur la plus banale, et
elle se manifesterait sinon des jours plus tard par un refus d'authentification obscur.

Sans état et sans Flask : testable tel quel.
"""
from __future__ import annotations

import json
from pathlib import Path

# Un fichier d'identités reste petit (une clé de compte de service ≈ 2,3 Kio). Le plafond
# n'est pas une optimisation : il empêche qu'un fichier choisi par erreur — un enregistrement,
# une archive — soit recopié dans le répertoire d'instance.
MAX_CREDENTIAL_BYTES = 64 * 1024

SECRETS_DIRNAME = "connector_secrets"


class CredentialFileError(ValueError):
    """Fichier d'identités refusé — le message est destiné à l'administrateur."""


def secrets_dir(instance_path: str | Path) -> Path:
    """Répertoire des fichiers d'identités, créé en 0700 au besoin."""
    directory = Path(instance_path) / SECRETS_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    return directory


def validate_json_credential(raw: bytes, expects: tuple[str, ...] = ()) -> dict:
    """Contrôle un fichier d'identités JSON. Rend l'objet ; lève `CredentialFileError`.

    `expects` nomme les clés que le fichier DOIT porter (données par le catalogue) : c'est
    ce qui distingue une clé de compte de service d'un fichier de client OAuth, que la
    console Google propose au téléchargement à deux clics l'un de l'autre.
    """
    if not raw:
        raise CredentialFileError("fichier vide")
    if len(raw) > MAX_CREDENTIAL_BYTES:
        raise CredentialFileError(
            f"fichier trop volumineux ({len(raw) // 1024} Kio) — une clé d'identités "
            f"en fait quelques-uns ; fichier choisi par erreur ?")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CredentialFileError(f"ce n'est pas du JSON lisible ({exc.__class__.__name__})") from exc
    if not isinstance(data, dict):
        raise CredentialFileError("objet JSON attendu")
    manquantes = [cle for cle in expects if not data.get(cle)]
    if manquantes:
        raise CredentialFileError(
            f"clés absentes : {', '.join(manquantes)} — mauvais fichier ? "
            f"(la clé d'un COMPTE DE SERVICE, pas un identifiant client OAuth)")
    return data


def store_json_credential(instance_path: str | Path, connector_id: str, key: str,
                          raw: bytes, expects: tuple[str, ...] = ()) -> Path:
    """Valide puis dépose le fichier en 0600. Rend le chemin à stocker en configuration.

    Le nom est DÉRIVÉ du connecteur et de la clé, jamais du nom d'origine téléversé : un
    nom fourni par le navigateur n'a pas à décider d'un chemin sur le serveur.
    """
    validate_json_credential(raw, expects)
    cible = secrets_dir(instance_path) / f"{connector_id}-{key.lower()}.json"
    cible.write_bytes(raw)
    cible.chmod(0o600)
    return cible
