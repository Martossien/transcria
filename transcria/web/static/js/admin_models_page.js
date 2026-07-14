/*
 * admin_models_page.js — page Administration → Modèles : poll de la progression
 * des téléchargements (barres [data-progress-role], ~2 s).
 *
 * Extrait du bloc inline d'admin_models.html (vague A3). Chaînes via t().
 */
(function () {
  var GO = t('Go');
  function human(n) { return (n / 1e9).toFixed(1) + " " + GO; }
  var bars = document.querySelectorAll("[data-progress-role]");
  bars.forEach(function (el) {
    var role = el.getAttribute("data-progress-role");
    var label = document.querySelector('[data-progress-label="' + role + '"]');
    var timer = setInterval(function () {
      fetch("/admin/models/progress/" + encodeURIComponent(role))
        .then(function (r) { return r.json(); })
        .then(function (p) {
          var bar = el.querySelector(".progress-bar");
          if (p.status === "done") { clearInterval(timer); location.reload(); return; }
          if (p.status === "error" || p.status === "absent") { clearInterval(timer); location.reload(); return; }
          if (p.pct !== null && p.pct !== undefined) {
            bar.style.width = p.pct + "%";
            bar.textContent = p.pct + "%";
            if (label) label.textContent = human(p.downloaded_bytes || 0) + " / " + human(p.total_bytes || 0);
          } else if (label) {
            label.textContent = t('téléchargement…') + " " + human(p.downloaded_bytes || 0);
          }
        })
        .catch(function () {});
    }, 2000);
  });
})();
