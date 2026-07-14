/*
 * job_result_page.js — panneau « Affiner les livrables avec l'assistant » de la
 * page Résultat (chat d'affinage LLM : discuter/appliquer, versions restaurables,
 * options de rendu sans LLM).
 *
 * Extrait du bloc inline de job_result.html (vague A3). Le job courant vient de
 * l'attribut data-job-id du panneau ; les chaînes traduites passent par t()
 * (window.I18N — les msgid sont déclarés dans transcria/i18n/js_catalog.py).
 */
(function () {
  "use strict";
  const jobId = document.getElementById("refine-chat").dataset.jobId;
  const api = (p) => `/api/jobs/${jobId}/refine${p}`;
  const el = (id) => document.getElementById(id);
  const thread = el("refine-thread"), input = el("refine-input");
  let busy = false, pollTimer = null;
  let lastProposal = null;   // dernière « Proposition d'application » du fil

  // Chaînes traduites — mêmes msgid que l'ancien bloc inline (catalogue JS window.I18N).
  const T = {
    proposition: t('Proposition'),
    applyProposal: t('Appliquer cette proposition'),
    version: t('Version'),
    refineApplied: t('Affinage appliqué (version v{v}) — les documents ci-dessus sont régénérés à chaque téléchargement et incluent vos modifications.'),
    describeChange: t("Décrivez la modification à appliquer, ou cliquez « Appliquer cette proposition » sous une réponse de l'assistant."),
    writeFirst: t("Écrivez d'abord votre demande."),
    applyHead: t('Appliquer aux documents :'),
    applyFoot: t('Une version restaurable sera créée (retour arrière possible).'),
    error: t('Erreur'),
    networkError: t('Erreur réseau — réessayez.'),
    invalidOptions: t('Options invalides'),
    revertConfirm: t('Restaurer la version v{v} ? Les documents reviendront à cet état.'),
    revertFailed: t('Restauration impossible'),
  };

  function showError(msg) {
    const box = el("refine-error");
    box.textContent = msg; box.classList.remove("d-none");
    setTimeout(() => box.classList.add("d-none"), 8000);
  }

  function renderTurns(turns) {
    thread.style.display = turns.length ? "block" : "none";
    thread.innerHTML = "";
    lastProposal = null;
    for (const t of turns) {
      if (t.role === "assistant" && t.proposal) lastProposal = t.proposal;
      const div = document.createElement("div");
      if (t.role === "user") {
        div.className = "text-end mb-2";
        div.innerHTML = '<span class="badge bg-primary-subtle text-primary-emphasis" style="white-space:pre-wrap;text-align:left;max-width:85%;display:inline-block;font-weight:normal;font-size:0.875rem;"></span>';
      } else if (t.role === "system") {
        div.className = "text-center mb-2";
        div.innerHTML = '<span class="text-muted fst-italic small"></span>';
      } else {
        div.className = "text-start mb-2";
        div.innerHTML = '<span class="badge bg-secondary-subtle text-body" style="white-space:pre-wrap;text-align:left;max-width:85%;display:inline-block;font-weight:normal;font-size:0.875rem;"></span>';
      }
      div.firstChild.textContent = t.text;   // textContent : jamais d'injection HTML
      thread.appendChild(div);
      if (t.role === "assistant" && t.proposal) {
        // Proposition d'application (extraite côté serveur) : consentement explicite —
        // l'utilisateur voit EXACTEMENT ce qui sera appliqué avant de cliquer.
        const card = document.createElement("div");
        card.className = "text-start mb-2 ms-3";
        card.innerHTML =
          '<div class="border border-primary-subtle rounded p-2 d-inline-block" style="max-width:85%;background:var(--bs-primary-bg-subtle);">' +
          '<div class="small mb-1"><i class="bi bi-lightbulb"></i> <strong>' + T.proposition + '</strong> : <span class="prop-text"></span></div>' +
          '<button type="button" class="btn btn-primary btn-sm refine-proposal-btn">' +
          '<i class="bi bi-magic"></i> ' + T.applyProposal + '</button></div>';
        card.querySelector(".prop-text").textContent = t.proposal;
        card.querySelector(".refine-proposal-btn").addEventListener("click", () => submit("apply", t.proposal));
        thread.appendChild(card);
      }
    }
    thread.scrollTop = thread.scrollHeight;
  }

  function setBusy(b) {
    busy = b;
    el("refine-busy").classList.toggle("d-none", !b);
    el("refine-discuss").disabled = b;
    el("refine-apply").disabled = b;
  }

  function renderVersions(versions) {
    const sel = el("refine-versions"), btn = el("refine-revert");
    sel.classList.toggle("d-none", !versions.length);
    btn.classList.toggle("d-none", !versions.length);
    sel.innerHTML = versions.map(v => `<option value="${v}">${T.version} v${v}</option>`).join("");
    sel.selectedIndex = sel.options.length - 1;
    // Note près des boutons de téléchargement : après un affinage, ils servent
    // toujours la dernière version (les documents sont régénérés à chaque clic).
    const note = el("refine-fresh-note");
    if (note) {
      note.classList.toggle("d-none", !versions.length);
      if (versions.length) {
        note.querySelector("span").textContent =
          T.refineApplied.replace("{v}", versions[versions.length - 1]);
      }
    }
  }

  function renderOptions(data) {
    const themeSel = el("refine-theme");
    if (themeSel.options.length <= 1 && data.themes) {
      for (const t of data.themes) {
        const o = document.createElement("option"); o.value = t; o.textContent = t;
        themeSel.appendChild(o);
      }
    }
    const opts = data.render_options || {};
    if (opts.theme) themeSel.value = opts.theme;
    const sections = opts.sections || {};
    for (const key of ["participants", "transcript", "quality"]) {
      if (key in sections) el(`refine-sec-${key}`).checked = sections[key];
    }
  }

  async function refresh() {
    try {
      const r = await fetch(api("/chat"));
      if (!r.ok) return;
      const data = await r.json();
      renderTurns(data.turns || []);
      renderVersions(data.versions || []);
      renderOptions(data);
      setBusy(!!data.busy);
      if (data.busy && !pollTimer) pollTimer = setInterval(refresh, 4000);
      if (!data.busy && pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    } catch (e) { /* réseau : on retentera au prochain poll */ }
  }

  async function submit(kind, messageOverride) {
    const fromInput = messageOverride == null;
    let message = (fromInput ? input.value : messageOverride).trim();
    if (!message && kind === "apply" && lastProposal) {
      // Zone vide mais une proposition existe dans le fil : on applique CELLE-LÀ
      // (le confirm ci-dessous la montre en toutes lettres avant d'agir).
      message = lastProposal;
    }
    if (!message) {
      showError(kind === "apply" ? T.describeChange : T.writeFirst);
      return;
    }
    if (kind === "apply") {
      // Confirmation EXPLICITE : l'utilisateur relit la demande exacte qui sera appliquée.
      const preview = message.length > 240 ? message.slice(0, 240) + "…" : message;
      if (!confirm(T.applyHead + "\n\n« " + preview + " »\n\n" + T.applyFoot)) return;
    }
    setBusy(true);
    try {
      const r = await fetch(api(""), {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind, message }),
      });
      const data = await r.json();
      if (!r.ok) { setBusy(false); showError(data.error || T.error); return; }
      if (fromInput) input.value = "";
      if (!pollTimer) pollTimer = setInterval(refresh, 4000);
      refresh();
    } catch (e) { setBusy(false); showError(T.networkError); }
  }

  el("refine-discuss").addEventListener("click", () => submit("discuss"));
  el("refine-apply").addEventListener("click", () => submit("apply"));
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); submit("discuss"); }
  });

  el("refine-options-apply").addEventListener("click", async () => {
    const payload = {
      sections: {
        participants: el("refine-sec-participants").checked,
        transcript: el("refine-sec-transcript").checked,
        quality: el("refine-sec-quality").checked,
      },
    };
    const theme = el("refine-theme").value;
    if (theme) payload.theme = theme;
    try {
      const r = await fetch(api("/render-options"), {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await r.json();
      if (!r.ok) { showError(data.error || T.invalidOptions); return; }
      refresh();
    } catch (e) { showError(T.networkError); }
  });

  el("refine-revert").addEventListener("click", async () => {
    const v = parseInt(el("refine-versions").value, 10);
    if (!v || !confirm(T.revertConfirm.replace("{v}", v))) return;
    try {
      const r = await fetch(api("/revert"), {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ version: v }),
      });
      const data = await r.json();
      if (!r.ok) { showError(data.error || T.revertFailed); return; }
      refresh();
    } catch (e) { showError(T.networkError); }
  });

  refresh();
})();
