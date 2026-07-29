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

TranscrIA.cancelMeeting = function (sessionId) {
  if (!window.confirm("Annuler cette captation de réunion ?")) return;
  fetch("/api/meetings/" + sessionId + "/cancel", { method: "POST" })
    .then(function () { window.location.reload(); });
};

window.TranscrIA = TranscrIA;
