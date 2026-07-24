// Protection CSRF côté client (chantier sécurité). Alimente AUTOMATIQUEMENT :
//  - tous les formulaires POST/PUT/DELETE (champ caché `csrf_token`),
//  - toutes les requêtes `fetch` mutantes MÊME ORIGINE (en-tête `X-CSRFToken`).
// Le jeton vient de la balise <meta name="csrf-token"> (posée par base.html).
// Aucun formulaire ni appel à modifier un par un → maintenable et exhaustif.
// Sans effet si la protection serveur est désactivée (le serveur ignore le jeton).
(function () {
  "use strict";
  var meta = document.querySelector('meta[name="csrf-token"]');
  var TOKEN = meta ? meta.getAttribute("content") : "";
  if (!TOKEN) return;

  var UNSAFE = { GET: 0, HEAD: 0, OPTIONS: 0, TRACE: 0 }; // méthodes SÛRES = pas de jeton

  // 1) Formulaires : injecter un champ caché csrf_token s'il manque.
  function ensureFormToken(form) {
    var method = (form.getAttribute("method") || "get").toUpperCase();
    if (method in UNSAFE) return;
    if (form.querySelector('input[name="csrf_token"]')) return;
    var input = document.createElement("input");
    input.type = "hidden";
    input.name = "csrf_token";
    input.value = TOKEN;
    form.appendChild(input);
  }
  function wireForms() {
    var forms = document.querySelectorAll("form");
    for (var i = 0; i < forms.length; i++) ensureFormToken(forms[i]);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wireForms);
  } else {
    wireForms();
  }
  // Formulaires ajoutés dynamiquement : filet à la soumission.
  document.addEventListener("submit", function (e) {
    if (e.target && e.target.tagName === "FORM") ensureFormToken(e.target);
  }, true);

  // 2) fetch : ajouter X-CSRFToken sur les requêtes mutantes MÊME ORIGINE.
  var origFetch = window.fetch;
  if (typeof origFetch === "function") {
    window.fetch = function (input, init) {
      init = init || {};
      var method = (init.method || (typeof input !== "string" && input && input.method) || "GET").toUpperCase();
      if (!(method in UNSAFE)) {
        // N'ajouter le jeton que pour une cible même-origine (jamais l'exfiltrer).
        // Résolution d'URL réelle (couvre //host, préfixes trompeurs ex. origin.evil.com).
        var url = (typeof input === "string") ? input : (input && input.url) || "";
        var sameOrigin;
        try {
          sameOrigin = new URL(url, window.location.href).origin === window.location.origin;
        } catch (err) {
          sameOrigin = false;
        }
        if (sameOrigin) {
          var headers = new Headers(init.headers || (typeof input !== "string" && input && input.headers) || {});
          if (!headers.has("X-CSRFToken")) headers.set("X-CSRFToken", TOKEN);
          init.headers = headers;
        }
      }
      return origFetch.call(this, input, init);
    };
  }
})();
