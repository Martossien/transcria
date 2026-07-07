/*
 * i18n.js — helper de traduction côté navigateur (axe A, Option 1).
 *
 * window.I18N est injecté par /i18n/messages.js (chargé AVANT ce script) :
 *   { "chaîne source française": "traduction dans la locale courante", ... }
 *
 * Usage :
 *   t("Confirmer la suppression ?")                       → traduction (ou la clé si absente)
 *   t("%(n)s fichier(s) restants", { n: 3 })              → interpolation nommée
 *
 * Convention gettext : la CLÉ est la chaîne source française (msgid). Une clé absente du
 * catalogue retombe sur elle-même → jamais de « [missing] » affiché à l'utilisateur.
 */
(function () {
  "use strict";

  function interpolate(template, params) {
    if (!params) return template;
    return template.replace(/%\(([^)]+)\)s/g, function (match, name) {
      return Object.prototype.hasOwnProperty.call(params, name) ? String(params[name]) : match;
    });
  }

  window.t = function (key, params) {
    var catalog = window.I18N || {};
    var translated = Object.prototype.hasOwnProperty.call(catalog, key) ? catalog[key] : key;
    return interpolate(translated, params);
  };
})();
