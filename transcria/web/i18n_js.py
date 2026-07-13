"""SHIM de dépréciation (vague A1, 2026-07-13) — module déplacé vers transcria.i18n.js_catalog.

Conservé UNE release (suppression prévue à la suivante) pour les imports externes éventuels.
Le code du dépôt importe déjà le nouveau chemin.
"""
from transcria.i18n.js_catalog import JS_MESSAGES, N_, build_js_catalog  # noqa: F401
