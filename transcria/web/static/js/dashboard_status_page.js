/*
 * dashboard_status_page.js — page /system : panneau « Ressources distantes »
 * (poll de /api/resources/status toutes les ~10 s, §7.1).
 *
 * Extrait du bloc inline de dashboard_status.html (vague A3). Les chaînes
 * traduites passent par t() (window.I18N) — placeholders {x} conservés tels
 * quels (le fmt local les résout, msgid inchangés).
 */
(function () {
  const T = {
    allLocal: t('tout local'),
    noRemote: t('Aucune ressource distante configurée (tout intégré).'),
    unreachable: t('injoignable'),
    nodeUnreachable: t('Nœud de ressources injoignable — les transcriptions distantes sont mises en file. Requis : {reqs}.'),
    remote: t('distant'),
    gpuLabel: t('GPU :'),
    gpuLine: t('GPU {i} — libre {free} / {total} Mo'),
    noEngine: t('Aucun moteur déclaré.'),
    error: t('erreur'),
    statusUnavailable: t('Statut indisponible ({e}).'),
  };
  function fmt(s, p) { return s.replace(/\{([^}]+)\}/g, function (m, k) { return p[k] !== undefined ? p[k] : m; }); }
  const modeEl = document.getElementById('rr-mode');
  const body = document.getElementById('rr-body');
  function dot(up) {
    return '<span class="badge bg-' + (up ? 'success' : 'danger') + '">' + (up ? 'UP' : 'DOWN') + '</span>';
  }
  async function refresh() {
    try {
      const resp = await fetch('/api/resources/status', { headers: { 'Accept': 'application/json' } });
      if (!resp.ok) throw new Error('http ' + resp.status);
      const d = await resp.json();
      const reqs = d.requires_remote || [];
      if (reqs.length === 0) {
        modeEl.textContent = T.allLocal;
        modeEl.className = 'badge bg-secondary';
        body.innerHTML = '<p class="text-muted">' + T.noRemote + '</p>';
        return;
      }
      if (!d.reachable) {
        modeEl.textContent = T.unreachable;
        modeEl.className = 'badge bg-danger';
        body.innerHTML = '<div class="alert alert-danger mb-0">' + fmt(T.nodeUnreachable, { reqs: reqs.join(', ') }) + '</div>';
        return;
      }
      modeEl.textContent = d.mode || T.remote;
      modeEl.className = 'badge bg-success';
      let html = '';
      (d.engines || []).forEach(function (e) {
        html += '<div class="d-flex justify-content-between mb-1"><span>' + e.name
          + ' <small class="text-muted">(' + e.kind + ')</small></span>' + dot(e.up) + '</div>';
      });
      if ((d.gpus || []).length) {
        html += '<hr><small class="text-muted">' + T.gpuLabel + '</small>';
        d.gpus.forEach(function (g) {
          html += '<div><small>' + fmt(T.gpuLine, { i: g.index, free: g.free_mb, total: g.total_mb }) + '</small></div>';
        });
      }
      body.innerHTML = html || '<p class="text-muted">' + T.noEngine + '</p>';
    } catch (err) {
      modeEl.textContent = T.error;
      modeEl.className = 'badge bg-warning';
      body.innerHTML = '<p class="text-muted">' + fmt(T.statusUnavailable, { e: err }) + '</p>';
    }
  }
  refresh();
  setInterval(refresh, 10000);  // §7.1 : ~10 s
})();
