"""Réunions voulues par l'administrateur → abonnements réels, et le compte rendu d'état.

Ces deux morceaux forment le pont entre l'interface et le service : l'un applique l'intention
écrite dans la configuration, l'autre rend visible ce qui s'est passé. Le contrat entre les
deux paquets est un FORMAT DE FICHIER, pas un module partagé — le service doit pouvoir
tourner sur une machine où `transcria` n'est pas installé. D'où le test qui fait lire au
portail ce que le connecteur écrit : c'est lui qui casse si l'un des deux dérive.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from connector_service.meet_desired import (
    covered_targets,
    ensure_subscriptions,
    ensure_user_subscriptions,
)
from connector_service.meet_report import write_report
from connector_service.workspace_events_client import WorkspaceEventsError
from transcria.ingestion.meet_status import is_stale, read_status

FILTRE = 'event_types:"google.workspace.meet.recording.v2.fileGenerated"'
SUJET = "projects/p/topics/p-events"


class _FauxMeet:
    """Résout un code en espace et règle l'enregistrement automatique.

    `refuse` simule une saisie fautive ; `refuse_reglage` simule la portée
    `meetings.space.settings` absente de la délégation.
    """

    def __init__(self, refuse=(), refuse_reglage=False):
        self._refuse = set(refuse)
        self._refuse_reglage = refuse_reglage
        self.regles: list[str] = []

    def resolve_space(self, saisie):
        if saisie in self._refuse:
            raise ValueError("réunion introuvable")
        return f"spaces/{saisie.rsplit('/', 1)[-1]}"

    def set_auto_recording(self, espace, *, enabled=True):
        if self._refuse_reglage:
            raise RuntimeError("HTTP 403 — portée manquante")
        self.regles.append(espace)
        return "ON"


class _FauxEvents:
    def __init__(self, existants=(), *, echoue_liste=False, echoue_create=False):
        self._existants = list(existants)
        self._echoue_liste = echoue_liste
        self._echoue_create = echoue_create
        self.crees: list[dict] = []

    def list(self, filtre):
        if self._echoue_liste:
            raise WorkspaceEventsError("inventaire refusé")
        return list(self._existants)

    def create(self, body, validate_only=False):
        if self._echoue_create:
            raise WorkspaceEventsError("création refusée")
        self.crees.append(body)
        return {"name": "subscriptions/x"}


def _abonnement(cible, etat="ACTIVE"):
    return {"name": "subscriptions/s", "targetResource": cible, "state": etat}


class TestCouverture:
    def test_un_abonnement_SUSPENDU_ne_compte_PAS_comme_couverture(self):
        """Le considérer comme couvrant laisserait la réunion sans surveillance en croyant
        l'inverse — et cela ne se verrait qu'à l'absence d'un compte rendu."""
        assert covered_targets([_abonnement("//x", "SUSPENDED")]) == set()

    def test_un_abonnement_actif_couvre_sa_cible(self):
        assert covered_targets([_abonnement("//x")]) == {"//x"}


class TestAlignement:
    def test_une_reunion_demandee_est_ABONNEE_ET_auto_enregistree(self):
        """La surveillance seule ne suffit pas : sans enregistrement automatique, il faut
        encore qu'un humain pense à cliquer « Enregistrer » — et le jour où il oublie, il
        n'y a pas de compte rendu, sans que rien ne le signale."""
        events, meet = _FauxEvents(), _FauxMeet()
        bilan = ensure_subscriptions(wanted=["abc-mnop-xyz"], topic=SUJET,
                                     events_client=events, meet_client=meet, settings_client=meet,
                                     subscriptions_filter=FILTRE)
        assert meet.regles == ["spaces/abc-mnop-xyz"]
        assert bilan.auto_recording == ["abc-mnop-xyz"]
        assert bilan.created == ["abc-mnop-xyz"]
        assert events.crees[0]["targetResource"] == "//meet.googleapis.com/spaces/abc-mnop-xyz"
        assert events.crees[0]["notificationEndpoint"]["pubsubTopic"] == SUJET

    def test_une_reunion_DEJA_couverte_ne_recree_rien(self):
        """Recréer à chaque tour multiplierait les abonnements — donc les évènements en
        double — sur une opération pourtant conçue pour être rejouable."""
        events = _FauxEvents([_abonnement("//meet.googleapis.com/spaces/abc")])
        bilan = ensure_subscriptions(wanted=["abc"], topic=SUJET, events_client=events,
                                     meet_client=_FauxMeet(), subscriptions_filter=FILTRE)
        assert bilan.already == ["abc"] and events.crees == []

    def test_une_saisie_FAUTIVE_n_empeche_pas_les_autres(self):
        events = _FauxEvents()
        bilan = ensure_subscriptions(wanted=["cassé", "bon"], topic=SUJET,
                                     events_client=events, meet_client=_FauxMeet(("cassé",)),
                                     subscriptions_filter=FILTRE)
        assert bilan.created == ["bon"]
        assert any("cassé" in echec for echec in bilan.failed)
        assert bilan.needs_attention

    def test_le_reglage_REFUSE_n_empeche_pas_la_surveillance(self):
        """Portée `meetings.space.settings` absente : on surveille quand même, en le disant.
        Renoncer à l'abonnement parce qu'on ne peut pas régler l'enregistrement priverait de
        compte rendu les réunions que l'organisateur enregistre à la main."""
        events = _FauxEvents()
        bilan = ensure_subscriptions(wanted=["abc"], topic=SUJET, events_client=events,
                                     meet_client=_FauxMeet(),
                                     settings_client=_FauxMeet(refuse_reglage=True),
                                     subscriptions_filter=FILTRE)
        assert bilan.created == ["abc"]
        assert bilan.auto_recording == []
        assert any("enregistrement automatique" in e for e in bilan.failed)

    def test_le_reglage_est_REAPPLIQUE_meme_si_deja_abonne(self):
        """Le réglage peut être remis à OFF dans l'interface Meet sans qu'on en soit averti :
        ne le poser qu'à la création laisserait la réunion silencieusement non enregistrée."""
        events = _FauxEvents([_abonnement("//meet.googleapis.com/spaces/abc")])
        meet = _FauxMeet()
        ensure_subscriptions(wanted=["abc"], topic=SUJET, events_client=events,
                             meet_client=meet, settings_client=meet,
                             subscriptions_filter=FILTRE)
        assert meet.regles == ["spaces/abc"]

    def test_SANS_client_de_reglage_on_surveille_quand_meme(self):
        """La portée `meetings.space.settings` n'est pas accordée par défaut, et Google
        refuse EN BLOC un jeton dont une portée manque. Fondre les deux ferait donc échouer
        la surveillance — une régression sur ce qui marchait, au nom d'un confort."""
        events = _FauxEvents()
        bilan = ensure_subscriptions(wanted=["abc"], topic=SUJET, events_client=events,
                                     meet_client=_FauxMeet(), settings_client=None,
                                     subscriptions_filter=FILTRE)
        assert bilan.created == ["abc"] and bilan.auto_recording == [] and not bilan.failed

    def test_un_abonnement_INCONNU_est_signale_JAMAIS_supprime(self):
        """Ce peut être un abonnement posé à la main pendant une campagne d'essais, ou celui
        d'une autre instance sur le même projet Cloud."""
        events = _FauxEvents([_abonnement("//meet.googleapis.com/spaces/autre")])
        bilan = ensure_subscriptions(wanted=["abc"], topic=SUJET, events_client=events,
                                     meet_client=_FauxMeet(), subscriptions_filter=FILTRE)
        assert bilan.extra == ["//meet.googleapis.com/spaces/autre"]

    def test_un_inventaire_IMPOSSIBLE_ne_cree_rien_a_l_aveugle(self):
        """Sans savoir ce qui existe, créer reviendrait à empiler des doublons."""
        events = _FauxEvents(echoue_liste=True)
        bilan = ensure_subscriptions(wanted=["abc"], topic=SUJET, events_client=events,
                                     meet_client=_FauxMeet(), subscriptions_filter=FILTRE)
        assert events.crees == [] and bilan.needs_attention

    def test_aucune_reunion_demandee_n_appelle_PAS_google(self):
        events = _FauxEvents()
        assert ensure_subscriptions(wanted=[], topic=SUJET, events_client=events,
                                    meet_client=_FauxMeet(),
                                    subscriptions_filter=FILTRE).wanted == 0


class TestCompteRendu:
    """Le contrat entre les deux paquets est le FORMAT. Ces tests le tiennent."""

    def test_ce_que_le_connecteur_ECRIT_le_portail_le_LIT(self, tmp_path):
        write_report(tmp_path, cycles=3, watched=["abc"], pending=[], problems=[],
                     last_jobs=["job-1"], subscriptions=[{"target": "//x"}],
                     auto_recording=["abc"])
        etat = read_status(tmp_path)
        assert etat is not None
        assert etat.cycles == 3 and etat.watched == ["abc"] and etat.last_jobs == ["job-1"]
        assert etat.auto_recording == ["abc"]
        assert etat.healthy

    def test_un_etat_frais_n_est_PAS_perime(self, tmp_path):
        write_report(tmp_path, cycles=1, watched=[], pending=[], problems=[], last_jobs=[],
                     subscriptions=[])
        assert not is_stale(read_status(tmp_path))

    def test_un_etat_VIEUX_est_perime(self, tmp_path):
        """Un compte rendu figé sur « OK » est le pire des affichages : il fait croire à une
        surveillance active alors que le service est arrêté."""
        vieux = datetime.now(timezone.utc) - timedelta(hours=2)
        write_report(tmp_path, cycles=1, watched=[], pending=[], problems=[], last_jobs=[],
                     subscriptions=[], now=vieux)
        assert is_stale(read_status(tmp_path))

    def test_aucun_fichier_vaut_JAMAIS_DEMARRE(self, tmp_path):
        assert read_status(tmp_path) is None

    @pytest.mark.parametrize("contenu", ["pas du json", "[1,2]", ""])
    def test_un_fichier_illisible_vaut_ABSENCE(self, tmp_path, contenu):
        """Afficher un état partiel serait pire que « jamais démarré », qui au moins oriente
        vers la bonne question."""
        (tmp_path / "meet_status.json").write_text(contenu, encoding="utf-8")
        assert read_status(tmp_path) is None

    def test_un_champ_INCONNU_ne_casse_pas_la_lecture(self, tmp_path):
        """Le connecteur peut être plus récent que le portail : ignorer ce qu'on ne connaît
        pas vaut mieux qu'une page en erreur pendant une mise à jour."""
        import json
        (tmp_path / "meet_status.json").write_text(
            json.dumps({"cycles": 2, "champ_du_futur": 42}), encoding="utf-8")
        etat = read_status(tmp_path)
        assert etat is not None and etat.cycles == 2


class TestAbonnementParUtilisateur:
    """Le modèle qui passe à l'échelle : 100 utilisateurs = 100 abonnements posés seuls, au
    lieu de 100 personnes qui font déclarer leurs salles une par une."""

    def _resolveur(self, refuse=()):
        def resolve(adresse):
            if adresse in refuse:
                raise RuntimeError("utilisateur inconnu de l'annuaire")
            return "1002697802021" + str(len(adresse))
        return resolve

    def test_un_abonnement_par_personne(self):
        events = _FauxEvents()
        bilan = ensure_user_subscriptions(users=["a@x.test", "bb@x.test"], topic=SUJET,
                                          events_client=events,
                                          resolve_user=self._resolveur(),
                                          subscriptions_filter=FILTRE)
        assert bilan.created == ["a@x.test", "bb@x.test"]
        cibles = [c["targetResource"] for c in events.crees]
        assert all(c.startswith("//cloudidentity.googleapis.com/users/") for c in cibles)
        assert len(set(cibles)) == 2          # une cible DISTINCTE par personne

    def test_une_personne_deja_couverte_ne_recree_rien(self):
        resolve = self._resolveur()
        deja = f"//cloudidentity.googleapis.com/users/{resolve('a@x.test')}"
        events = _FauxEvents([_abonnement(deja)])
        bilan = ensure_user_subscriptions(users=["a@x.test"], topic=SUJET,
                                          events_client=events, resolve_user=resolve,
                                          subscriptions_filter=FILTRE)
        assert bilan.already == ["a@x.test"] and events.crees == []

    def test_une_personne_NON_resolue_n_empeche_pas_les_autres(self):
        """Un compte hors annuaire ne doit pas priver toute l'organisation de comptes
        rendus — c'est le bug de boucle classique, ici à l'échelle de l'entreprise."""
        events = _FauxEvents()
        bilan = ensure_user_subscriptions(users=["fantome@x.test", "bon@x.test"], topic=SUJET,
                                          events_client=events,
                                          resolve_user=self._resolveur(("fantome@x.test",)),
                                          subscriptions_filter=FILTRE)
        assert bilan.created == ["bon@x.test"]
        assert any("fantome@x.test" in e for e in bilan.failed)
        assert bilan.needs_attention

    def test_aucun_utilisateur_n_appelle_PAS_google(self):
        events = _FauxEvents()
        assert ensure_user_subscriptions(users=[], topic=SUJET, events_client=events,
                                         resolve_user=self._resolveur(),
                                         subscriptions_filter=FILTRE).wanted == 0
        assert events.crees == []
