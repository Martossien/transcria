/* Page « Mes types de réunion » (lot E — docs/TYPES_REUNION_PERSONNALISES.md §7).
   Vanilla JS : galerie duplicate-first + éditeur avec aperçu vivant de la couverture.
   Les palettes proposées sont DÉRIVÉES des thèmes intégrés (aucune couleur en dur). */
(function () {
  "use strict";

  const state = {
    catalog: null,        // réponse GET /api/meeting-types
    editingId: null,      // id du template en cours d'édition (null = création)
    baseDefinition: null, // fiche source (duplication) ou fiche éditée
    hasLogo: false,
  };

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
    {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));

  const SECTION_LABELS = {
    contexte: "Contexte de la réunion",
    synthese: "Synthèse (en section autonome)",
    champs_type: "Informations spécifiques (champs du type)",
    pv: "Contenu extrait (ordre du jour, décisions, votes…)",
    participants: "Participants & locuteurs",
    transcript: "Transcription",
    quality: "Points à vérifier",
  };
  const DEFAULT_ORDER = ["contexte", "pv", "participants", "transcript", "quality"];
  const TOGGLEABLE = ["participants", "transcript", "quality"];

  // ── Chargement ────────────────────────────────────────────────────────────
  async function load() {
    const r = await fetch("/api/meeting-types");
    state.catalog = await r.json();
    renderGallery();
    renderPalettes();
    const used = state.catalog.custom.filter(
      (t) => t.created_by === null || true).length; // le quota exact est côté serveur
    $("mt-quota").textContent = `${state.catalog.custom.length} type(s) visible(s) — quota : ${state.catalog.max_per_user} créés/personne`;
  }

  // ── Galerie ───────────────────────────────────────────────────────────────
  function scopeBadge(t) {
    if (t.builtin) return '<span class="badge text-bg-light border">Intégré</span>';
    if (t.is_active === false) return '<span class="badge text-bg-warning">Importé — à relire</span>';
    if (t.scope === "global") return '<span class="badge text-bg-primary">Partagé à tous</span>';
    if (t.scope === "group") return `<span class="badge text-bg-info">Groupe ${esc(t.group_name)}</span>`;
    return '<span class="badge text-bg-secondary">Privé</span>';
  }

  function paletteDots(palette) {
    if (!palette) return '<span class="mt-dot" style="background:#1f3864"></span><span class="mt-dot" style="background:#2e74b5"></span><span class="mt-dot" style="background:#d6e4f0"></span>';
    return ["primary", "accent", "light"].map(
      (k) => `<span class="mt-dot" style="background:#${esc(palette[k])}"></span>`).join("");
  }

  function card(t) {
    const def = t.builtin ? t : (t.definition || {});
    const palette = def.palette;
    const banner = def.banner_text || "COMPTE-RENDU DE TRANSCRIPTION";
    const primary = palette ? `#${palette.primary}` : "#1f3864";
    const manageable = !t.builtin && state.catalog.manageable_ids.includes(t.id);
    const name = t.builtin ? t.name : t.name;
    let actions = `
      <button class="btn btn-sm btn-primary" data-action="duplicate" data-id="${esc(t.id || "")}" data-name="${esc(name)}">
        <i class="bi bi-plus-circle"></i> Créer le mien</button>`;
    if (!t.builtin) {
      actions += ` <a class="btn btn-sm btn-outline-secondary" href="/api/meeting-types/${esc(t.id)}/preview.docx"
                     title="Télécharger un exemple Word"><i class="bi bi-file-earmark-word"></i></a>`;
      actions += ` <a class="btn btn-sm btn-outline-secondary" href="/api/meeting-types/${esc(t.id)}/export"
                     title="Exporter (partage entre installations / communauté)"><i class="bi bi-box-arrow-up"></i></a>`;
    }
    if (manageable) {
      actions += ` <button class="btn btn-sm btn-outline-secondary" data-action="edit" data-id="${esc(t.id)}">
          <i class="bi bi-pencil"></i></button>`;
      actions += shareMenu(t);
      actions += ` <button class="btn btn-sm btn-outline-danger" data-action="delete" data-id="${esc(t.id)}" data-name="${esc(name)}">
          <i class="bi bi-trash"></i></button>`;
    }
    return `
      <div class="mt-card card">
        <div class="mt-card-banner" style="background:${primary}">${esc(banner)}</div>
        <div class="card-body py-2 px-3">
          <div class="d-flex justify-content-between align-items-center">
            <strong class="mt-card-name">${esc(name)}</strong>
            <span class="mt-dots">${paletteDots(palette)}</span>
          </div>
          <div class="d-flex justify-content-between align-items-center mt-1">
            ${scopeBadge(t)}
            ${def.badge ? `<span class="small text-muted">[ ${esc(def.badge)} ]</span>` : ""}
          </div>
          <div class="mt-2 d-flex gap-1 flex-wrap">${actions}</div>
        </div>
      </div>`;
  }

  function shareMenu(t) {
    const targets = state.catalog.share_targets;
    let items = "";
    for (const g of targets.groups) {
      items += `<li><a class="dropdown-item" href="#" data-action="share" data-id="${esc(t.id)}"
                   data-scope="group" data-group="${esc(g.id)}">Partager au groupe ${esc(g.name)}</a></li>`;
    }
    if (targets.global) {
      items += `<li><a class="dropdown-item" href="#" data-action="share" data-id="${esc(t.id)}"
                   data-scope="global">Partager à tous</a></li>`;
    }
    if (t.scope !== "private") {
      items += `<li><a class="dropdown-item" href="#" data-action="share" data-id="${esc(t.id)}"
                   data-scope="private">Rendre privé</a></li>`;
    }
    if (!items) return "";
    return `
      <div class="btn-group">
        <button class="btn btn-sm btn-outline-secondary dropdown-toggle" data-bs-toggle="dropdown">
          <i class="bi bi-share"></i></button>
        <ul class="dropdown-menu">${items}</ul>
      </div>`;
  }

  function renderGallery() {
    const cards = [];
    for (const t of state.catalog.custom) cards.push(card(t));
    for (const t of state.catalog.builtin) cards.push(card(t));
    $("mt-cards").innerHTML = cards.join("");
  }

  // ── Palettes prédéfinies (dérivées des thèmes intégrés, dédupliquées) ────
  function builtinPalettes() {
    const seen = new Set(); const out = [];
    for (const t of state.catalog.builtin) {
      if (!t.palette) continue;
      const key = t.palette.primary + t.palette.accent + t.palette.light;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push(t.palette);
    }
    return out;
  }

  function renderPalettes() {
    $("mt-palettes").innerHTML = builtinPalettes().map((p, i) => `
      <button type="button" class="mt-palette" title="Appliquer cette palette"
              data-primary="${p.primary}" data-accent="${p.accent}" data-light="${p.light}">
        <span style="background:#${p.primary}"></span><span style="background:#${p.accent}"></span><span style="background:#${p.light}"></span>
      </button>`).join("");
  }

  // ── Éditeur ───────────────────────────────────────────────────────────────
  function openEditor(source, editingId) {
    state.editingId = editingId || null;
    state.baseDefinition = source;
    state.hasLogo = false;
    $("mt-editor-title").textContent = editingId ? `Modifier « ${source.name} »` : `Créer mon type (à partir de « ${source.name} »)`;
    $("mt-name").value = editingId ? source.name : "";
    $("mt-name").placeholder = editingId ? "" : `ex. ${source.name} — mon équipe`;
    $("mt-badge").value = source.badge || "";
    $("mt-banner").value = source.banner_text || "";
    $("mt-description").value = source.description || "";
    const palette = source.palette || {primary: "1F3864", accent: "2E74B5", light: "D6E4F0"};
    $("mt-primary").value = "#" + palette.primary.toLowerCase();
    $("mt-accent").value = "#" + palette.accent.toLowerCase();
    $("mt-light").value = "#" + palette.light.toLowerCase();
    $("mt-confidential").checked = !!(source.behavior || {}).confidential;
    $("mt-footer").value = (source.branding || {}).footer_text || "";
    renderFields(source.fields || []);
    $("mt-hints").value = (source.detection_hints || []).join("\n");
    renderExtracts(source.extract_fields || []);
    renderSections((source.sections || {}).order || DEFAULT_ORDER, (source.sections || {}).enabled || {});
    if (editingId) {
      const tpl = state.catalog.custom.find((t) => t.id === editingId);
      state.hasLogo = !!(tpl && tpl.has_logo);
    }
    $("mt-logo-hint").classList.toggle("d-none", !!editingId);
    $("mt-logo").disabled = !editingId;
    $("mt-logo-clear").classList.toggle("d-none", !state.hasLogo);
    $("mt-error").textContent = "";
    $("mt-gallery").classList.add("d-none");
    $("mt-editor").classList.remove("d-none");
    refreshPreview();
    window.scrollTo({top: 0});
  }

  function closeEditor() {
    $("mt-editor").classList.add("d-none");
    $("mt-gallery").classList.remove("d-none");
  }

  // Champs de saisie dynamiques
  function renderFields(fields) {
    $("mt-fields").innerHTML = "";
    for (const f of fields) addField(f);
  }

  function addField(f) {
    const row = document.createElement("div");
    row.className = "row g-1 mb-1 mt-field-row";
    row.innerHTML = `
      <div class="col-5"><input class="form-control form-control-sm mt-f-label" maxlength="80"
        placeholder="Libellé (ex. Filiale concernée)" value="${esc(f && f.label || "")}"></div>
      <div class="col-3"><select class="form-select form-select-sm mt-f-type">
        <option value="text">Texte</option><option value="number">Nombre</option><option value="textarea">Texte long</option>
      </select></div>
      <div class="col-3"><input class="form-control form-control-sm mt-f-short" maxlength="80"
        placeholder="Libellé court (Word)" value="${esc(f && f.short_label || "")}"></div>
      <div class="col-1"><button class="btn btn-sm btn-outline-danger" title="Retirer"
        onclick="this.closest('.mt-field-row').remove()"><i class="bi bi-x"></i></button></div>`;
    if (f && f.type) row.querySelector(".mt-f-type").value = f.type;
    row.dataset.key = (f && f.key) || "";
    $("mt-fields").appendChild(row);
  }

  function renderExtracts(extracts) {
    $("mt-extracts").innerHTML = "";
    for (const e of extracts) addExtract(e);
  }

  function addExtract(e) {
    const row = document.createElement("div");
    row.className = "row g-1 mb-1 mt-extract-row";
    row.innerHTML = `
      <div class="col-4"><input class="form-control form-control-sm mt-e-label" maxlength="80"
        placeholder="Libellé (ex. Budgets évoqués)" value="${esc(e && e.label || "")}"></div>
      <div class="col-7"><input class="form-control form-control-sm mt-e-instr" maxlength="200"
        placeholder="À relever (ex. montants budgétaires explicitement cités)" value="${esc(e && e.instruction || "")}"></div>
      <div class="col-1"><button class="btn btn-sm btn-outline-danger" title="Retirer"
        onclick="this.closest('.mt-extract-row').remove()"><i class="bi bi-x"></i></button></div>`;
    row.dataset.key = (e && e.key) || "";
    $("mt-extracts").appendChild(row);
  }

  function slugKey(label) {
    return label.normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase()
      .replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 60) || "champ";
  }

  // Sections ordonnées (flèches haut/bas + cases pour les désactivables)
  function renderSections(order, enabled) {
    const list = $("mt-sections");
    list.innerHTML = "";
    const full = [...order];
    for (const k of DEFAULT_ORDER) if (!full.includes(k)) full.push(k);
    for (const key of full) {
      const li = document.createElement("li");
      li.className = "list-group-item d-flex align-items-center gap-2 py-1";
      li.dataset.key = key;
      const toggle = TOGGLEABLE.includes(key)
        ? `<input type="checkbox" class="form-check-input mt-s-enabled" ${enabled[key] === false ? "" : "checked"}>`
        : `<i class="bi bi-lock text-muted" title="Toujours présent"></i>`;
      li.innerHTML = `
        <span class="btn-group btn-group-sm">
          <button class="btn btn-outline-secondary py-0 px-1" data-move="-1" title="Monter"><i class="bi bi-chevron-up"></i></button>
          <button class="btn btn-outline-secondary py-0 px-1" data-move="1" title="Descendre"><i class="bi bi-chevron-down"></i></button>
        </span>
        ${toggle}
        <span class="small">${SECTION_LABELS[key] || key}</span>`;
      list.appendChild(li);
    }
  }

  // « synthese »/« champs_type » ne sont proposés qu'en les ajoutant à l'ordre :
  // v1 = liste complète avec les 7 unités si l'utilisateur déplace synthese en tête.
  function currentOrder() {
    return [...$("mt-sections").children].map((li) => li.dataset.key);
  }

  function currentEnabled() {
    const enabled = {};
    for (const li of $("mt-sections").children) {
      const box = li.querySelector(".mt-s-enabled");
      if (box) enabled[li.dataset.key] = box.checked;
    }
    return enabled;
  }

  // ── Fiche courante (formulaire → definition) ─────────────────────────────
  function currentDefinition() {
    const hex = (v) => v.replace("#", "").toUpperCase();
    const fields = [...document.querySelectorAll(".mt-field-row")].map((row) => {
      const label = row.querySelector(".mt-f-label").value.trim();
      if (!label) return null;
      const field = {
        key: row.dataset.key || slugKey(label),
        label,
        type: row.querySelector(".mt-f-type").value,
      };
      const short = row.querySelector(".mt-f-short").value.trim();
      if (short) field.short_label = short;
      return field;
    }).filter(Boolean);
    const definition = {
      name: $("mt-name").value.trim(),
      badge: $("mt-badge").value.trim() || null,
      banner_text: $("mt-banner").value.trim() || null,
      palette: {primary: hex($("mt-primary").value), accent: hex($("mt-accent").value), light: hex($("mt-light").value)},
      behavior: {confidential: $("mt-confidential").checked,
                 quorum: !!(state.baseDefinition.behavior || {}).quorum},
      fields,
      detection_hints: $("mt-hints").value.split("\n").map((h) => h.trim()).filter(Boolean).slice(0, 8),
      extract_fields: [...document.querySelectorAll(".mt-extract-row")].map((row) => {
        const label = row.querySelector(".mt-e-label").value.trim();
        const instruction = row.querySelector(".mt-e-instr").value.trim();
        if (!label || !instruction) return null;
        return {key: row.dataset.key || slugKey(label), label, instruction};
      }).filter(Boolean),
      sections: {order: currentOrder(), enabled: currentEnabled()},
    };
    const footer = $("mt-footer").value.trim();
    definition.branding = footer ? {footer_text: footer} : {};
    if (!definition.badge) delete definition.badge;
    if (!definition.banner_text) definition.banner_text = null;
    return definition;
  }

  // ── Aperçu vivant ─────────────────────────────────────────────────────────
  function luminance(hex) {
    const n = parseInt(hex.replace("#", ""), 16);
    const r = (n >> 16) & 255, g = (n >> 8) & 255, b = n & 255;
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255;
  }

  function refreshPreview() {
    const primary = $("mt-primary").value, accent = $("mt-accent").value, light = $("mt-light").value;
    const banner = $("mt-banner").value.trim() || "COMPTE-RENDU DE TRANSCRIPTION";
    const badge = $("mt-badge").value.trim();
    $("mt-cover-banner").textContent = banner;
    $("mt-cover-banner").style.background = primary;
    $("mt-cover-accent").style.background = accent;
    $("mt-cover").style.setProperty("--mt-light", light);
    const badgeEl = $("mt-cover-badge");
    badgeEl.classList.toggle("d-none", !badge);
    badgeEl.textContent = badge ? `[ ${badge} ]` : "";
    badgeEl.style.color = accent;
    $("mt-cover-confidential").classList.toggle("d-none", !$("mt-confidential").checked);
    const footer = $("mt-footer").value.trim();
    $("mt-cover-footer").textContent =
      (footer ? footer + " · " : "") + "TranscrIA · Réunion d'exemple · Page 1/4";
    $("mt-cover-logo").classList.toggle("d-none", !state.hasLogo);
    $("mt-contrast-warning").classList.toggle("d-none", luminance(primary) < 0.6);
  }

  async function downloadPreview() {
    const definition = currentDefinition();
    if (!definition.name) { $("mt-error").textContent = "Donnez d'abord un nom au type."; return; }
    const r = await fetch("/api/meeting-types/preview.docx", {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(definition),
    });
    if (!r.ok) { $("mt-error").textContent = (await r.json()).error || "Aperçu impossible."; return; }
    const blob = await r.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "apercu_type.docx";
    a.click();
    URL.revokeObjectURL(a.href);
  }

  // ── Enregistrement / partage / suppression ───────────────────────────────
  async function save() {
    const definition = currentDefinition();
    $("mt-error").textContent = "";
    const url = state.editingId ? `/api/meeting-types/${state.editingId}` : "/api/meeting-types";
    const r = await fetch(url, {
      method: state.editingId ? "PUT" : "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify(definition),
    });
    const data = await r.json();
    if (!r.ok) { $("mt-error").textContent = data.error || "Enregistrement impossible."; return; }
    // Logo choisi : téléversé après coup (le binaire vit hors de la fiche).
    const logoInput = $("mt-logo");
    if (logoInput.files && logoInput.files[0]) {
      const form = new FormData();
      form.append("logo", logoInput.files[0]);
      const lr = await fetch(`/api/meeting-types/${data.id}/logo`, {method: "POST", body: form});
      if (!lr.ok) { $("mt-error").textContent = (await lr.json()).error || "Logo refusé."; return; }
    }
    await load();
    closeEditor();
  }

  async function clearLogo() {
    if (!state.editingId) return;
    await fetch(`/api/meeting-types/${state.editingId}/logo`, {method: "DELETE"});
    state.hasLogo = false;
    refreshPreview();
    $("mt-logo-clear").classList.add("d-none");
  }

  async function importFile(file) {
    const form = new FormData();
    form.append("file", file);
    const r = await fetch("/api/meeting-types/import", {method: "POST", body: form});
    const data = await r.json();
    if (!r.ok) { alert(data.error || "Import impossible."); return; }
    alert(`Type « ${data.name} » importé (privé, à relire) : ouvrez-le, vérifiez, enregistrez pour l'activer.`);
    await load();
  }

  async function share(id, scope, groupId) {
    const r = await fetch(`/api/meeting-types/${id}/scope`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({scope, group_id: groupId || null}),
    });
    if (!r.ok) alert((await r.json()).error || "Partage impossible.");
    await load();
  }

  async function remove(id, name) {
    if (!confirm(`Supprimer le type « ${name} » ? Les traitements passés le conservent.`)) return;
    const r = await fetch(`/api/meeting-types/${id}`, {method: "DELETE"});
    if (!r.ok) alert((await r.json()).error || "Suppression impossible.");
    await load();
  }

  // ── Délégation d'événements ───────────────────────────────────────────────
  document.addEventListener("click", (ev) => {
    const palette = ev.target.closest(".mt-palette");
    if (palette) {
      $("mt-primary").value = "#" + palette.dataset.primary.toLowerCase();
      $("mt-accent").value = "#" + palette.dataset.accent.toLowerCase();
      $("mt-light").value = "#" + palette.dataset.light.toLowerCase();
      refreshPreview();
      return;
    }
    const move = ev.target.closest("[data-move]");
    if (move) {
      const li = move.closest("li");
      const delta = parseInt(move.dataset.move, 10);
      const sibling = delta < 0 ? li.previousElementSibling : li.nextElementSibling;
      if (sibling) (delta < 0 ? li.parentNode.insertBefore(li, sibling)
                              : li.parentNode.insertBefore(sibling, li));
      return;
    }
    const btn = ev.target.closest("[data-action]");
    if (!btn) return;
    ev.preventDefault();
    const id = btn.dataset.id;
    if (btn.dataset.action === "duplicate") {
      const source = state.catalog.custom.find((t) => t.id === id);
      openEditor(source ? source.definition : state.catalog.builtin.find((t) => t.name === btn.dataset.name), null);
    } else if (btn.dataset.action === "edit") {
      const tpl = state.catalog.custom.find((t) => t.id === id);
      openEditor({...tpl.definition, name: tpl.name}, id);
    } else if (btn.dataset.action === "share") {
      share(id, btn.dataset.scope, btn.dataset.group);
    } else if (btn.dataset.action === "delete") {
      remove(id, btn.dataset.name);
    }
  });

  window.MT = {closeEditor, refreshPreview, downloadPreview, save, clearLogo, importFile,
               addField: () => addField(null), addExtract: () => addExtract(null)};
  load();
})();
