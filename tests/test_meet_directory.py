"""Adresse → identifiant Cloud Identity : les deux voies, le cache, et le repli.

L'enjeu est une panne qui se tait : un abonnement posé sur un mauvais identifiant est accepté
par Google et ne remonte JAMAIS rien. D'où la prudence sur l'équivalence des deux voies, que
la documentation n'affirme nulle part.
"""
from __future__ import annotations

import pytest

from connector_service.meet_directory import (
    DIRECTORY_SCOPE,
    UserResolutionError,
    UserResolver,
    directory_call,
    explain_failure,
    user_id_of_directory,
    user_id_of_userinfo,
    userinfo_call,
    verify_resolvers_agree,
)

ID = "118427905513870264891"


class TestAppels:
    def test_l_annuaire_accepte_l_adresse_telle_quelle(self):
        """`userKey` accepte l'adresse principale : pas de traduction préalable, donc pas
        d'occasion de se tromper."""
        _, url, _ = directory_call("admin@exemple.test")
        assert url.endswith("/users/admin%40exemple.test")

    def test_une_non_adresse_est_refusee_avant_l_appel(self):
        with pytest.raises(UserResolutionError, match="adresse"):
            directory_call("pas-une-adresse")

    def test_openid_interroge_le_profil_de_l_utilisateur_impersonne(self):
        assert userinfo_call()[1].endswith("/v1/userinfo")

    def test_les_deux_lectures_extraient_le_bon_champ(self):
        assert user_id_of_directory({"id": ID, "primaryEmail": "a@x"}) == ID
        assert user_id_of_userinfo({"sub": ID}) == ID

    @pytest.mark.parametrize("charge", [{}, {"id": ""}, None, "texte"])
    def test_une_reponse_sans_identifiant_est_REFUSEE(self, charge):
        """Rendre une chaîne vide poserait un abonnement sur `users/` — accepté peut-être,
        muet à coup sûr."""
        with pytest.raises(UserResolutionError):
            user_id_of_directory(charge)


class TestResolution:
    def test_l_annuaire_est_essaye_EN_PREMIER(self):
        """À cent utilisateurs, l'annuaire demande UN jeton pour tous là où OpenID en demande
        cent — la préférence n'est pas cosmétique."""
        vus = []
        resolver = UserResolver(
            directory=lambda e: vus.append("annuaire") or {"id": ID},
            openid=lambda e: vus.append("openid") or {"sub": "autre"})
        assert resolver.resolve("a@x.test") == ID
        assert vus == ["annuaire"]

    def test_OpenID_prend_le_relais_si_l_annuaire_echoue(self):
        """Cas réel : une DSI refuse la lecture de l'annuaire, ce qui est légitime."""
        def annuaire_casse(_):
            raise RuntimeError("403 portée non accordée")
        resolver = UserResolver(directory=annuaire_casse, openid=lambda e: {"sub": ID})
        assert resolver.resolve("a@x.test") == ID

    def test_les_DEUX_indisponibles_donnent_un_message_ACTIONNABLE(self):
        with pytest.raises(UserResolutionError) as exc:
            UserResolver().resolve("a@x.test")
        assert DIRECTORY_SCOPE in str(exc.value)

    def test_les_deux_echecs_sont_RAPPORTES_ensemble(self):
        """Ne montrer que le dernier ferait chercher la panne du mauvais côté."""
        def casse(msg):
            def _(_e):
                raise RuntimeError(msg)
            return _
        resolver = UserResolver(directory=casse("annuaire refusé"),
                                openid=casse("openid refusé"))
        with pytest.raises(UserResolutionError) as exc:
            resolver.resolve("a@x.test")
        assert "annuaire refusé" in str(exc.value) and "openid refusé" in str(exc.value)

    def test_le_cache_evite_de_re_resoudre(self):
        """Un identifiant Cloud Identity ne change jamais : re-résoudre cent adresses à
        chaque tour serait cent authentifications par heure pour une donnée immuable."""
        appels = []
        resolver = UserResolver(directory=lambda e: appels.append(e) or {"id": ID})
        resolver.resolve("a@x.test")
        resolver.resolve("A@X.test")          # même adresse, casse différente
        assert appels == ["a@x.test"]

    def test_oublier_permet_de_re_resoudre(self):
        appels = []
        resolver = UserResolver(directory=lambda e: appels.append(e) or {"id": ID})
        resolver.resolve("a@x.test")
        resolver.forget("a@x.test")
        resolver.resolve("a@x.test")
        assert len(appels) == 2


class TestMessagesActionnables:
    """Deux prérequis qui se ressemblent et n'appellent pas le même geste — l'un dans la
    console Cloud, l'autre dans la console d'administration Workspace."""

    def test_l_API_non_activee_renvoie_vers_la_console_CLOUD(self):
        """Vécu le 2026-08-01 : portée bien déléguée, mais l'API Admin SDK jamais activée
        dans le projet. Le message brut de Google fait chercher du côté de la délégation."""
        message = explain_failure(
            'HTTP 403 — {"message": "Admin SDK API has not been used in project 104570807442 '
            'before or it is disabled"}')
        assert "n'est pas ACTIVÉE" in message and "Cloud" in message
        assert "délégation, elle, est indépendante" in message

    def test_la_portee_non_deleguee_renvoie_vers_la_console_WORKSPACE(self):
        message = explain_failure("unauthorized_client: not authorized for any of the scopes")
        assert "non déléguée" in message and DIRECTORY_SCOPE in message

    def test_un_refus_inconnu_est_rendu_TEL_QUEL(self):
        """Inventer une explication pour un cas non reconnu enverrait sur une fausse piste."""
        assert explain_failure("HTTP 500 — panique interne") == "HTTP 500 — panique interne"


class TestEquivalenceDesVoies:
    """La documentation n'affirme PAS que le `sub` OpenID est l'identifiant de l'annuaire."""

    def test_l_accord_est_CONSTATE_pas_suppose(self):
        ok, message = verify_resolvers_agree(
            UserResolver(directory=lambda e: {"id": ID}),
            UserResolver(openid=lambda e: {"sub": ID}), "a@x.test")
        assert ok and ID in message

    def test_une_DIVERGENCE_est_dite_sans_ambiguite(self):
        """Un identifiant différent produirait des abonnements acceptés par Google qui ne
        remontent jamais rien — la pire des pannes, celle qui se tait."""
        ok, message = verify_resolvers_agree(
            UserResolver(directory=lambda e: {"id": ID}),
            UserResolver(openid=lambda e: {"sub": "999"}), "a@x.test")
        assert not ok and "DIVERGENCE" in message and "muets" in message
