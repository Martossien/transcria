"""Passe sécurité S1.1 — le service d'inférence ne doit plus échouer OUVERT.

Deux gardes fail-open cohabitaient dans `inference_service` :

1. `enforce_api_key` était un **no-op** quand aucune clé n'était configurée. Or la clé
   se lit d'abord dans une variable d'environnement : une variable qui disparaît au
   déploiement transformait silencieusement un service authentifié en service ouvert.
   Le service écoute sur `0.0.0.0:8002` — l'inversion n'est pas théorique.
2. `resolve_safe_audio_path` autorisait **n'importe quel chemin** quand
   `inference.allowed_audio_roots` était vide — et cette clé n'a ni défaut ni exemple,
   alors que `file_ref` est le transport par DÉFAUT. Toute installation lisait donc, en
   pratique, sous n'importe quelle racine.

Ces tests échouent sans le correctif : ils sont écrits pour ça.
"""
from __future__ import annotations

import pytest

from inference_service.errors import ForbiddenError
from inference_service.security import (
    InsecureInferenceConfig,
    assert_secure_startup,
    resolve_safe_audio_path,
)

CLE = "cle-partagee-de-test"


# --- 1. La posture d'authentification, décidée au DÉMARRAGE -----------------------------

def test_sans_cle_et_sans_intention_explicite_le_service_refuse_de_demarrer():
    """Le cas dangereux : rien de configuré. Avant, on démarrait ouvert avec un warning
    que personne ne lit dans un journal systemd."""
    with pytest.raises(InsecureInferenceConfig) as exc:
        assert_secure_startup({})
    assert "allow_unauthenticated" in str(exc.value)   # le message dit comment s'en sortir


def test_le_mode_ouvert_reste_possible_mais_doit_etre_DEMANDE():
    """On ne ferme pas la porte du développement — on exige qu'elle soit ouverte exprès."""
    assert assert_secure_startup({"inference": {"auth": {"allow_unauthenticated": True}}}) is None


def test_une_cle_directe_donne_le_mode_authentifie():
    assert assert_secure_startup({"inference": {"auth": {"api_key": CLE}}}) == CLE


def test_une_cle_par_variable_denvironnement(monkeypatch):
    monkeypatch.setenv("CLE_INFERENCE_TEST", CLE)
    cfg = {"inference": {"auth": {"api_key_env": "CLE_INFERENCE_TEST"}}}
    assert assert_secure_startup(cfg) == CLE


def test_variable_declaree_mais_ABSENTE_refuse_meme_avec_le_drapeau(monkeypatch):
    """LE scénario de l'audit : la clé perdue au déploiement.

    Déclarer `api_key_env` dit « ce service est authentifié ». Si la variable manque, la
    configuration se contredit — ce n'est pas du développement, c'est un déploiement
    cassé. Le drapeau de mode ouvert ne doit PAS le couvrir, sinon il suffirait de
    l'avoir laissé traîner pour que la perte de la clé passe inaperçue."""
    monkeypatch.delenv("CLE_INFERENCE_TEST", raising=False)
    cfg = {"inference": {"auth": {"api_key_env": "CLE_INFERENCE_TEST",
                                  "allow_unauthenticated": True}}}
    with pytest.raises(InsecureInferenceConfig) as exc:
        assert_secure_startup(cfg)
    assert "CLE_INFERENCE_TEST" in str(exc.value)


def test_variable_declaree_mais_VIDE_refuse_aussi(monkeypatch):
    """Une variable posée à la chaîne vide est le même accident, en plus discret."""
    monkeypatch.setenv("CLE_INFERENCE_TEST", "   ")
    cfg = {"inference": {"auth": {"api_key_env": "CLE_INFERENCE_TEST"}}}
    with pytest.raises(InsecureInferenceConfig):
        assert_secure_startup(cfg)


def test_la_fabrique_dapplication_applique_la_garde():
    """La garde doit vivre dans `create_app`, pas seulement dans une fonction qu'on
    pourrait oublier d'appeler."""
    from inference_service.app import create_app

    with pytest.raises(InsecureInferenceConfig):
        create_app(config={})


# --- 2. `file_ref` : une borne par défaut au lieu d'aucune borne ------------------------

def test_sans_allowlist_la_borne_par_defaut_est_le_repertoire_des_jobs(tmp_path):
    """Plutôt que refuser (ce qui casserait toutes les installations existantes, `file_ref`
    étant le transport par défaut), on déduit la racine légitime : les jobs."""
    jobs = tmp_path / "jobs"
    (jobs / "abc" / "input").mkdir(parents=True)
    audio = jobs / "abc" / "input" / "reunion.wav"
    audio.write_bytes(b"\0")
    cfg = {"storage": {"jobs_dir": str(jobs)}}
    assert resolve_safe_audio_path(str(audio), cfg) == audio.resolve()


def test_sans_allowlist_un_chemin_hors_jobs_est_refuse(tmp_path):
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    dehors = tmp_path / "ailleurs.wav"
    dehors.write_bytes(b"\0")
    with pytest.raises(ForbiddenError):
        resolve_safe_audio_path(str(dehors), {"storage": {"jobs_dir": str(jobs)}})


def test_la_traversee_reste_refusee_sous_la_borne_par_defaut(tmp_path):
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    secret = tmp_path / "secret.wav"
    secret.write_bytes(b"\0")
    with pytest.raises(ForbiddenError):
        resolve_safe_audio_path(str(jobs / ".." / "secret.wav"),
                                {"storage": {"jobs_dir": str(jobs)}})


def test_une_allowlist_explicite_reste_prioritaire(tmp_path):
    """Le déploiement qui configure ses racines garde exactement son comportement."""
    racine = tmp_path / "partage"
    racine.mkdir()
    audio = racine / "a.wav"
    audio.write_bytes(b"\0")
    cfg = {"inference": {"allowed_audio_roots": [str(racine)]},
           "storage": {"jobs_dir": str(tmp_path / "jobs")}}
    assert resolve_safe_audio_path(str(audio), cfg) == audio.resolve()
