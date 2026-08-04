/* Page Maintenance : suivi de la mise à niveau lancée depuis l'UI.
 *
 * Le serveur écrit un fichier d'état (oneshot systemd) ; la page le sonde via
 * /admin/maintenance/upgrade/status. Pendant le redémarrage du service, les
 * requêtes échouent : on l'affiche et on continue de sonder — le changement de
 * version courante est le signal « terminé » (puis rechargement de la page).
 * Chaînes traduites via t() (window.I18N, cf. transcria/i18n/js_catalog.py). */
(function () {
  'use strict';

  const box = document.getElementById('upgrade-progress');
  if (!box) return;
  const status = box.dataset.status;
  if (status !== 'requested' && status !== 'running') return;

  const initialVersion = box.dataset.currentVersion;
  const label = document.getElementById('upgrade-progress-label');
  const bar = document.getElementById('upgrade-progress-bar');

  function show(text) {
    if (label) label.textContent = text;
  }

  async function poll() {
    try {
      const resp = await fetch('/admin/maintenance/upgrade/status',
                               { headers: { Accept: 'application/json' } });
      if (!resp.ok) throw new Error(String(resp.status));
      const data = await resp.json();
      if (data.current_version && data.current_version !== initialVersion) {
        show(t('Mise à niveau terminée — rechargement…'));
        window.location.reload();
        return;
      }
      const state = data.state || {};
      if (state.status === 'failed') {
        show(t('Échec de la mise à niveau : %(e)s').replace('%(e)s', state.error || '?'));
        box.classList.add('text-danger');
        return; // état final : plus de sondage, le détail est dans la page rechargée
      }
      if (state.status === 'ok') {
        window.location.reload();
        return;
      }
      if (state.status === 'running' && state.label) {
        show('[' + state.step + '/' + state.steps_total + '] ' + state.label);
        if (bar && state.steps_total) {
          bar.style.width = Math.round((100 * state.step) / state.steps_total) + '%';
        }
      } else {
        show(t('Mise à niveau demandée…'));
      }
    } catch {
      show(t('Le service redémarre — reconnexion…'));
    }
    setTimeout(poll, 2000);
  }

  poll();
})();
