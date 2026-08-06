// Checklist de premier démarrage (accueil admin) — remplit #first-run-checklist
// avec le fragment servi par /admin/first-run-status. Asynchrone à dessein :
// l'accueil ne paie jamais les sondes (GPU, fichiers modèles, binaire opencode),
// et toute erreur laisse simplement la page telle quelle (204 = tout est vert).
(function () {
  "use strict";
  const box = document.getElementById("first-run-checklist");
  if (!box) return;
  fetch(box.dataset.url, { headers: { Accept: "text/html" } })
    .then((resp) => (resp.status === 200 ? resp.text() : ""))
    .then((html) => {
      if (html) box.innerHTML = html;
    })
    .catch(() => {});
})();
