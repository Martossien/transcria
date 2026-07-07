"""Catalogue des chaînes traduites exposées au JavaScript (axe A, Option 1).

Le front appelle ``t("chaîne source française")`` (helper static/js/i18n.js). Ici on liste
les **chaînes source** (msgid = français, convention gettext) utilisées côté navigateur, et on
construit ``window.I18N = { source: traduction }`` pour la locale courante.

Source UNIQUE de vérité = les mêmes catalogues gettext que le reste de l'UI. Les chaînes sont
marquées avec ``N_`` (gettext-noop, mot-clé extrait par pybabel) pour apparaître dans le
catalogue **sans** être traduites à l'import — la traduction se fait par requête dans
``build_js_catalog``. Quand on ajoute un ``t("…")`` dans un .js, on ajoute son source ici.
"""
from __future__ import annotations

import json

from flask_babel import gettext


def N_(s: str) -> str:
    """Marqueur d'extraction gettext (no-op) : pybabel récolte l'argument, renvoie tel quel."""
    return s


# Chaînes utilisées dans wizard.js / wizard-api.js (français source). Tenu à jour à la main,
# vague après vague. Beaucoup recoupent des msgid déjà présents côté templates (dédupliqués
# dans le catalogue) ; les nouvelles sont propres au JS.
JS_MESSAGES: tuple[str, ...] = (
    N_('%(label)s (%(elapsed)s écoulées)'),
    N_('%(n)s diapo(s)'),
    N_('%(n)s image(s) ignorée(s)'),
    N_('%(n)s page(s)'),
    N_('(créé)'),
    N_('Ajouter cette forme validée à un lexique central, partagé et réutilisé sur les prochains jobs'),
    N_('Annuler'),
    N_('Annuler le traitement'),
    N_('Au lexique central'),
    N_('Catégorie'),
    N_('Catégorie libre'),
    N_('Ce job a déjà été traité.'),
    N_('Contexte projet'),
    N_('Contexte proposé (%(n)s)'),
    N_('Continuer à attendre'),
    N_('Correction LLM en cours…'),
    N_('Démarrage…'),
    N_('Détection des locuteurs lancée sur le nœud GPU — la page se rafraîchira.'),
    N_('Erreur :'),
    N_('Erreur : %(e)s'),
    N_('Erreur réseau : %(msg)s'),
    N_('Erreur réseau.'),
    N_('Erreur serveur (%(status)s).'),
    N_('Erreur serveur non JSON.'),
    N_('Ex: Forme douteuse A, forme douteuse B'),
    N_('Ex: Forme validée'),
    N_('Export en cours…'),
    N_('Fichier trop volumineux (dépasse la limite du serveur).'),
    N_('Fichier téléversé. Rechargement…'),
    N_('Finalisation…'),
    N_('Fonction'),
    N_('Forme validée'),
    N_('Formes suspectes observées'),
    N_('Identification des locuteurs…'),
    N_('Informations client'),
    N_('Informations entretien'),
    N_('Informations formation'),
    N_('Informations incident'),
    N_('Informations légales PV'),
    N_('Informations légales PV — Séance extraordinaire'),
    N_('Informations négociation'),
    N_('Informations séminaire'),
    N_('La LLM est peut-être en boucle. Si les fichiers de correction sont déjà produits, vous pouvez annuler — le job sera récupéré automatiquement au redémarrage du service.'),  # noqa: E501
    N_("Le résumé n'a pas pu être généré (LLM sans production après 3 tentatives). La transcription est conservée — vous pouvez relancer."),
    N_('Lexique central : %(name)s'),
    N_('Nom'),
    N_('Ordre du jour & indicateurs'),
    N_('Oui, relancer'),
    N_('Pourquoi cette forme doit être validée'),
    N_('Priorité'),
    N_("Proposé à partir d'un document que vous avez joint à la réunion"),
    N_('Rapport qualité…'),
    N_('Relancement du traitement…'),
    N_('Relancer le résumé'),
    N_("Renseignez d'abord la forme validée."),
    N_('Retirer'),
    N_('Réponse serveur inattendue (HTTP %(status)s).'),
    N_('Réponse serveur invalide.'),
    N_('Rôle dans la réunion'),
    N_('Session expirée — redirection vers la connexion…'),
    N_('Terminé.'),
    N_('Traitement annulé après %(elapsed)s.'),
    N_('Traitement annulé.'),
    N_('Traitement démarré. Transcription ASR en cours… (0s écoulées)'),
    N_('Traitement relancé. Transcription ASR en cours… (0s écoulées)'),
    N_('Traitement terminé en %(elapsed)s. Chargement…'),
    N_('Transcription ASR en cours…'),
    N_("VRAM insuffisante : l'administrateur a été prévenu. Le résumé reprendra automatiquement dès que la mémoire GPU sera libérée."),
    N_('Voulez-vous relancer le traitement ? (le lexique et les corrections actuels seront appliqués)'),
    N_('issu des documents fournis'),
    N_('mis à jour il y a %(n)sh'),
    N_('mis à jour il y a %(n)smin'),
    N_('mis à jour il y a %(n)ss'),
    N_('nouveau'),
    N_('requête impossible'),
    N_('sans timecode'),
    N_('tronqué'),
    N_('· %(listened)s/%(total)s écoutés'),
    N_("Échec de l'ajout du document."),
    N_('Échec du traitement après %(elapsed)s.'),
    N_('Échec du traitement.'),
    N_('Écouter 5 secondes avant et après'),
    N_('Écouté'),
    N_('⚠ Le traitement LLM prend plus de temps que prévu (%(elapsed)s).'),
)


def build_js_catalog(locale: str) -> str:
    """Rend le corps JS ``window.I18N = {…}; window.I18N_LOCALE = "xx";``.

    ``locale`` sert au marqueur exposé au front (et au débogage) ; la traduction elle-même
    utilise la locale déjà résolue pour la requête par Flask-Babel.
    """
    catalog = {source: gettext(source) for source in JS_MESSAGES}
    return (
        "window.I18N = " + json.dumps(catalog, ensure_ascii=False) + ";\n"
        "window.I18N_LOCALE = " + json.dumps(locale) + ";\n"
    )
