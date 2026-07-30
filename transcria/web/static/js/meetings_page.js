// Planification d'une réunion depuis la page d'accueil (vague 3, plan UI_REUNIONS §5.1).
// Deux actions déléguées CSP-safe (cf. ui_actions.js) : scheduleMeeting / cancelMeeting.
// Le serveur reste le juge (disponibilité, permission, doublon) — ici, uniquement le POST
// et l'affichage de SON message d'erreur, jamais une règle métier dupliquée.
var TranscrIA = window.TranscrIA || {};

TranscrIA.scheduleMeeting = function () {
  var error = document.getElementById("meeting-error");
  error.classList.add("d-none");
  var payload = {
    provider: document.getElementById("meeting-provider").value,
    meeting_ref: document.getElementById("meeting-ref").value.trim(),
    title: document.getElementById("meeting-title").value.trim(),
    scheduled_at: document.getElementById("meeting-when").value || "",
    // Salle protégée : envoyé une seule fois, chiffré au repos, jamais réaffiché.
    passcode: document.getElementById("meeting-passcode").value,
  };
  fetch("/api/meetings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })
    .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
    .then(function (res) {
      if (!res.ok) {
        error.textContent = res.body.error || "Erreur";
        error.classList.remove("d-none");
        return;
      }
      window.location.href = "/jobs/" + res.body.job_id;   // le wizard prend le relais
    })
    .catch(function () {
      error.textContent = "Serveur injoignable";
      error.classList.remove("d-none");
    });
};

TranscrIA.meetingNow = function () {
  document.getElementById("meeting-when").value = "";   // vide = dès que possible (serveur)
  TranscrIA.scheduleMeeting();
};

TranscrIA.cancelMeeting = function (sessionId) {
  if (!window.confirm("Annuler cette captation de réunion ?")) return;
  fetch("/api/meetings/" + sessionId + "/cancel", { method: "POST" })
    .then(function () { window.location.reload(); });
};

TranscrIA.rescheduleMeeting = function (sessionId) {
  fetch("/api/meetings/" + sessionId + "/reschedule", { method: "POST" })
    .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
    .then(function (res) {
      if (!res.ok) { window.alert(res.body.error || "Erreur"); return; }
      window.location.reload();
    });
};

// La page d'un job de réunion se tient À JOUR toute seule : au moindre changement d'état
// (captation OU job — l'audio vient d'arriver), rechargement. États terminaux : on arrête.
(function () {
  var banner = document.getElementById("meeting-session-banner");
  if (!banner) return;
  var terminal = ["done", "not_admitted", "failed_final", "cancelled"];
  var sessionState = banner.getAttribute("data-state");
  var jobId = banner.getAttribute("data-job");
  if (terminal.indexOf(sessionState) !== -1) return;
  var timer = setInterval(function () {
    fetch("/api/jobs/" + jobId + "/status")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var fresh = data.meeting_session ? data.meeting_session.state : null;
        if ((fresh && fresh !== sessionState) || (data.state && data.state !== "created")) {
          clearInterval(timer);
          window.location.reload();
        }
      })
      .catch(function () {});
  }, 5000);
})();

// Suivi en direct — PROVISOIRE (vague 5, D5.5) : poll en delta (?after=<n>) toutes les 5 s
// tant que le panneau existe ; le rechargement d'état (poll ci-dessus) l'efface à
// l'ingestion. Rendu par textContent uniquement — jamais d'HTML issu du flux.
(function () {
  var panel = document.getElementById("live-captions-panel");
  if (!panel) return;
  var list = document.getElementById("live-captions-list");
  var sessionId = panel.getAttribute("data-session");
  var truncatedLabel = panel.getAttribute("data-truncated-label") || "";
  var after = 0;
  var announced = 0;

  function render(caption) {
    var empty = document.getElementById("live-captions-empty");
    if (empty) empty.remove();
    var row = document.createElement("div");
    if (caption.speaker) {
      var who = document.createElement("strong");
      who.textContent = caption.speaker + " : ";
      row.appendChild(who);
    }
    row.appendChild(document.createTextNode(caption.text));
    list.appendChild(row);
  }

  function tick() {
    fetch("/api/meetings/" + sessionId + "/captions?after=" + after)
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) return;
        (data.captions || []).forEach(render);
        if (data.captions && data.captions.length) {
          after = data.next;
          list.scrollTop = list.scrollHeight;
        }
        if (data.truncated > announced) {
          announced = data.truncated;
          var note = document.createElement("div");
          note.className = "text-muted fst-italic";
          note.textContent = truncatedLabel.replace("%(n)s", String(data.truncated));
          list.appendChild(note);
        }
      })
      .catch(function () {});
  }

  tick();
  setInterval(tick, 5000);
})();

window.TranscrIA = TranscrIA;
