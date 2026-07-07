/* Atelier d'édition de transcription (lot B — docs/EDITEUR_SRT_INTEGRE.md §7).
   Vanilla JS. Principes gravés : le texte EST le champ (zéro mode, zéro modale),
   icônes au survol, pause auto à la frappe (D3), 3 filets de sauvegarde (D2 :
   undo/redo par deltas, brouillon serveur ~5 s, « Enregistrer une version »).
   Rendu paresseux natif des cartes (content-visibility) — pas de virtualisation JS. */
(function () {
  "use strict";

  // Alias du helper i18n (window.t chargé par i18n.js avant ce script). `_t` évite toute
  // collision avec les variables locales `t` (ex. review.points.forEach((t) => …)).
  const _t = window.t;

  const root = document.getElementById("se-root");
  const JOB = root.dataset.jobId;
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
    {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));

  // Palette locuteurs : 12 teintes calibrées (contraste AA sur fond atelier),
  // affectation STABLE par ordre de temps de parole (§7.1).
  const COLORS = ["#2563eb", "#d97706", "#059669", "#dc2626", "#7c3aed", "#0891b2",
                  "#be185d", "#65a30d", "#b45309", "#4f46e5", "#0d9488", "#9f1239"];

  const state = {
    chunks: [],            // {start_ms, end_ms, speaker_id, speaker_name, text}
    srtSha: "",
    revision: 0,
    audio: {available: false, duration_ms: null},
    readonly: false,
    speakerColors: new Map(),
    dirty: new Set(),      // indices modifiés depuis la dernière version
    undo: [], redo: [],    // piles de deltas
    activeIndex: -1,
    segmentStopAt: null,   // fin d'écoute « ce segment seulement »
    wasPlayingBeforeType: false,
    pausedInSegment: null, // segment mis en pause par reclic ▶ (reprise au 3ᵉ clic)
    draftTimer: null,
    draftBlocked: false,   // 409 : un autre onglet a la main
    view: "fresque",       // fresque | lanes (bascule — retour utilisateur)
    peaks: null,           // Uint8Array des pics serveur (lot C)
    peaksMeta: null,
    zoom: {centerMs: 0, spanMs: 60000},   // fenêtre de la bande zoomée
    markers: [],           // [{at_ms, label}] — persistés au brouillon
    dragging: null,        // poignée en cours : {i, side}
    selection: new Set(),  // indices sélectionnés (shift-clic) — barre flottante
    solo: null,            // speaker_id en écoute solo (saute les autres segments)
    visited: new Set(),    // chunks visités (jauge de relecture, persistée au brouillon)
    searchHits: [], searchPos: -1,
    review: {points: [], anchors: [], done: new Set()},   // points qualité (tiroir)
  };

  const audio = $("se-audio");

  // ── Utilitaires temps ──────────────────────────────────────────────────────
  const fmt = (ms) => {
    ms = Math.max(0, Math.round(ms));
    const h = Math.floor(ms / 3600000), m = Math.floor(ms / 60000) % 60,
          s = Math.floor(ms / 1000) % 60;
    return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  };
  const fmtMs = (ms) => `${fmt(ms)},${String(Math.round(ms) % 1000).padStart(3, "0")}`;
  const parseTs = (v) => {
    const m = String(v).trim().match(/^(?:(\d+):)?(\d{1,2}):(\d{1,2})(?:[,.](\d{1,3}))?$/);
    if (!m) return null;
    return ((+(m[1] || 0) * 60 + +m[2]) * 60 + +m[3]) * 1000 + +String(m[4] || "0").padEnd(3, "0");
  };

  function speakerColor(id) {
    if (!id) return "#94a3b8";
    if (!state.speakerColors.has(id)) {
      state.speakerColors.set(id, COLORS[state.speakerColors.size % COLORS.length]);
    }
    return state.speakerColors.get(id);
  }

  function speakerLabel(c) {
    return c.speaker_name || c.speaker_id || "—";
  }

  function knownSpeakers() {
    const seen = new Map();
    for (const c of state.chunks) {
      if (c.speaker_id && !seen.has(c.speaker_id)) {
        seen.set(c.speaker_id, c.speaker_name || null);
      }
    }
    return seen;
  }

  // ── Chargement ─────────────────────────────────────────────────────────────
  async function load() {
    const r = await fetch(`/api/jobs/${JOB}/editor/state`);
    if (!r.ok) { banner("warn", _t("Impossible de charger la transcription.")); return; }
    const data = await r.json();
    state.srtSha = data.srt_sha256;
    state.audio = data.audio;
    state.readonly = data.readonly;
    // couleurs stables : par temps de parole décroissant (stats), puis inconnus
    const stats = (data.speakers.stats.speakers || []);
    for (const s of stats) speakerColor(s.speaker_id);
    for (const id of Object.keys((data.speakers.mapping || {}).mapping || {})) initialSpeakerIds.add(id);
    for (const c of data.chunks) if (c.speaker_id) initialSpeakerIds.add(c.speaker_id);

    if (data.draft.exists && data.draft.chunk_count > 0) {
      showResume(data);
    } else {
      state.chunks = data.chunks;
      state.revision = data.draft.revision || 0;
      start(data);
    }
  }

  function showResume(data) {
    $("se-resume").classList.remove("d-none");
    const when = data.draft.updated_at ? new Date(data.draft.updated_at).toLocaleString("fr-FR") : "?";
    $("se-resume-detail").innerHTML =
      _t("Un brouillon du <strong>%(when)s</strong> (%(n)s segments) a été trouvé.", { when: esc(when), n: data.draft.chunk_count }) +
      (data.draft.conflict
        ? "<br><span class='text-warning'>⚠ " +
          _t("La transcription a changé depuis (correction ou affinage) : reprendre le brouillon écrasera ces changements à l'enregistrement.") +
          "</span>"
        : "");
    $("se-resume-yes").onclick = async () => {
      const dr = await fetch(`/api/jobs/${JOB}/editor/state`);
      const fresh = await dr.json();
      // le brouillon complet n'est pas dans state → un endpoint léger suffirait ; v1 :
      // le brouillon EST la vérité côté serveur, on le recharge via son fichier exposé
      const draft = await (await fetch(`/api/jobs/${JOB}/editor/draft`)).json();
      state.chunks = draft.chunks;
      state.revision = draft.revision || 0;
      state.markers = draft.markers || [];
      state.visited = new Set((draft.progress || {}).visited || []);
      state.review.done = new Set((draft.progress || {}).reviewed || []);
      for (const c of state.chunks) if (c.speaker_id) speakerColor(c.speaker_id);
      $("se-resume").classList.add("d-none");
      start(fresh, {fromDraft: true});
    };
    $("se-resume-no").onclick = async () => {
      await fetch(`/api/jobs/${JOB}/editor/draft`, {method: "DELETE"});
      state.chunks = data.chunks;
      state.revision = 0;
      $("se-resume").classList.add("d-none");
      start(data);
    };
  }

  function start(data, opts) {
    $("se-main").classList.remove("d-none");
    if (state.readonly) {
      banner("info", _t("Un traitement est en cours sur ce dossier — l'éditeur est en lecture seule et rouvrira en écriture à la fin."));
    }
    if (!state.audio.available) {
      banner("warn", _t("Audio non disponible sur cette installation — l'écoute et la forme d'onde sont désactivées, toutes les éditions restent possibles."));
      $("se-player-wrap").querySelectorAll("button, select").forEach((b) => { b.disabled = true; });
    } else {
      audio.src = `/api/jobs/${JOB}/audio/stream`;
      loadPeaks();
    }
    renderList();
    drawFresque();
    state.zoom.spanMs = Math.min(60000, totalMs());
    drawZoomBand();
    renderMarkerChips();
    state.review.points = data.review_points || [];
    state.review.anchors = data.review_anchors || [];
    renderReviewMenu();
    if (opts && opts.fromDraft) {
      for (let i = 0; i < state.chunks.length; i++) state.dirty.add(i);
      setSaveState(_t("brouillon repris — pensez à enregistrer une version"), "saving");
    } else {
      setSaveState("aucune modification");
    }
  }

  async function loadPeaks(attempt) {
    const r = await fetch(`/api/jobs/${JOB}/editor/peaks`);
    if (r.status === 202) {           // génération en cours côté serveur
      if ((attempt || 0) < 30) setTimeout(() => loadPeaks((attempt || 0) + 1), 1500);
      return;
    }
    if (!r.ok) return;                // best-effort : la bande vit sans forme d'onde
    state.peaksMeta = JSON.parse(r.headers.get("X-Peaks-Meta") || "{}");
    state.peaks = new Uint8Array(await r.arrayBuffer());
    drawZoomBand();
  }

  function totalMs() {
    return state.audio.duration_ms
      || (state.chunks.length ? state.chunks[state.chunks.length - 1].end_ms : 1);
  }

  function banner(kind, text) {
    const div = document.createElement("div");
    div.className = `se-banner ${kind}`;
    div.innerHTML = `<i class="bi ${kind === "warn" ? "bi-exclamation-triangle" : "bi-info-circle"} me-1"></i>${esc(text)}`;
    $("se-banners").appendChild(div);
  }

  // ── Rendu de la liste (cartes paresseuses) ─────────────────────────────────
  function cardHtml(c, i) {
    const color = speakerColor(c.speaker_id);
    return `
      <div class="se-card${state.dirty.has(i) ? " dirty" : ""}" data-i="${i}" style="--se-color:${color}">
        <div class="se-card-head">
          <span class="se-speaker-chip" data-act="speaker" title="${_t('Changer le locuteur')}">
            <span class="se-speaker-dot"></span>${esc(speakerLabel(c))}</span>
          <span class="se-times" data-act="seek" title="${_t('Aller à cet instant')}">${fmt(c.start_ms)} → ${fmt(c.end_ms)}</span>
          <span class="se-dirty-dot" title="${_t('Modifié depuis la dernière version')}"></span>
          ${i > 0 && c.start_ms < state.chunks[i - 1].end_ms
            ? `<span class="se-overlap-pill" title="${_t('Commence avant la fin du segment précédent')}">${_t('chevauche')}</span>` : ""}
          <span class="ms-auto">#${i + 1}</span>
        </div>
        <div class="se-text" ${state.readonly ? "" : 'contenteditable="true"'} spellcheck="true">${esc(c.text)}</div>
        <div class="se-actions">
          ${state.audio.available ? `<button class="btn btn-outline-secondary" data-act="play" title="${_t('Écouter ce segment')}">▶</button>` : ""}
          <button class="btn btn-outline-secondary" data-act="speaker" title="${_t('Changer le locuteur')}">🗣</button>
          <button class="btn btn-outline-secondary" data-act="split" title="${_t('Couper au curseur (C)')}">✂</button>
          <button class="btn btn-outline-secondary" data-act="merge" title="${_t('Fusionner avec le précédent')}">⧉</button>
          <button class="btn btn-outline-secondary" data-act="timing" title="${_t('Ajuster début/fin')}">⏱</button>
          <button class="btn btn-outline-danger" data-act="delete" title="${_t('Supprimer ce segment')}">🗑</button>
        </div>
        <div class="se-timing">
          <label>${_t('Début')} <input class="form-control form-control-sm se-t-start" value="${fmtMs(c.start_ms)}"></label>
          <label>${_t('Fin')} <input class="form-control form-control-sm se-t-end" value="${fmtMs(c.end_ms)}"></label>
          <span class="text-muted">${_t('Début')}</span>
          <span class="btn-group btn-group-sm">
            <button class="btn btn-outline-secondary" data-tshift="start:-100">−100 ms</button>
            <button class="btn btn-outline-secondary" data-tshift="start:100">+100 ms</button>
          </span>
          <span class="text-muted">${_t('Fin')}</span>
          <span class="btn-group btn-group-sm">
            <button class="btn btn-outline-secondary" data-tshift="end:-100">−100 ms</button>
            <button class="btn btn-outline-secondary" data-tshift="end:100">+100 ms</button>
          </span>
          <span class="btn-group btn-group-sm">
            <button class="btn btn-outline-secondary" data-cascade="-500" title="${_t('Avancer ce segment ET tous les suivants de 500 ms')}">${_t('⇤ suivants −500 ms')}</button>
            <button class="btn btn-outline-secondary" data-cascade="500" title="${_t('Reculer ce segment ET tous les suivants de 500 ms')}">${_t('suivants +500 ms ⇥')}</button>
          </span>
          <span class="text-muted">${_t('S/E : caler début/fin sur la lecture')}</span>
        </div>
      </div>`;
  }

  function renderList() {
    $("se-list").innerHTML = state.chunks.map((c, i) => cardHtml(c, i)).join("");
    $("se-meta").textContent =
      _t("%(seg)s segments · %(spk)s locuteurs", { seg: state.chunks.length, spk: knownSpeakers().size }) +
      (state.audio.duration_ms ? ` · ${fmt(state.audio.duration_ms)}` : "");
  }

  function rerenderCard(i) {
    const el = $("se-list").querySelector(`.se-card[data-i="${i}"]`);
    if (!el) { renderList(); return; }
    const tmp = document.createElement("div");
    tmp.innerHTML = cardHtml(state.chunks[i], i);
    el.replaceWith(tmp.firstElementChild);
  }

  // ── Deltas undo/redo (D2, filet n°1) ───────────────────────────────────────
  function pushUndo(delta) {
    state.undo.push(delta);
    if (state.undo.length > 300) state.undo.shift();
    state.redo.length = 0;
    updateUndoButtons();
    markDirtyFromDelta(delta);
    scheduleDraft();
  }

  function markDirtyFromDelta(delta) {
    if (typeof delta.i === "number") state.dirty.add(delta.i);
    setSaveState(_t("modifications non enregistrées"), "saving");
  }

  function applyDelta(delta, direction) {
    const forward = direction === "redo";
    if (delta.op === "edit") {
      state.chunks[delta.i] = structuredClone(forward ? delta.after : delta.before);
      rerenderCard(delta.i);
    } else if (delta.op === "split") {
      if (forward) {
        state.chunks.splice(delta.i, 1, structuredClone(delta.after[0]), structuredClone(delta.after[1]));
      } else {
        state.chunks.splice(delta.i, 2, structuredClone(delta.before));
      }
      renderList();
    } else if (delta.op === "merge") {
      if (forward) {
        state.chunks.splice(delta.i - 1, 2, structuredClone(delta.after));
      } else {
        state.chunks.splice(delta.i - 1, 1, structuredClone(delta.before[0]), structuredClone(delta.before[1]));
      }
      renderList();
    } else if (delta.op === "range") {
      // remplacement d'une plage (fusion/suppression/attribution de sélection)
      if (forward) state.chunks.splice(delta.start, delta.before.length, ...structuredClone(delta.after));
      else state.chunks.splice(delta.start, delta.after.length, ...structuredClone(delta.before));
      renderList();
    } else if (delta.op === "shift") {
      // décalage en cascade des segments à partir de delta.start
      const sign = forward ? 1 : -1;
      for (let k = delta.start; k < state.chunks.length; k++) {
        state.chunks[k].start_ms += sign * delta.delta;
        state.chunks[k].end_ms += sign * delta.delta;
      }
      renderList();
    } else if (delta.op === "delete") {
      if (forward) state.chunks.splice(delta.i, 1);
      else state.chunks.splice(delta.i, 0, structuredClone(delta.before));
      renderList();
    }
    drawFresque();
    scheduleDraft();
  }

  function undo() {
    const delta = state.undo.pop();
    if (!delta) return;
    applyDelta(delta, "undo");
    state.redo.push(delta);
    updateUndoButtons();
  }

  function redo() {
    const delta = state.redo.pop();
    if (!delta) return;
    applyDelta(delta, "redo");
    state.undo.push(delta);
    updateUndoButtons();
  }

  function updateUndoButtons() {
    $("se-undo").disabled = !state.undo.length;
    $("se-redo").disabled = !state.redo.length;
  }

  // ── Gestes d'édition ───────────────────────────────────────────────────────
  function editChunk(i, mutate) {
    const before = structuredClone(state.chunks[i]);
    mutate(state.chunks[i]);
    pushUndo({op: "edit", i, before, after: structuredClone(state.chunks[i])});
  }

  function commitText(i, el) {
    const text = el.innerText.replace(/ /g, " ");
    if (text === state.chunks[i].text) return;
    editChunk(i, (c) => { c.text = text; });
    rerenderCard(i);
  }

  function splitAtCursor(i, cardEl) {
    const c = state.chunks[i];
    const sel = window.getSelection();
    const textEl = cardEl.querySelector(".se-text");
    let offset = Math.floor(c.text.length / 2);
    if (sel && sel.rangeCount && textEl.contains(sel.anchorNode)) {
      const range = sel.getRangeAt(0).cloneRange();
      range.selectNodeContents(textEl);
      range.setEnd(sel.anchorNode, sel.anchorOffset);
      offset = range.toString().length;
    }
    offset = Math.max(1, Math.min(c.text.length - 1, offset));
    const ratio = offset / Math.max(1, c.text.length);
    const cut = Math.round(c.start_ms + (c.end_ms - c.start_ms) * ratio);
    const first = {...c, end_ms: cut, text: c.text.slice(0, offset).trimEnd()};
    const second = {...c, start_ms: cut, text: c.text.slice(offset).trimStart()};
    pushUndo({op: "split", i, before: structuredClone(c), after: [first, second]});
    state.chunks.splice(i, 1, first, second);
    renderList();
    drawFresque();
  }

  function mergeWithPrevious(i) {
    if (i === 0) return;
    const prev = state.chunks[i - 1], cur = state.chunks[i];
    const merged = {...prev, end_ms: Math.max(prev.end_ms, cur.end_ms),
                    text: `${prev.text.trimEnd()} ${cur.text.trimStart()}`};
    pushUndo({op: "merge", i, before: [structuredClone(prev), structuredClone(cur)], after: structuredClone(merged)});
    state.chunks.splice(i - 1, 2, merged);
    renderList();
    drawFresque();
  }

  function deleteChunk(i) {
    const c = state.chunks[i];
    if (c.text.trim() && !confirm(_t("Supprimer le segment #%(n)s et son texte ?", { n: i + 1 }))) return;
    pushUndo({op: "delete", i, before: structuredClone(c)});
    state.chunks.splice(i, 1);
    renderList();
    drawFresque();
  }

  // Menu locuteur (popover, création à la volée)
  function openSpeakerMenu(i, anchor) {
    closeSpeakerMenu();
    const menu = document.createElement("div");
    menu.className = "se-speaker-menu";
    menu.id = "se-speaker-menu";
    let items = "";
    for (const [id, name] of knownSpeakers()) {
      items += `<button data-sid="${esc(id)}" data-sname="${esc(name || "")}">
        <span class="se-speaker-dot" style="background:${speakerColor(id)}"></span>
        ${esc(name || id)}</button>`;
    }
    menu.innerHTML = items + `
      <div class="border-top mt-1 pt-1 d-flex gap-1">
        <input class="form-control form-control-sm" id="se-new-speaker" placeholder="${_t('Nouveau locuteur…')}">
        <button class="btn btn-sm btn-primary" id="se-new-speaker-ok">OK</button>
      </div>`;
    document.body.appendChild(menu);
    const rect = anchor.getBoundingClientRect();
    menu.style.left = `${Math.min(rect.left + window.scrollX, window.scrollX + window.innerWidth - 270)}px`;
    menu.style.top = `${rect.bottom + window.scrollY + 4}px`;
    menu.addEventListener("click", (ev) => {
      const btn = ev.target.closest("button[data-sid]");
      if (btn) { assignSpeaker(i, btn.dataset.sid, btn.dataset.sname || null); closeSpeakerMenu(); }
    });
    $("se-new-speaker-ok").onclick = () => {
      const name = $("se-new-speaker").value.trim();
      if (!name) return;
      const nums = [...knownSpeakers().keys()]
        .map((s) => parseInt((s.match(/^SPEAKER_(\d+)$/) || [])[1], 10)).filter(Number.isFinite);
      const nextId = `SPEAKER_${String(Math.max(-1, ...nums) + 1).padStart(2, "0")}`;
      assignSpeaker(i, nextId, name);
      closeSpeakerMenu();
    };
    $("se-new-speaker").focus();
  }

  function closeSpeakerMenu() {
    const menu = $("se-speaker-menu");
    if (menu) menu.remove();
  }

  function assignSpeaker(i, sid, sname) {
    if (i === -1) {   // attribution à TOUTE la sélection (barre flottante)
      const sorted = [...state.selection].sort((a, b) => a - b);
      if (!sorted.length) return;
      const before = sorted.map((k) => structuredClone(state.chunks[k]));
      const after = before.map((c) => ({...c, speaker_id: sid, speaker_name: sname || null}));
      // la sélection peut être non contiguë : deltas unitaires groupés
      for (let k = 0; k < sorted.length; k++) {
        pushUndo({op: "edit", i: sorted[k], before: before[k], after: structuredClone(after[k])});
        state.chunks[sorted[k]] = after[k];
      }
      clearSelection();
      renderList(); drawFresque(); drawZoomBand();
      return;
    }
    editChunk(i, (c) => { c.speaker_id = sid; c.speaker_name = sname || null; });
    rerenderCard(i);
    drawFresque();
  }

  // ── Audio : synchro, segment, pause à la frappe (D3) ──────────────────────
  function chunkAt(ms) {
    let lo = 0, hi = state.chunks.length - 1, best = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (state.chunks[mid].start_ms <= ms) { best = mid; lo = mid + 1; } else hi = mid - 1;
    }
    return best;
  }

  function setActive(i, scroll) {
    // IDEMPOTENT : renderList() (fusion, attribution…) efface les classes — on
    // ré-applique toujours l'état visuel, mais on ne re-scrolle qu'au CHANGEMENT.
    const changed = i !== state.activeIndex;
    const prev = $("se-list").querySelector(".se-card.active");
    if (prev && +prev.dataset.i !== i) prev.classList.remove("active");
    state.activeIndex = i;
    refreshCardPlayIcons();
    if (i < 0) return;
    const el = $("se-list").querySelector(`.se-card[data-i="${i}"]`);
    if (el) {
      el.classList.add("active");
      if (changed && scroll && $("se-follow").checked && document.activeElement?.className !== "se-text") {
        el.scrollIntoView({block: "center", behavior: "smooth"});
      }
    }
  }

  audio.addEventListener("timeupdate", () => {
    const ms = audio.currentTime * 1000;
    $("se-time").textContent = `${fmt(ms)} / ${state.audio.duration_ms ? fmt(state.audio.duration_ms) : "—"}`;
    if (state.segmentStopAt !== null && ms >= state.segmentStopAt) {
      audio.pause();
      state.segmentStopAt = null;
    }
    if (state.solo && !audio.paused) {
      const cur = chunkAt(ms);
      if (cur >= 0 && (state.chunks[cur].speaker_id || "—") !== state.solo) {
        const next = state.chunks.findIndex((c, k) => k > cur && (c.speaker_id || "—") === state.solo);
        if (next >= 0) audio.currentTime = state.chunks[next].start_ms / 1000;
        else { audio.pause(); setSolo(null); }
      }
    }
    const activeNow = chunkAt(ms);
    if (activeNow >= 0 && !audio.paused) { state.visited.add(activeNow); updateGauge(); }
    setActive(activeNow, true);
    drawPlayhead(ms);
    // la bande zoomée suit la lecture (recentrage quand la tête sort de la fenêtre)
    const zoomWin = zoomWindow();
    if (ms < zoomWin.from || ms > zoomWin.to) { state.zoom.centerMs = ms + zoomWin.span * 0.3; }
    drawZoomBand();
    if (state.view === "lanes") drawLanes();
  });
  function refreshCardPlayIcons() {
    document.querySelectorAll('.se-card [data-act="play"]').forEach((b) => { b.textContent = "▶"; });
    if (!audio.paused && state.activeIndex >= 0) {
      const btn = $("se-list").querySelector(`.se-card[data-i="${state.activeIndex}"] [data-act="play"]`);
      if (btn) btn.textContent = "⏸";
    }
  }
  audio.addEventListener("play", () => {
    $("se-play").innerHTML = '<i class="bi bi-pause-fill"></i>';
    state.pausedInSegment = null;
    refreshCardPlayIcons();
  });
  audio.addEventListener("pause", () => {
    $("se-play").innerHTML = '<i class="bi bi-play-fill"></i>';
    if (state.segmentStopAt !== null) state.pausedInSegment = state.activeIndex;
    refreshCardPlayIcons();
  });

  function playSegment(i) {
    const c = state.chunks[i];
    const playingThisSegment = !audio.paused
      && audio.currentTime * 1000 >= c.start_ms - 200
      && audio.currentTime * 1000 <= c.end_ms + 200;
    if (playingThisSegment) { audio.pause(); return; }          // reclic = pause
    if (audio.paused && state.pausedInSegment === i) {           // re-reclic = reprise
      state.pausedInSegment = null;
      state.segmentStopAt = c.end_ms;
      audio.play();
      return;
    }
    audio.currentTime = c.start_ms / 1000;
    state.segmentStopAt = c.end_ms;
    audio.play();
  }

  function togglePlay() {
    if (!state.audio.available) return;
    state.segmentStopAt = null;
    if (audio.paused) audio.play(); else audio.pause();
  }

  // ── Fresque globale (canvas) ───────────────────────────────────────────────
  let playheadMs = 0;
  function drawFresque() {
    const canvas = $("se-fresque");
    const width = canvas.clientWidth || canvas.parentElement.clientWidth;
    canvas.width = width * devicePixelRatio;
    const ctx = canvas.getContext("2d");
    ctx.scale(devicePixelRatio, devicePixelRatio);
    ctx.clearRect(0, 0, width, 34);
    const total = state.audio.duration_ms
      || (state.chunks.length ? state.chunks[state.chunks.length - 1].end_ms : 1);
    for (let i = 0; i < state.chunks.length; i++) {
      const c = state.chunks[i];
      ctx.fillStyle = speakerColor(c.speaker_id);
      const x = (c.start_ms / total) * width;
      const w = Math.max(1, ((c.end_ms - c.start_ms) / total) * width);
      ctx.fillRect(x, 6, w, 22);
      if (i && c.start_ms < state.chunks[i - 1].end_ms) {   // chevauchement hachuré
        ctx.fillStyle = "rgba(146, 64, 14, 0.55)";
        const ox = (c.start_ms / total) * width;
        const ow = Math.max(2, ((state.chunks[i - 1].end_ms - c.start_ms) / total) * width);
        ctx.fillRect(ox, 2, ow, 30);
      }
    }
    // repères (triangles) + cadre de la fenêtre zoomée
    ctx.fillStyle = "#0d6efd";
    for (const m of state.markers) {
      const x = (m.at_ms / total) * width;
      ctx.beginPath(); ctx.moveTo(x - 4, 0); ctx.lineTo(x + 4, 0); ctx.lineTo(x, 6); ctx.fill();
    }
    if (state.audio.available) {
      ctx.strokeStyle = "rgba(37, 99, 235, 0.9)";
      ctx.lineWidth = 1.5;
      const zx = ((state.zoom.centerMs - state.zoom.spanMs / 2) / total) * width;
      const zw = (state.zoom.spanMs / total) * width;
      ctx.strokeRect(Math.max(0, zx), 1, Math.min(width, zw), 32);
    }
    drawPlayhead(playheadMs, true);
    drawLanes();
  }

  // ── Lanes par locuteur (bascule — retour utilisateur) ──────────────────────
  function laneSpeakers() {
    const totals = new Map();
    for (const c of state.chunks) {
      const id = c.speaker_id || "—";
      totals.set(id, (totals.get(id) || 0) + (c.end_ms - c.start_ms));
    }
    return [...totals.entries()].sort((a, b) => b[1] - a[1]).map(([id]) => id);
  }

  function drawLanes() {
    const canvas = $("se-lanes");
    if (canvas.classList.contains("d-none")) return;
    const width = canvas.clientWidth || canvas.parentElement.clientWidth;
    const speakers = laneSpeakers();
    const rowH = 20, labelW = 150;
    canvas.height = Math.max(1, speakers.length) * rowH * devicePixelRatio;
    canvas.style.height = `${speakers.length * rowH}px`;
    canvas.width = width * devicePixelRatio;
    const ctx = canvas.getContext("2d");
    ctx.scale(devicePixelRatio, devicePixelRatio);
    ctx.clearRect(0, 0, width, speakers.length * rowH);
    const total = totalMs();
    const names = new Map();
    for (const c of state.chunks) if (c.speaker_id && !names.has(c.speaker_id)) names.set(c.speaker_id, c.speaker_name);
    speakers.forEach((id, row) => {
      const y = row * rowH;
      ctx.fillStyle = row % 2 ? "rgba(148, 163, 184, 0.08)" : "transparent";
      ctx.fillRect(0, y, width, rowH);
      ctx.fillStyle = "#334155";
      ctx.font = "11px system-ui";
      ctx.fillText((names.get(id) || id).slice(0, 22), 4, y + 13);
      ctx.fillStyle = speakerColor(id === "—" ? null : id);
      for (const c of state.chunks) {
        if ((c.speaker_id || "—") !== id) continue;
        const x = labelW + (c.start_ms / total) * (width - labelW);
        const w = Math.max(1.5, ((c.end_ms - c.start_ms) / total) * (width - labelW));
        ctx.fillRect(x, y + 4, w, rowH - 8);
      }
    });
    // tête de lecture
    const px = labelW + (playheadMs / total) * (width - labelW);
    ctx.fillStyle = "#dc2626";
    ctx.fillRect(px, 0, 1.5, speakers.length * rowH);
  }

  // ── Bande zoomée : forme d'onde + poignées du segment actif ───────────────
  function zoomWindow() {
    const total = totalMs();
    let span = Math.max(3000, Math.min(total, state.zoom.spanMs));
    let center = Math.max(span / 2, Math.min(total - span / 2, state.zoom.centerMs));
    return {from: center - span / 2, to: center + span / 2, span, total};
  }

  function msToX(ms, zoomWin, width) {
    return ((ms - zoomWin.from) / zoomWin.span) * width;
  }

  function drawZoomBand() {
    const canvas = $("se-zoomband");
    if (!state.audio.available) { canvas.classList.add("d-none"); return; }
    canvas.classList.remove("d-none");
    const width = canvas.clientWidth || canvas.parentElement.clientWidth;
    canvas.width = width * devicePixelRatio;
    const ctx = canvas.getContext("2d");
    ctx.scale(devicePixelRatio, devicePixelRatio);
    ctx.clearRect(0, 0, width, 86);
    const zoomWin = zoomWindow();

    // forme d'onde (pics serveur) — sans pics : fond neutre + mention discrète
    if (state.peaks && state.peaksMeta) {
      const {window_ms} = state.peaksMeta;
      ctx.fillStyle = "#94a3b8";
      const first = Math.max(0, Math.floor(zoomWin.from / window_ms));
      const last = Math.min(state.peaks.length - 1, Math.ceil(zoomWin.to / window_ms));
      const step = Math.max(1, Math.floor((last - first) / width));
      for (let k = first; k <= last; k += step) {
        let peak = 0;
        for (let j = k; j < Math.min(last, k + step); j++) peak = Math.max(peak, state.peaks[j]);
        const x = msToX(k * window_ms, zoomWin, width);
        const h = Math.max(1, (peak / 127) * 62);
        ctx.fillRect(x, 43 - h / 2, Math.max(1, width / (last - first + 1) * step), h);
      }
    } else {
      ctx.fillStyle = "#94a3b8";
      ctx.font = "11px system-ui";
      ctx.fillText(_t("préparation de la forme d'onde…"), 8, 46);
    }

    // segments : liserés colorés en bas + bornes du segment ACTIF en poignées
    for (let i = 0; i < state.chunks.length; i++) {
      const c = state.chunks[i];
      if (c.end_ms < zoomWin.from || c.start_ms > zoomWin.to) continue;
      ctx.fillStyle = speakerColor(c.speaker_id);
      const x1 = Math.max(0, msToX(c.start_ms, zoomWin, width));
      const x2 = Math.min(width, msToX(c.end_ms, zoomWin, width));
      ctx.fillRect(x1, 78, Math.max(1, x2 - x1), 6);
    }
    if (state.activeIndex >= 0) {
      const c = state.chunks[state.activeIndex];
      ctx.fillStyle = "rgba(37, 99, 235, 0.12)";
      const x1 = msToX(c.start_ms, zoomWin, width), x2 = msToX(c.end_ms, zoomWin, width);
      ctx.fillRect(x1, 0, x2 - x1, 78);
      ctx.fillStyle = "#2563eb";
      for (const x of [x1, x2]) {                    // poignées glissables
        ctx.fillRect(x - 1.5, 0, 3, 78);
        ctx.beginPath(); ctx.arc(x, 8, 5, 0, 7); ctx.fill();
      }
    }
    // repères + tête de lecture
    ctx.fillStyle = "#0d6efd";
    for (const m of state.markers) {
      if (m.at_ms < zoomWin.from || m.at_ms > zoomWin.to) continue;
      const x = msToX(m.at_ms, zoomWin, width);
      ctx.beginPath(); ctx.moveTo(x - 5, 0); ctx.lineTo(x + 5, 0); ctx.lineTo(x, 8); ctx.fill();
    }
    ctx.fillStyle = "#dc2626";
    ctx.fillRect(msToX(playheadMs, zoomWin, width) - 1, 0, 2, 86);
  }

  // ── Repères (M) — persistés au brouillon ───────────────────────────────────
  function addMarker() {
    if (!state.audio.available) return;
    const at = Math.round(audio.currentTime * 1000);
    state.markers.push({at_ms: at, label: _t("Repère %(n)s — %(t)s", { n: state.markers.length + 1, t: fmt(at) })});
    renderMarkerChips();
    drawFresque(); drawZoomBand();
    scheduleDraft();
  }

  function renderMarkerChips() {
    $("se-marker-chips").innerHTML = state.markers.map((m, k) =>
      `<span class="se-marker-chip" data-k="${k}" title="${_t('Aller au repère')}">${esc(m.label)}<span class="se-marker-x" data-x="${k}" title="${_t('Retirer')}">×</span></span>`
    ).join("");
  }

  function drawPlayhead(ms) {
    playheadMs = ms;
    // repère léger : re-tracé complet évité — un trait par-dessus à chaque tick suffit
    const canvas = $("se-fresque");
    const width = canvas.clientWidth;
    const total = state.audio.duration_ms
      || (state.chunks.length ? state.chunks[state.chunks.length - 1].end_ms : 1);
    const ctx = canvas.getContext("2d");
    ctx.save();
    ctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
    // nettoie l'ancienne tête en re-traçant la bande (peu coûteux : 1 rect + blocs clip)
    ctx.restore();
    // simplicité v1 : trait tracé au prochain drawFresque complet ; ici un marqueur haut
    const x = (ms / total) * width;
    ctx.save();
    ctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
    ctx.clearRect(0, 0, width, 5);
    ctx.fillStyle = "#dc2626";
    ctx.fillRect(Math.max(0, x - 1), 0, 2, 5);
    ctx.restore();
  }

  $("se-fresque").addEventListener("click", (ev) => {
    if (!state.audio.available) return;
    const rect = ev.currentTarget.getBoundingClientRect();
    const total = state.audio.duration_ms
      || (state.chunks.length ? state.chunks[state.chunks.length - 1].end_ms : 1);
    audio.currentTime = ((ev.clientX - rect.left) / rect.width) * total / 1000;
    state.segmentStopAt = null;
    setActive(chunkAt(audio.currentTime * 1000), true);
  });

  // ── Brouillon serveur (D2, filet n°2) ─────────────────────────────────────
  function scheduleDraft() {
    if (state.readonly || state.draftBlocked) return;
    clearTimeout(state.draftTimer);
    state.draftTimer = setTimeout(pushDraft, 5000);
  }

  async function pushDraft() {
    const r = await fetch(`/api/jobs/${JOB}/editor/draft`, {
      method: "PUT", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        revision: state.revision,
        base_srt_sha256: state.srtSha,
        chunks: state.chunks,
        new_speakers: newSpeakersPayload(),
        markers: state.markers,
        progress: {visited: [...state.visited], reviewed: [...state.review.done]},
      }),
    });
    if (r.status === 409) {
      state.draftBlocked = true;
      banner("warn", _t("Ce dossier est édité dans un autre onglet ou par une autre personne — brouillon suspendu ici pour ne rien écraser."));
      setSaveState(_t("brouillon suspendu (édition ailleurs)"), "error");
      return;
    }
    if (r.ok) {
      state.revision = (await r.json()).revision;
      setSaveState(_t("brouillon enregistré à %(t)s", { t: new Date().toLocaleTimeString() }));
    } else {
      setSaveState(_t("brouillon non enregistré — nouvelle tentative"), "error");
      clearTimeout(state.draftTimer);
      state.draftTimer = setTimeout(pushDraft, 5000);
    }
  }

  const initialSpeakerIds = new Set();   // remplis au chargement (mapping + chunks)

  function newSpeakersPayload() {
    const out = [];
    for (const [id, name] of knownSpeakers()) {
      if (name && !initialSpeakerIds.has(id)) out.push({speaker_id: id, speaker_name: name});
    }
    return out;
  }

  function setSaveState(text, kind) {
    const el = $("se-save-state");
    el.textContent = `● ${text}`;
    el.className = `se-save-state${kind ? " " + kind : ""}`;
  }

  // ── Enregistrer une version (D2, filet n°3) ────────────────────────────────
  async function saveVersion() {
    if (state.readonly) return;
    setSaveState("enregistrement de la version…", "saving");
    const r = await fetch(`/api/jobs/${JOB}/editor/save`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({chunks: state.chunks, edited_count: state.dirty.size,
                            new_speakers: newSpeakersPayload()}),
    });
    const data = await r.json();
    if (!r.ok) { setSaveState(data.error || "enregistrement impossible", "error"); return; }
    state.dirty.clear();
    state.revision = 0;
    const fresh = await (await fetch(`/api/jobs/${JOB}/editor/state`)).json();
    state.srtSha = fresh.srt_sha256;
    renderList();
    setSaveState(_t("version v%(v)s enregistrée — documents à jour au téléchargement", { v: data.version }));
    if (data.warnings && data.warnings.length) {
      banner("warn", _t("Avertissements (non bloquants) : %(w)s", { w: data.warnings.join(" · ") }));
    }
  }

  // ── Événements globaux ─────────────────────────────────────────────────────
  $("se-list").addEventListener("click", (ev) => {
    const card = ev.target.closest(".se-card");
    if (!card) return;
    const i = +card.dataset.i;
    const act = ev.target.closest("[data-act]")?.dataset.act;
    const cascade = ev.target.closest("[data-cascade]")?.dataset.cascade;
    if (cascade && !state.readonly) {
      pushUndo({op: "shift", start: i, delta: +cascade});
      for (let k = i; k < state.chunks.length; k++) {
        state.chunks[k].start_ms += +cascade;
        state.chunks[k].end_ms += +cascade;
      }
      renderList(); drawFresque(); drawZoomBand();
      $("se-list").querySelector(`.se-card[data-i="${i}"]`)?.classList.add("timing-open");
      return;
    }
    const shift = ev.target.closest("[data-tshift]")?.dataset.tshift;
    if (shift) {
      const [side, delta] = shift.split(":");
      editChunk(i, (c) => { c[side === "start" ? "start_ms" : "end_ms"] += +delta; });
      rerenderCard(i);
      card.classList.add("timing-open");
      drawFresque();
      return;
    }
    if (!act) {
      if (ev.shiftKey && !state.readonly) { toggleSelect(i); return; }
      setActive(i, false);
      state.visited.add(i);
      updateGauge();
      return;
    }
    if (act === "play" && state.audio.available) playSegment(i);
    else if (act === "seek" && state.audio.available) { audio.currentTime = state.chunks[i].start_ms / 1000; }
    else if (act === "speaker" && !state.readonly) openSpeakerMenu(i, ev.target);
    else if (act === "split" && !state.readonly) splitAtCursor(i, card);
    else if (act === "merge" && !state.readonly) mergeWithPrevious(i);
    else if (act === "delete" && !state.readonly) deleteChunk(i);
    else if (act === "timing") card.classList.toggle("timing-open");
  });

  // ── Sélection multiple (lot D — fusion/attribution/suppression en masse) ───
  function toggleSelect(i) {
    if (state.selection.has(i)) state.selection.delete(i); else state.selection.add(i);
    $("se-list").querySelector(`.se-card[data-i="${i}"]`)?.classList.toggle("selected", state.selection.has(i));
    refreshSelectionBar();
  }

  function clearSelection() {
    state.selection.clear();
    document.querySelectorAll(".se-card.selected").forEach((el) => el.classList.remove("selected"));
    refreshSelectionBar();
  }

  function refreshSelectionBar() {
    const bar = $("se-selection-bar");
    const n = state.selection.size;
    bar.classList.toggle("d-none", n < 2);
    if (n < 2) return;
    $("se-selection-count").textContent = `${n} segments`;
    const sorted = [...state.selection].sort((a, b) => a - b);
    const contiguous = sorted[sorted.length - 1] - sorted[0] === sorted.length - 1;
    $("se-sel-merge").disabled = !contiguous;
    $("se-sel-merge").title = contiguous ? _t("Fusionner la sélection") : _t("Fusion possible sur une plage contiguë seulement");
  }

  function applyRange(start, before, after) {
    pushUndo({op: "range", start, before: structuredClone(before), after: structuredClone(after)});
    state.chunks.splice(start, before.length, ...after);
    clearSelection();
    renderList();
    drawFresque(); drawZoomBand();
  }

  $("se-sel-merge").onclick = () => {
    const sorted = [...state.selection].sort((a, b) => a - b);
    const before = sorted.map((i) => state.chunks[i]);
    const merged = {...before[0],
      end_ms: Math.max(...before.map((c) => c.end_ms)),
      text: before.map((c) => c.text.trim()).filter(Boolean).join(" ")};
    applyRange(sorted[0], before, [merged]);
  };
  $("se-sel-delete").onclick = () => {
    const sorted = [...state.selection].sort((a, b) => a - b);
    if (!confirm(_t("Supprimer %(n)s segments ?", { n: sorted.length }))) return;
    // plages potentiellement non contiguës : traiter de la fin vers le début
    for (let k = sorted.length - 1; k >= 0; k--) {
      pushUndo({op: "delete", i: sorted[k], before: structuredClone(state.chunks[sorted[k]])});
      state.chunks.splice(sorted[k], 1);
    }
    clearSelection();
    renderList(); drawFresque(); drawZoomBand();
  };
  $("se-sel-speaker").onclick = (ev) => openSpeakerMenuForSelection(ev.target);
  $("se-sel-clear").onclick = clearSelection;

  function openSpeakerMenuForSelection(anchor) {
    // réutilise le popover : l'attribution s'applique à TOUTE la sélection
    openSpeakerMenu(-1, anchor);
  }

  // ── Solo locuteur (réparation de diarisation) ──────────────────────────────
  function setSolo(speakerId, name) {
    state.solo = speakerId;
    $("se-solo-chip").classList.toggle("d-none", !speakerId);
    if (speakerId) $("se-solo-name").textContent = name || speakerId;
  }
  $("se-solo-chip").onclick = () => setSolo(null);

  // ── Recherche (Ctrl+F) ──────────────────────────────────────────────────────
  function runSearch() {
    const q = $("se-search").value.trim().toLowerCase();
    document.querySelectorAll(".se-card.hit").forEach((el) => el.classList.remove("hit"));
    state.searchHits = [];
    state.searchPos = -1;
    if (q.length < 2) return;
    state.chunks.forEach((c, i) => { if (c.text.toLowerCase().includes(q)) state.searchHits.push(i); });
    gotoHit(0);
  }
  function gotoHit(pos) {
    if (!state.searchHits.length) return;
    state.searchPos = (pos + state.searchHits.length) % state.searchHits.length;
    const i = state.searchHits[state.searchPos];
    document.querySelectorAll(".se-card.hit").forEach((el) => el.classList.remove("hit"));
    const el = $("se-list").querySelector(`.se-card[data-i="${i}"]`);
    if (el) { el.classList.add("hit"); el.scrollIntoView({block: "center"}); }
    setActive(i, false);
  }
  $("se-search").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") { ev.shiftKey ? gotoHit(state.searchPos - 1) : (state.searchPos < 0 ? runSearch() : gotoHit(state.searchPos + 1)); }
    if (ev.key === "Escape") { $("se-search").value = ""; runSearch(); ev.target.blur(); }
  });
  $("se-search").addEventListener("input", () => { state.searchPos = -1; });
  $("se-search-next").onclick = () => (state.searchPos < 0 ? runSearch() : gotoHit(state.searchPos + 1));
  $("se-search-prev").onclick = () => gotoHit(state.searchPos - 1);

  // ── Points à vérifier (rapport qualité → liste de travail cliquable) ────────
  function renderReviewMenu() {
    const total = state.review.points.length + state.review.anchors.length;
    const btn = $("se-review-btn");
    btn.classList.toggle("d-none", total === 0);
    if (!total) return;
    btn.innerHTML = `<i class="bi bi-clipboard-check"></i> ` + _t("À vérifier %(done)s/%(total)s", { done: state.review.done.size, total: total });
    let html = "";
    state.review.anchors.forEach((a, k) => {
      html += `<div class="se-review-item anchored${state.review.done.has("a" + k) ? " done" : ""}" data-anchor="${k}">
        <input type="checkbox" data-done="a${k}" ${state.review.done.has("a" + k) ? "checked" : ""}
               title="${_t('Marquer comme traité')}">
        <span>${a.kind === "time" ? "🎧" : "🔎"} ${esc(a.text)}</span></div>`;
    });
    state.review.points.forEach((t, k) => {
      html += `<div class="se-review-item${state.review.done.has("p" + k) ? " done" : ""}">
        <input type="checkbox" data-done="p${k}" ${state.review.done.has("p" + k) ? "checked" : ""}
               title="${_t('Marquer comme traité')}">
        <span>${esc(t)}</span></div>`;
    });
    $("se-review-menu").innerHTML = html;
  }

  $("se-review-menu").addEventListener("click", (ev) => {
    const box = ev.target.closest("[data-done]");
    if (box) {
      const key = box.dataset.done;
      if (state.review.done.has(key)) state.review.done.delete(key); else state.review.done.add(key);
      renderReviewMenu();
      scheduleDraft();
      ev.stopPropagation();
      return;
    }
    const item = ev.target.closest("[data-anchor]");
    if (!item) return;
    const a = state.review.anchors[+item.dataset.anchor];
    if (a.kind === "time" && state.audio.available) {
      audio.currentTime = a.start_ms / 1000;
      state.segmentStopAt = null;
      const i = chunkAt(a.start_ms + 1);
      if (i >= 0) {
        setActive(i, false);
        $("se-list").querySelector(`.se-card[data-i="${i}"]`)?.scrollIntoView({block: "center"});
      }
    } else if (a.kind === "search") {
      $("se-search").value = a.query;
      runSearch();
    }
  });

  // ── Jauge de relecture (persistée au brouillon) ────────────────────────────
  function updateGauge() {
    if (!state.chunks.length) return;
    const pct = Math.round((state.visited.size / state.chunks.length) * 100);
    $("se-gauge").textContent = pct ? _t("relu : %(pct)s %%", { pct: pct }) : "";
  }

  // Édition de texte : pause auto à la frappe (D3), commit au blur/Entrée
  $("se-list").addEventListener("input", (ev) => {
    if (!ev.target.classList.contains("se-text")) return;
    if (!audio.paused) { state.wasPlayingBeforeType = true; audio.pause(); }
    setSaveState(_t("modifications non enregistrées"), "saving");
  });
  $("se-list").addEventListener("focusout", (ev) => {
    if (!ev.target.classList.contains("se-text")) return;
    commitText(+ev.target.closest(".se-card").dataset.i, ev.target);
  }, true);
  $("se-list").addEventListener("paste", (ev) => {
    if (!ev.target.classList.contains("se-text")) return;
    ev.preventDefault();  // collage TEXTE BRUT forcé (P1)
    document.execCommand("insertText", false, ev.clipboardData.getData("text/plain"));
  });
  // Champs timing : commit à la validation
  $("se-list").addEventListener("change", (ev) => {
    const card = ev.target.closest(".se-card");
    if (!card) return;
    const i = +card.dataset.i;
    if (ev.target.classList.contains("se-t-start") || ev.target.classList.contains("se-t-end")) {
      const ms = parseTs(ev.target.value);
      if (ms === null) { ev.target.value = fmtMs(state.chunks[i][ev.target.classList.contains("se-t-start") ? "start_ms" : "end_ms"]); return; }
      editChunk(i, (c) => { c[ev.target.classList.contains("se-t-start") ? "start_ms" : "end_ms"] = ms; });
      rerenderCard(i);
      $("se-list").querySelector(`.se-card[data-i="${i}"]`)?.classList.add("timing-open");
      drawFresque();
    }
  });

  document.addEventListener("click", (ev) => {
    if (!ev.target.closest("#se-speaker-menu") && !ev.target.closest("[data-act='speaker']")
        && !ev.target.closest("#se-sel-speaker")) closeSpeakerMenu();
  });

  document.addEventListener("keydown", (ev) => {
    const typing = ev.target.isContentEditable || /INPUT|TEXTAREA|SELECT/.test(ev.target.tagName);
    if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "f") { ev.preventDefault(); $("se-search").focus(); return; }
    if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "s") { ev.preventDefault(); saveVersion(); return; }
    if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "z") { if (!typing) { ev.preventDefault(); undo(); } return; }
    if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "y") { if (!typing) { ev.preventDefault(); redo(); } return; }
    if (typing) {
      if (ev.key === "Enter" && !ev.shiftKey && ev.target.classList.contains("se-text")) {
        ev.preventDefault();
        ev.target.blur();  // commit
        if (state.wasPlayingBeforeType && state.audio.available) { audio.play(); }
        state.wasPlayingBeforeType = false;
      }
      if (ev.key === "Escape") ev.target.blur();
      return;
    }
    switch (ev.key) {
      case " ": ev.preventDefault(); togglePlay(); break;
      case "s": case "S": if (state.activeIndex >= 0 && state.audio.available && !state.readonly) {
        const i = state.activeIndex;
        editChunk(i, (c) => { c.start_ms = Math.round(audio.currentTime * 1000); });
        rerenderCard(i); drawFresque();
      } break;
      case "e": case "E": if (state.activeIndex >= 0 && state.audio.available && !state.readonly) {
        const i = state.activeIndex;
        editChunk(i, (c) => { c.end_ms = Math.round(audio.currentTime * 1000); });
        rerenderCard(i); drawFresque();
      } break;
      case "Tab": ev.preventDefault();
        setActive(Math.min(state.chunks.length - 1, Math.max(0, state.activeIndex + (ev.shiftKey ? -1 : 1))), false);
        $("se-list").querySelector(`.se-card[data-i="${state.activeIndex}"]`)?.scrollIntoView({block: "center"});
        break;
      case "m": case "M": addMarker(); break;
      case "g": case "G": {
        const v = prompt("Aller au temps (HH:MM:SS ou MM:SS) :");
        const ms = v && parseTs(v);
        if (ms !== null && ms !== undefined && state.audio.available) {
          audio.currentTime = ms / 1000;
          state.segmentStopAt = null;
        }
        break;
      }
      case "Escape": clearSelection(); break;
      case "?": showHelp(); break;
    }
  });

  // Bascule fresque ↔ lanes (un seul contrôle — arbitrage utilisateur)
  $("se-view-toggle").addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-view]");
    if (!btn) return;
    state.view = btn.dataset.view;
    $("se-view-toggle").querySelectorAll("button").forEach((b) => b.classList.toggle("active", b === btn));
    $("se-fresque").classList.toggle("d-none", state.view !== "fresque");
    $("se-lanes").classList.toggle("d-none", state.view !== "lanes");
    drawFresque();
  });

  $("se-lanes").addEventListener("click", (ev) => {
    const rect = ev.currentTarget.getBoundingClientRect();
    const labelW = 150;
    if (ev.clientX - rect.left < labelW) {
      const speakers = laneSpeakers();
      const id = speakers[Math.floor((ev.clientY - rect.top) / 20)];
      if (!id) return;
      const names = new Map();
      for (const c of state.chunks) if (c.speaker_id && !names.has(c.speaker_id)) names.set(c.speaker_id, c.speaker_name);
      if (state.solo === id) { setSolo(null); return; }
      setSolo(id, names.get(id));
      // démarrer sur sa première prise de parole
      const first = state.chunks.findIndex((c) => (c.speaker_id || "—") === id);
      if (first >= 0 && state.audio.available) {
        audio.currentTime = state.chunks[first].start_ms / 1000;
        state.segmentStopAt = null;
        audio.play();
      }
      return;
    }
    const speakers = laneSpeakers();
    const id = speakers[Math.floor((ev.clientY - rect.top) / 20)];
    const ms = ((ev.clientX - rect.left - labelW) / (rect.width - labelW)) * totalMs();
    // la prise de parole de CE locuteur la plus proche de l'instant cliqué
    let best = -1, bestDist = Infinity;
    for (let i = 0; i < state.chunks.length; i++) {
      const c = state.chunks[i];
      if ((c.speaker_id || "—") !== id) continue;
      const dist = ms < c.start_ms ? c.start_ms - ms : ms > c.end_ms ? ms - c.end_ms : 0;
      if (dist < bestDist) { bestDist = dist; best = i; }
    }
    if (best >= 0) {
      setActive(best, false);
      $("se-list").querySelector(`.se-card[data-i="${best}"]`)?.scrollIntoView({block: "center"});
      if (state.audio.available) { audio.currentTime = state.chunks[best].start_ms / 1000; }
      state.zoom.centerMs = state.chunks[best].start_ms;
      drawZoomBand();
    }
  });

  // Zoom : boutons ÉVIDENTS + molette sur la bande (exigence utilisateur)
  function zoomBy(factor) {
    state.zoom.spanMs = Math.max(3000, Math.min(totalMs(), state.zoom.spanMs * factor));
    drawFresque(); drawZoomBand();
  }
  $("se-zoom-in").onclick = () => zoomBy(0.5);
  $("se-zoom-out").onclick = () => zoomBy(2);
  $("se-zoom-fit").onclick = () => { state.zoom.spanMs = totalMs(); state.zoom.centerMs = totalMs() / 2; drawFresque(); drawZoomBand(); };
  $("se-zoomband").addEventListener("wheel", (ev) => {
    ev.preventDefault();
    zoomBy(ev.deltaY > 0 ? 1.25 : 0.8);
  }, {passive: false});

  // Bande zoomée : clic = seek ; glisser une poignée du segment actif = retimer
  const zoomband = $("se-zoomband");
  zoomband.addEventListener("mousedown", (ev) => {
    const rect = zoomband.getBoundingClientRect();
    const zoomWin = zoomWindow();
    const x = ev.clientX - rect.left;
    if (state.activeIndex >= 0 && !state.readonly) {
      const c = state.chunks[state.activeIndex];
      for (const side of ["start", "end"]) {
        const hx = msToX(c[side + "_ms"], zoomWin, rect.width);
        if (Math.abs(x - hx) < 6) {
          state.dragging = {i: state.activeIndex, side, before: structuredClone(c)};
          zoomband.classList.add("dragging");
          return;
        }
      }
    }
    // clic simple = seek
    if (state.audio.available) {
      audio.currentTime = (zoomWin.from + (x / rect.width) * zoomWin.span) / 1000;
      state.segmentStopAt = null;
    }
  });
  window.addEventListener("mousemove", (ev) => {
    if (!state.dragging) return;
    const rect = zoomband.getBoundingClientRect();
    const zoomWin = zoomWindow();
    const ms = Math.round(zoomWin.from + ((ev.clientX - rect.left) / rect.width) * zoomWin.span);
    state.chunks[state.dragging.i][state.dragging.side + "_ms"] = Math.max(0, ms);
    drawZoomBand();
  });
  window.addEventListener("mouseup", () => {
    if (!state.dragging) return;
    const {i, before} = state.dragging;
    state.dragging = null;
    zoomband.classList.remove("dragging");
    pushUndo({op: "edit", i, before, after: structuredClone(state.chunks[i])});
    rerenderCard(i);
    drawFresque();
  });

  // Repères : bouton + chips (aller / retirer)
  $("se-marker-btn").onclick = addMarker;
  $("se-marker-chips").addEventListener("click", (ev) => {
    const x = ev.target.closest("[data-x]");
    if (x) {
      state.markers.splice(+x.dataset.x, 1);
      renderMarkerChips(); drawFresque(); drawZoomBand(); scheduleDraft();
      return;
    }
    const chip = ev.target.closest("[data-k]");
    if (chip && state.audio.available) {
      audio.currentTime = state.markers[+chip.dataset.k].at_ms / 1000;
      state.segmentStopAt = null;
    }
  });

  $("se-play").onclick = togglePlay;
  $("se-back10").onclick = () => { audio.currentTime = Math.max(0, audio.currentTime - 10); };
  $("se-fwd10").onclick = () => { audio.currentTime += 10; };
  $("se-rate").onchange = (ev) => { audio.playbackRate = +ev.target.value; };
  $("se-save").onclick = saveVersion;
  $("se-undo").onclick = undo;
  $("se-redo").onclick = redo;
  $("se-help").onclick = showHelp;
  window.addEventListener("resize", drawFresque);
  window.addEventListener("beforeunload", (ev) => {
    if (state.dirty.size && !state.draftBlocked) { pushDraft(); }
  });

  function showHelp() {
    alert(
      _t("Raccourcis :") + "\n" +
      _t("Espace — lecture/pause") + "\n" + _t("Entrée — valider le texte et reprendre l'écoute") + "\n" +
      _t("Tab / Maj+Tab — segment suivant / précédent") + "\n" + _t("S / E — caler le début / la fin sur la lecture") + "\n" +
      _t("Ctrl+Z / Ctrl+Y — annuler / rétablir") + "\n" + _t("Ctrl+S — enregistrer une version") + "\n" +
      _t("M — poser un repère · G — aller au temps") + "\n" +
      _t("✂ — couper au curseur · ⧉ — fusionner avec le précédent") + "\n" +
      _t("Bande du bas : molette = zoom · glisser les bords bleus = retimer le segment actif")
    );
  }

  load();
})();
