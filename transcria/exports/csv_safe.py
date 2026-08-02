"""Neutralisation des formules dans les exports CSV — passe sécurité S3.

Un tableur (Excel, LibreOffice, Google Sheets) **exécute** une cellule qui commence par
`=`, `+`, `-` ou `@`. Or nos exports contiennent des valeurs saisies par des utilisateurs :
le **titre d'un job** part dans `target_label` de l'export d'audit, les termes et
commentaires dans l'export du lexique central. Un job intitulé ``=cmd|'/c calc'!A1``
devient donc une commande au moment où une secrétaire ouvre le fichier.

Ce n'est pas une faille du portail : c'est le portail qui fabrique l'arme, et un tiers qui
la déclenche sur SON poste. D'où la correction ici, à l'écriture, plutôt qu'un avertissement
que personne ne lira.

**Méthode : préfixer d'une apostrophe**, la convention que tous les tableurs comprennent
comme « ceci est du texte ». La valeur reste entièrement lisible — c'est ce qui distingue
cette approche du remplacement ou de la suppression, qui abîmeraient des libellés légitimes
(« budget -- révisé ») et finiraient par être désactivés.
"""
from __future__ import annotations

#: Caractères qui, EN TÊTE de cellule, déclenchent une interprétation.
_AMORCES = ("=", "+", "-", "@")

#: Blancs qu'un tableur retire AVANT d'interpréter : `"\t=1+1"` s'exécute aussi. Les
#: chercher n'est donc pas de la coquetterie — c'est le contournement évident.
_BLANCS_DE_TETE = "\t\r\n "


def cellule_sure(valeur: object) -> str:
    """Rend la valeur telle quelle, ou préfixée d'une apostrophe si elle serait interprétée.

    Les valeurs ordinaires ressortent **inchangées** : la garde ne doit gêner personne, sinon
    quelqu'un finira par la retirer.
    """
    if valeur is None:
        return ""
    texte = str(valeur)
    if not texte:
        return texte
    if texte.lstrip(_BLANCS_DE_TETE)[:1] in _AMORCES:
        return "'" + texte
    return texte


def ligne_sure(valeurs) -> list[str]:
    """`cellule_sure` sur toute une ligne — la forme attendue par ``csv.writer.writerow``."""
    return [cellule_sure(v) for v in valeurs]
