/*
 * queue_page.js — page File d'attente : actions par job (pause/reprise/annulation),
 * changement de priorité, purge des jobs de test E2E.
 *
 * Extrait du bloc inline de queue.html (vague A3). Le préfixe des jobs de test
 * (donnée Jinja) arrive par window.QUEUE_E2E_TEST_JOB_PREFIX (init 1 ligne) ;
 * les chaînes traduites passent par t() (window.I18N).
 */
const queueFeedback = document.getElementById('queue-feedback');

function showQueueFeedback(message, level = 'danger') {
  queueFeedback.className = `alert alert-${level}`;
  queueFeedback.textContent = message;
}

async function postQueueAction(jobId, action, payload = null) {
  const options = {method: 'POST'};
  if (payload) {
    options.headers = {'Content-Type': 'application/json'};
    options.body = JSON.stringify(payload);
  }
  const response = await fetch(`/api/queue/${jobId}/${action}`, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || t('Action impossible sur ce job.'));
  }
  return data;
}

document.querySelectorAll('[data-action]').forEach((button) => {
  button.addEventListener('click', async () => {
    if (button.dataset.action === 'cancel' && !window.confirm(t('Annuler ce job ?'))) {
      return;
    }
    button.disabled = true;
    try {
      await postQueueAction(button.dataset.job, button.dataset.action);
      window.location.reload();
    } catch (error) {
      button.disabled = false;
      showQueueFeedback(error.message);
    }
  });
});

document.querySelectorAll('.queue-priority-form').forEach((form) => {
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = form.querySelector('button[type="submit"]');
    const priority = Number.parseInt(form.priority.value, 10);
    if (!Number.isInteger(priority) || priority < 1 || priority > 100) {
      showQueueFeedback(t('La priorité doit être comprise entre 1 et 100.'));
      return;
    }
    button.disabled = true;
    try {
      await postQueueAction(form.dataset.job, 'priority', {priority});
      window.location.reload();
    } catch (error) {
      button.disabled = false;
      showQueueFeedback(error.message);
    }
  });
});

document.querySelector('[data-reload]')?.addEventListener('click', () => window.location.reload());

document.querySelector('[data-purge-e2e]')?.addEventListener('click', async (event) => {
  const prefix = window.QUEUE_E2E_TEST_JOB_PREFIX || '';
  const confirmation = t('Supprimer tous les jobs de test ?') + `\n"${prefix}…"\n\n` + t('Les jobs en cours seront ignorés.');
  if (!window.confirm(confirmation)) {
    return;
  }
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const response = await fetch('/api/queue/e2e-test-jobs/purge', {method: 'POST'});
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.ok === false) {
      throw new Error(data.error || t('Nettoyage impossible.'));
    }
    const skipped = data.skipped_count ? ` (${data.skipped_count})` : '';
    showQueueFeedback(`${data.deleted_count} ` + t('job(s) de test supprimé(s).') + skipped, 'success');
    window.setTimeout(() => window.location.reload(), 900);
  } catch (error) {
    button.disabled = false;
    showQueueFeedback(error.message);
  }
});
