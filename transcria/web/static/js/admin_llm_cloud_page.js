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

  // Liste des modèles du fournisseur choisi (catalogue opencode du serveur) : l'admin choisit un
  // IDENTIFIANT, il ne le devine pas (« GLM-5.3 » saisi ≠ « zai-glm-5-3 » attendu).
  var list = document.getElementById("cloud-model-list");
  var other = document.getElementById("cloud-provider-other");
  var hint = form.querySelector("[data-cloud-models-hint]");
  var hintBase = hint ? hint.textContent : "";

  function fillModels(pid) {
    if (!list || !pid) return;
    var url = list.getAttribute("data-models-url").replace("__PROVIDER__", encodeURIComponent(pid));
    fetch(url, { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : { models: [] }; })
      .then(function (data) {
        list.innerHTML = "";
        (data.models || []).forEach(function (m) {
          var opt = document.createElement("option");
          opt.value = m.id;
          if (m.name && m.name !== m.id) opt.label = m.name;
          list.appendChild(opt);
        });
        if (hint) hint.textContent = hintBase + (data.models && data.models.length ? " (" + data.models.length + ")" : "");
      })
      .catch(function () { list.innerHTML = ""; });
  }

  function currentProvider() {
    if (!provider) return "";
    return provider.value === "__other__" ? (other ? other.value.trim() : "") : provider.value;
  }

  form.querySelectorAll("input[name=mode]").forEach(function (radio) { radio.addEventListener("change", refresh); });
  if (provider) provider.addEventListener("change", function () { refresh(); fillModels(currentProvider()); });
  if (other) other.addEventListener("change", function () { fillModels(currentProvider()); });
  refresh();
  fillModels(currentProvider());
})();
