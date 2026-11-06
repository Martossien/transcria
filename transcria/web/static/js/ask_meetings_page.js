/*
 * ask_meetings_page.js — page /meetings/ask : « Discuter avec mes réunions ».
 *
 * Le navigateur ÉCRIT une demande (POST /api/meetings/ask → 202) puis INTERROGE son état
 * (GET /api/meetings/ask/<id>) jusqu'à done|failed : le worker fait tout le reste, jamais
 * d'appel LLM depuis le web. La conversation (identifiant) vit dans sessionStorage ; au
 * rechargement, l'historique est relu depuis le serveur (GET /api/meetings/ask/history).
 * Chaînes traduites via t() (msgid déclarés dans transcria/i18n/js_catalog.py).
 */
(function () {
  "use strict";
  var island = document.getElementById("ask-settings");
  var settings = island ? JSON.parse(island.textContent || "{}") : {};
  var form = document.getElementById("ask-form");
  if (!form) return;

  var T = {
    waiting: t("En attente du worker…"),
    you: t("Vous"),
    assistant: t("Assistant"),
    failed: t("La demande a échoué."),
    network: t("Impossible de joindre le serveur."),
    tooLong: t("La demande n'a pas été traitée dans le délai prévu (le worker est-il démarré ?)."),
    transcript: t("Chercher dans la transcription de cette réunion"),
    importDate: t("date d'import"),
    citedLabel: t("Réunions citées :"),
    none: t("aucune"),
  };

  var thread = document.getElementById("ask-thread");
  var question = document.getElementById("ask-question");
  var confidential = document.getElementById("ask-confidential");
  var submit = document.getElementById("ask-submit");
  var errorBox = document.getElementById("ask-error");
  var targetBox = document.getElementById("ask-target");
  var targetTitle = document.getElementById("ask-target-title");
  var target = null; // {job_id, title}

  var STORAGE_KEY = "transcria.ask.conversation";
  var conversationId = null;
  try { conversationId = window.sessionStorage.getItem(STORAGE_KEY) || null; } catch (e) { conversationId = null; }

  function rememberConversation(id) {
    conversationId = id;
    try { if (id) window.sessionStorage.setItem(STORAGE_KEY, id); else window.sessionStorage.removeItem(STORAGE_KEY); } catch (e) { /* stockage indisponible : la conversation vit le temps de la page */ }
  }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function showError(message) {
    errorBox.textContent = message;
    errorBox.classList.toggle("d-none", !message);
  }

  function setTarget(meeting) {
    target = meeting;
    targetBox.classList.toggle("d-none", !meeting);
    targetBox.classList.toggle("d-flex", !!meeting);
    targetTitle.textContent = meeting ? meeting.title : "";
    // Une réunion confidentielle citée (case cochée à l'époque) ne peut être relue qu'avec la case :
    // on la coche, sinon le worker refuserait la cible comme invisible.
    if (meeting && meeting.confidential) confidential.checked = true;
    if (meeting) question.focus();
  }

  function renderCited(container, cited) {
    var wrap = el("div", "d-flex flex-wrap gap-2 mt-2");
    container.appendChild(el("div", "small text-muted", T.citedLabel + " " + (cited.length ? "" : T.none)));
    cited.forEach(function (m) {
      var card = el("div", "border rounded p-2 small bg-body-tertiary");
      card.setAttribute("data-ask-cited", m.job_id);
      var link = el("a", "fw-semibold text-decoration-none", m.title);
      link.href = m.url;
      card.appendChild(link);
      var meta = [];
      if (m.date) meta.push(m.date + (m.date_is_import_fallback ? " (" + T.importDate + ")" : ""));
      if (m.meeting_type) meta.push(m.meeting_type);
      if (meta.length) card.appendChild(el("div", "text-muted", meta.join(" · ")));
      var btn = el("button", "btn btn-link btn-sm p-0 mt-1", T.transcript);
      btn.type = "button";
      btn.addEventListener("click", function () { setTarget({ job_id: m.job_id, title: m.title, confidential: !!m.confidential }); });
      card.appendChild(btn);
      wrap.appendChild(card);
    });
    container.appendChild(wrap);
  }

  function renderTurn(role, text, extra) {
    var box = el("div", "card mb-2 " + (role === "user" ? "border-primary-subtle" : ""));
    box.setAttribute("data-ask-turn", role);
    var body = el("div", "card-body py-2");
    var head = el("div", "small text-muted mb-1", role === "user" ? T.you : T.assistant + (extra && extra.level_label ? " — " + extra.level_label : ""));
    body.appendChild(head);
    var para = el("div", role === "user" ? "" : "ask-answer");
    para.style.whiteSpace = "pre-wrap";
    para.textContent = text;
    body.appendChild(para);
    if (role === "assistant" && extra && extra.cited) renderCited(body, extra.cited);
    box.appendChild(body);
    thread.appendChild(box);
    box.scrollIntoView({ block: "nearest" });
    return box;
  }

  function renderPending() {
    var box = el("div", "card mb-2 border-dashed");
    box.setAttribute("data-ask-pending", "1");
    var body = el("div", "card-body py-2 small text-muted");
    body.appendChild(el("span", "spinner-border spinner-border-sm me-2"));
    body.appendChild(document.createTextNode(T.waiting));
    box.appendChild(body);
    thread.appendChild(box);
    return box;
  }

  function renderFailure(pending, message) {
    pending.setAttribute("data-ask-turn", "failed");
    pending.removeAttribute("data-ask-pending");
    pending.className = "card mb-2 border-danger-subtle";
    var body = pending.querySelector(".card-body");
    body.className = "card-body py-2 text-danger";
    body.textContent = message || T.failed;
  }

  function poll(requestId, pending, startedAt) {
    startedAt = startedAt || Date.now();
    if (Date.now() - startedAt > (settings.poll_max_ms || 960000)) { renderFailure(pending, T.tooLong); submit.disabled = false; return; }
    fetch("/api/meetings/ask/" + encodeURIComponent(requestId), { headers: { Accept: "application/json" } })
      .then(function (resp) { return resp.json().then(function (d) { return { ok: resp.ok, d: d }; }); })
      .then(function (r) {
        if (!r.ok) { renderFailure(pending, r.d && r.d.error); submit.disabled = false; return; }
        if (r.d.status === "done") {
          pending.remove();
          renderTurn("assistant", r.d.answer, r.d);
          submit.disabled = false;
          return;
        }
        if (r.d.status === "failed") { renderFailure(pending, r.d.error); submit.disabled = false; return; }
        window.setTimeout(function () { poll(requestId, pending, startedAt); }, settings.poll_interval_ms || 2000);
      })
      .catch(function () { renderFailure(pending, T.network); submit.disabled = false; });
  }

  function loadHistory() {
    if (!conversationId) return;
    fetch("/api/meetings/ask/history?conversation_id=" + encodeURIComponent(conversationId), { headers: { Accept: "application/json" } })
      .then(function (resp) { return resp.ok ? resp.json() : { turns: [] }; })
      .then(function (d) {
        (d.turns || []).forEach(function (turn) { renderTurn(turn.role, turn.text, turn); });
      })
      .catch(function () { /* historique indisponible : la page reste utilisable */ });
  }

  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    var text = (question.value || "").trim();
    if (!text) return;
    showError("");
    submit.disabled = true;
    var payload = {
      question: text,
      conversation_id: conversationId,
      include_confidential: !!confidential.checked,
      level: target ? 3 : 1,
      target_job_id: target ? target.job_id : null,
    };
    renderTurn("user", text);
    var pending = renderPending();
    fetch("/api/meetings/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(payload),
    })
      .then(function (resp) { return resp.json().then(function (d) { return { ok: resp.ok, d: d }; }); })
      .then(function (r) {
        if (!r.ok) { renderFailure(pending, r.d && r.d.error); showError(r.d && r.d.error); submit.disabled = false; return; }
        rememberConversation(r.d.conversation_id);
        question.value = "";
        setTarget(null);
        poll(r.d.request_id, pending);
      })
      .catch(function () { renderFailure(pending, T.network); submit.disabled = false; });
  });

  document.getElementById("ask-new").addEventListener("click", function () {
    rememberConversation(null);
    thread.innerHTML = "";
    setTarget(null);
    showError("");
    question.focus();
  });
  document.getElementById("ask-target-cancel").addEventListener("click", function () { setTarget(null); });

  loadHistory();
})();
