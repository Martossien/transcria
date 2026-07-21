// Délégation d'événements CSP-safe (chantier CSP stricte). Remplace les gestionnaires
// inline (onclick=, onchange=, oninput=, onsubmit="return confirm(...)") pour permettre
// une Content-Security-Policy sans 'unsafe-inline' sur script-src.
//
// Deux mécanismes, tous deux par DÉLÉGATION (fonctionne aussi pour le contenu ajouté
// dynamiquement) :
//
//  1) Confirmation de formulaire :
//       <form ... data-confirm="Message ?"> … </form>
//     → demande window.confirm(...) à la soumission ; annule si refus.
//
//  2) Action déléguée — l'ARGUMENT reproduit EXACTEMENT l'appel inline d'origine :
//       action "MT.addField"                            → MT.addField()          (aucun arg)
//       action "TranscrIA.chooseProfile" + data-arg=ID   → …chooseProfile("ID")   (chaîne)
//       action + data-action-el                          → …fn(element)           (l'élément)
//       action + data-action-val                         → …fn(element.value)     (sa valeur)
//       data-on="change"|"input"                         → événement déclencheur (défaut click)
//
// Actions intégrées (sans fonction de page) :
//   data-action="dom.removeClosest" data-target=".sel"  → retire l'ancêtre correspondant
//     (remplace onclick="this.parentElement.remove()") ;
//   data-action="dom.clickTarget" data-target="#id"     → clique l'élément visé
//     (remplace onclick="document.getElementById('id').click()").
(function () {
  "use strict";

  // 1) Confirmations de formulaire.
  document.addEventListener("submit", function (e) {
    var f = e.target;
    if (f && f.dataset && f.dataset.confirm) {
      if (!window.confirm(f.dataset.confirm)) e.preventDefault();
    }
  }, true);

  // 2) Actions déléguées.
  function resolve(path) {
    if (!path) return null;
    var parts = path.split("."), owner = window, fn = window;
    for (var i = 0; i < parts.length; i++) {
      if (fn == null) return null;
      owner = fn; fn = fn[parts[i]];
    }
    return (typeof fn === "function") ? { fn: fn, owner: owner } : null;
  }

  // Reproduit l'argument de l'appel inline d'origine (voir en-tête).
  function callArgs(el) {
    var d = el.dataset;
    if ("arg" in d) return [d.arg];
    if ("actionEl" in d) return [el];
    if ("actionVal" in d) return [el.value];
    return [];
  }

  var BUILTIN = {
    "dom.removeClosest": function (el) {
      var sel = el.dataset.target;
      var node = sel ? el.closest(sel) : el.parentElement;
      if (node && node.remove) node.remove();
    },
    "dom.clickTarget": function (el) {
      var t = document.querySelector(el.dataset.target || "");
      if (t) t.click();
    },
    // onclick="document.getElementById('x').innerHTML=''" → data-action="dom.clearTarget" data-target="#x"
    "dom.clearTarget": function (el) {
      var t = document.querySelector(el.dataset.target || "");
      if (t) t.innerHTML = "";
    }
  };

  function dispatch(e) {
    var el = e.target.closest ? e.target.closest("[data-action]") : null;
    if (!el) return;
    if ((el.dataset.on || "click") !== e.type) return;
    var action = el.dataset.action;
    if (BUILTIN[action]) { BUILTIN[action](el, e); return; }
    var r = resolve(action);
    if (r) r.fn.apply(r.owner, callArgs(el));
  }
  document.addEventListener("click", dispatch, false);
  document.addEventListener("change", dispatch, false);
  document.addEventListener("input", dispatch, false);
})();
