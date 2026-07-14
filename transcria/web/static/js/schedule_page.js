/*
 * schedule_page.js — page Planification GPU : agenda maître, création/bascule/
 * suppression des créneaux (limit_concurrency, pause_queue, force_gpu).
 *
 * Extrait du bloc inline de schedule.html (vague A3). Les descriptions d'action
 * (données Jinja) arrivent par window.SCHEDULE_ACTION_DESCRIPTIONS (init 1 ligne
 * dans le template) ; les chaînes traduites passent par t() (window.I18N).
 */
const masterToggle = document.getElementById('schedule-master-toggle');
masterToggle?.addEventListener('change', async () => {
  const resp = await fetch('/api/schedule/enabled', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({enabled: masterToggle.checked}),
  });
  if (resp.ok) { location.reload(); }
  else {
    const data = await resp.json().catch(() => ({}));
    alert(data.error || t("Impossible de modifier l'agenda."));
    masterToggle.checked = !masterToggle.checked;
  }
});
const scheduleFeedback = document.getElementById('schedule-feedback');
const actionSelect = document.getElementById('window-action');
const workersInput = document.getElementById('window-workers');
const actionHelp = document.getElementById('window-action-help');
const actionDescriptions = window.SCHEDULE_ACTION_DESCRIPTIONS || {};

function showScheduleFeedback(message, level = 'danger') {
  scheduleFeedback.className = `alert alert-${level}`;
  scheduleFeedback.textContent = message;
}

function syncActionFields() {
  workersInput.disabled = actionSelect.value !== 'limit_concurrency';
  actionHelp.textContent = actionDescriptions[actionSelect.value] || '';
}

async function sendScheduleRequest(url, options) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.error || t('Modification impossible.'));
  }
  return data;
}

actionSelect.addEventListener('change', syncActionFields);
syncActionFields();

document.getElementById('schedule-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const elements = form.elements;
  const days = Array.from(form.querySelectorAll('input[name="days"]:checked')).map((item) => item.value);
  if (days.length === 0) {
    showScheduleFeedback(t('Sélectionne au moins un jour.'));
    return;
  }
  const maxWorkers = Number.parseInt(elements.namedItem('max_concurrent_jobs').value, 10);
  const payload = {
    name: elements.namedItem('name').value.trim(),
    start: elements.namedItem('start').value,
    end: elements.namedItem('end').value,
    action: elements.namedItem('action').value,
    days,
    enabled: elements.namedItem('enabled').checked,
    action_params: elements.namedItem('action').value === 'limit_concurrency' ? {max_concurrent_jobs: maxWorkers || 1} : {}
  };
  try {
    await sendScheduleRequest('/api/schedule/windows', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    window.location.reload();
  } catch (error) {
    showScheduleFeedback(error.message);
  }
});

document.querySelectorAll('[data-toggle]').forEach((button) => {
  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      await sendScheduleRequest(`/api/schedule/windows/${button.dataset.toggle}`, {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({enabled: button.dataset.enabled !== '1'})
      });
      window.location.reload();
    } catch (error) {
      button.disabled = false;
      showScheduleFeedback(error.message);
    }
  });
});

document.querySelectorAll('[data-delete]').forEach((button) => {
  button.addEventListener('click', async () => {
    if (!window.confirm(t('Supprimer ce créneau ?'))) {
      return;
    }
    button.disabled = true;
    try {
      await sendScheduleRequest(`/api/schedule/windows/${button.dataset.delete}`, {method: 'DELETE'});
      window.location.reload();
    } catch (error) {
      button.disabled = false;
      showScheduleFeedback(error.message);
    }
  });
});
