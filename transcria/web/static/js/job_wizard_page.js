/*
 * job_wizard_page.js — initialisation de la page wizard : pré-remplissage des
 * participants et du lexique depuis les suggestions LLM, champs spécifiques au
 * type de réunion. Dépend de wizard.js (TranscrIA.*) — chargé après lui.
 *
 * Extrait du bloc inline de job_wizard.html (vague A3). Les données Jinja
 * arrivent par window.__WIZARD_LLM_PARTICIPANTS__ / window.__WIZARD_LEX_TERMS__
 * (init 1 ligne dans le template, à côté de __JOB_ID__).
 */
(function(){
  // Auto-fill participants from LLM suggestions
  var nameInputs = document.querySelectorAll('.speaker-name');
  var hasAny = false;
  nameInputs.forEach(function(el) { if (el.value.trim()) hasAny = true; });
  if (!hasAny) {
    var llmParts = window.__WIZARD_LLM_PARTICIPANTS__ || [];
    llmParts.forEach(function(line) {
      var cleaned = line.replace('👤 ', '').trim();
      var speakerMatch = cleaned.match(/^(SPEAKER_\d+)\s*(?:\[([^\]]+)\])?\s*:\s*(.+)$/);
      var name = cleaned;
      var role = '';
      if (speakerMatch) {
        var rest = (speakerMatch[3] || '').trim();
        var split = rest.split(/\s+[—–-]\s+/);
        name = speakerMatch[2] ? speakerMatch[2].trim() : (split.length > 1 ? split[0].trim() : speakerMatch[1]);
        role = split.length > 1 ? split.slice(1).join(' — ').trim() : rest;
      } else {
        var parts = cleaned.split(':');
        name = (parts[0] || cleaned).trim();
        role = parts.length > 1 ? parts.slice(1).join(':').trim() : '';
      }
      // Chercher la ligne HTML correspondant au SPEAKER_XX de cette entrée LLM
      // Extraire le label seul depuis "SPEAKER_XX [label]" → "label"
      var labelMatch = name.match(/\[([^\]]+)\]/);
      var displayName = labelMatch ? labelMatch[1].trim() : name;
      var m = cleaned.match(/^(SPEAKER_\d+)/);
      if (m) {
        var row = document.getElementById('spk-' + m[1]);
        if (row) {
          var ni = row.querySelector('.speaker-name');
          var ri = row.querySelector('.speaker-role');
          if (ni && !ni.value.trim()) ni.value = displayName;
          if (ri && !ri.value.trim()) ri.value = role;
          return;
        }
      }
      // Fallback : remplir le premier champ vide (si pas de préfixe SPEAKER_XX)
      var unfilled = Array.from(nameInputs).find(function(el) { return !el.value.trim(); });
      if (unfilled) {
        unfilled.value = displayName;
        if (role) {
          var row2 = unfilled.closest('.speaker-item');
          var ri2 = row2 && row2.querySelector('.speaker-role');
          if (ri2 && !ri2.value.trim()) ri2.value = role;
        }
      }
    });
  }

  // Auto-fill lexicon from LLM suggestions (vide côté serveur si un lexique de
  // session existe déjà — même condition que l'ancien {% if %} du template).
  var lexTerms = window.__WIZARD_LEX_TERMS__ || [];
  var lexList = document.getElementById('lexicon-list');
  if (lexList && lexTerms.length > 0 && lexList.children.length === 0) {
    lexTerms.forEach(function(t) {
      TranscrIA.renderLexiconRow(t);
    });
  }

  // Initialiser les champs spécifiques au type au chargement
  (function() {
    var sel = document.getElementById('meeting_type_select');
    if (sel && sel.value) { TranscrIA.updateTypeSpecificFields(sel.value); }
  })();
})();
