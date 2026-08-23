"""Garde vie privée : aucun nom/organisation/application RÉEL (issu des audios de test
réels) ne doit apparaître dans les fichiers versionnés — le dépôt est PUBLIC.

**La liste est stockée en EMPREINTES, jamais en clair.** La version précédente gardait les
tokens en toutes lettres : la garde anti-fuite publiait donc, dans un dépôt public et
depuis le 2026-07-04, exactement ce qu'elle devait protéger. Une empreinte SHA-256 permet
de reconnaître un token sans jamais l'écrire, et personne ne remonte d'une empreinte au
token sans le connaître déjà.

Pour surveiller un nouveau token, calculer son empreinte et coller SEULEMENT celle-ci :

    venv/bin/python -c "import hashlib,sys; print(hashlib.sha256(
        sys.argv[1].strip().lower().encode()).hexdigest())" "Le Token"

Ne jamais commenter un token en clair à côté de son empreinte — la catégorie suffit.

Toute réapparition = CI rouge, avant qu'elle n'atteigne le dépôt public.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

#: Empreintes SHA-256 des tokens relevés dans les enregistrements réels : personnes,
#: organisations, applications et directions du parc informatique, URL interne.
#: Les acronymes génériques et les mots courants en sont volontairement absents : ils ne
#: désignent personne et produiraient des faux positifs.
_DENYLIST_SHA256: frozenset[str] = frozenset({
    "c88c67ee57b834bdeef372afca5a724397db930885b1fd49561dd0b1a3967f6d",
    "bddf7c48d23d7aa03a8027fa692d80f1c0b52f51b4d2f269f7ea7a6f64ffc56a",
    "5f5654cca8cd82641c4715cdd729071533e4899097a6c0ed423fa1bfe0cc9f38",
    "2e8111de49163354ff7814662e4d87d50ba8e16b44887138d7e24f60759c3fda",
    "730bcbec761192486403e642d7d0873f0c8994d21881ec6ff5346df1be992583",
    "cc39ac00d5a5e67e5f0ccf42a9b108d98503cd6beeb708186fcd8a54070793a6",
    "de6949cec2f07b481f2036478ebb6e110fce76c9fa2efa8bd56b1e3723c4c0e8",
    "4167eaa21e1377fb8b8d02f01c8c71153a7c4caa32f849f389e9d09877497252",
    "bcb7c4a8c4954d9aa2bf1d8e9f27df7b3033d84ea6cbb465e7c7910208c24da2",
    "029724022e53423a2a106c7c2b015b4981ccf5b109f0e0130dc6b4523fc8faa5",
    "ef1651ee92c1e38b9ce691e52265020f5432eb50139266cc107a27d49af1258b",
    "99a3610722de37696bf726ceea174e1ab2e6b40d7c3cff16a02656a67033b9a0",
    "822111d3fa2ab61854bb50ac97e4643f3393eb0c24cc1108851db5ce7bb7adf1",
    "165dae3404b3a31fe8d26fb57b592b6643300635c7ae0d9e46d4f3ac3f7a20f2",
    "e4dbe2a1e66f06f43a44ae3c589aa073d66c9f236e16ff6b356d38a2e301e97d",
    "fc925cdb7a7464b1f88128a57890c4abff3134e0d1211f41cb341952a34f6152",
    "62186d3024542c86a654d176f8573feaa4d60e79bf842deb8b0b5d1af0d297dd",
    "8a48c7d885f4a6742c7845f572f70a65cca722b32ef8ad7dab79260fb9ae2a89",
    "7b459103e523d87dc5a138d3ccd7c6e3e35b493e2894afca709c4c257987d170",
    "1ff9917a92fe43ec58cd518f2ce9368beb7642252f3ba1d92dbf1a46029cff75",
    "ce2b36fc16585a54b83dd203b7f9a925a7081c8aa437482dd9fc9cec027e3824",
    "a09c0cfb13adb346d4a0a2e600748dc12f7cf10602effcb13ad5bec1609bdf7a",
    "6089cc161489ea06ea23d6ec634049c029ecb55cb131cf47a4b3287a1af37c83",
    # Sentinelle FICTIVE (« zzz-sentinelle-vie-privee ») : elle ne protège rien, elle
    # prouve que le mécanisme fonctionne encore — sans qu'un vrai token ait à être écrit
    # en clair dans ce fichier pour le tester.
    "18ee98f4aa873dfc5e69b93e25f8a16b6d3505f91fe94ae197c63ccffe2b8b72",
})

#: Fichiers où un token pourrait légitimement apparaître dans un contexte historique
#: neutre (entrées de changelog déjà publiées) — exclus du contrôle.
_ALLOWED_PATHS = {"CHANGELOG.md", "tests/test_no_real_names.py"}

_REPO = Path(__file__).resolve().parent.parent

#: Un token peut faire jusqu'à trois mots (« Prénom Composé Nom », « hôte.domaine »).
_MOT = re.compile(r"[\wÀ-ÿ'’.\-]+", re.UNICODE)
_MAX_MOTS = 3


def _tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=_REPO, capture_output=True, text=True)
    return [f for f in out.stdout.splitlines() if f and not f.startswith("archives/")]


def empreinte(token: str) -> str:
    """Empreinte d'un token, insensible à la casse et aux espaces de bord."""
    return hashlib.sha256(token.strip().lower().encode("utf-8")).hexdigest()


def tokens_candidats(texte: str, max_mots: int = _MAX_MOTS):
    """Groupes de 1 à `max_mots` mots consécutifs — les candidats à hacher.

    Les mots en snake_case sont AUSSI éclatés sur `_` : un token collé dans un
    identifiant (`token_saas_comparison`) échappait à la garde, l'identifiant entier
    ayant sa propre empreinte — constaté le 2026-08-23 dans une archive de docs.
    """
    mots = _MOT.findall(texte)
    for i in range(len(mots)):
        for n in range(1, max_mots + 1):
            if i + n <= len(mots):
                yield " ".join(mots[i:i + n])
    for mot in mots:
        if "_" in mot:
            yield from (part for part in mot.split("_") if part)


def test_aucun_nom_reel_dans_les_fichiers_versionnes():
    files = [f for f in _tracked_files() if f not in _ALLOWED_PATHS]
    hits: list[str] = []
    for rel in files:
        path = _REPO / rel
        if not path.is_file() or path.suffix in (".png", ".jpg", ".ico", ".woff", ".woff2", ".gz"):
            continue
        try:
            texte = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for candidat in tokens_candidats(texte):
            if empreinte(candidat) in _DENYLIST_SHA256:
                # Le token trouvé n'est PAS journalisé : ce message finirait dans un log
                # de CI public. Le fichier suffit à son auteur pour retrouver ce qu'il a
                # écrit — c'est le défaut d'origine qu'on ne reproduit pas.
                hits.append(rel)
                break
    assert not hits, (
        "Nom/organisation/application RÉEL détecté dans un ou plusieurs fichiers versionnés "
        "(dépôt PUBLIC) — anonymiser avec un placeholder fictif :\n  "
        + "\n  ".join(sorted(set(hits)))
    )


def test_la_liste_ne_contient_aucun_token_en_clair():
    """Le défaut d'origine : la garde publiait ce qu'elle protégeait."""
    for ligne in Path(__file__).read_text(encoding="utf-8").splitlines():
        nue = ligne.strip()
        if nue.startswith('"') and nue.endswith('",'):
            valeur = nue.strip('",')
            assert re.fullmatch(r"[0-9a-f]{64}", valeur), (
                "un token en clair figure dans la liste — n'y mettre que des empreintes"
            )


def test_la_garde_attrape_bien_un_token_surveille():
    """Sans ça, la liste pourrait être vide ou le calcul faux sans que personne ne le voie.

    On teste avec la SENTINELLE fictive, jamais avec un vrai token : écrire un vrai token
    ici, même « pour tester », le publierait — c'est exactement le défaut d'origine.
    """
    texte = "une phrase quelconque contenant zzz-sentinelle-vie-privee au milieu"
    trouve = [c for c in tokens_candidats(texte) if empreinte(c) in _DENYLIST_SHA256]

    assert trouve, "la détection ne reconnaît plus un token pourtant surveillé"
    assert len(_DENYLIST_SHA256) > 20, "la liste d'empreintes a été vidée"


def test_la_garde_attrape_un_token_colle_dans_un_identifiant():
    """Le trou du 2026-08-23 : un token dans `token_saas_comparison` passait, l'identifiant
    snake_case entier ayant sa propre empreinte. La sentinelle rejoue ce cas exact."""
    texte = "voir [[zzz-sentinelle-vie-privee_saas_comparison]] pour le detail"
    # `-` fait partie de _MOT : le mot capturé est l'identifiant entier ; seul
    # l'éclatement sur `_` isole la sentinelle.
    trouve = [c for c in tokens_candidats(texte) if empreinte(c) in _DENYLIST_SHA256]

    assert trouve, "un token collé dans un identifiant snake_case échappe à la garde"
