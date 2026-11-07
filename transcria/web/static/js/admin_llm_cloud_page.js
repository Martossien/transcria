/*
 * admin_llm_cloud_page.js — écran « LLM d'arbitrage » : montre les champs du mode choisi
 * (serveur de mon organisation / fournisseur externe) et, pour le cloud, le bloc « autre
 * fournisseur ». Aucune donnée : tout vient du formulaire rendu par le serveur.
 */
(function () {
  "use strict";
  var form = document.getElementById("llm-mode-form");
  if (!form) return;
  var sections = form.querySelectorAll("[data-llm-section]");
  var provider = document.getElementById("cloud-provider");

  function show(name, visible) {
    sections.forEach(function (el) {
      if (el.getAttribute("data-llm-section") === name) el.classList.toggle("d-none", !visible);
    });
  }

  function refresh() {
    var mode = (form.querySelector("input[name=mode]:checked") || {}).value || "local";
    show("organisation", mode === "organisation");
    show("cloud", mode === "cloud");
    show("cloud-other", mode === "cloud" && provider && provider.value === "__other__");
  }

  form.querySelectorAll("input[name=mode]").forEach(function (radio) { radio.addEventListener("change", refresh); });
  if (provider) provider.addEventListener("change", refresh);
  refresh();
})();
