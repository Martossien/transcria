var TranscrIA = window.TranscrIA || {};

(function () {
    var W = TranscrIA;

    var root = document.getElementById('wizard-root');
    if (!root) {
        console.error('[TranscrIA] ERREUR: #wizard-root introuvable dans le DOM');
        return;
    }
    var JOB_ID = window.__JOB_ID__ || root.dataset.jobId;
    if (!JOB_ID) {
        console.error('[TranscrIA] ERREUR: data-job-id manquant sur #wizard-root');
        return;
    }
    console.log('[TranscrIA] wizard.js initialisé, job=' + JOB_ID);

    W.escapeHtml = W.escapeHtml || function (value) {
        return String(value || '').replace(/[&<>"']/g, function (char) {
            return {
                '&': '&amp;',
                '<': '&lt;',
                '>': '&gt;',
                '"': '&quot;',
                "'": '&#39;'
            }[char];
        });
    };

    W.uploadFile = function () {
        console.log('[TranscrIA] uploadFile() appelé');
        var fi = document.getElementById('file-upload');
        if (!fi) { console.error('[TranscrIA] #file-upload introuvable'); return; }
        var file = fi.files[0];
        if (!file) { alert('Veuillez choisir un fichier.'); return; }
        W.showSpinner('upload-spinner');
        var fd = new FormData();
        fd.append('file', file);
        fetch('/api/jobs/' + JOB_ID + '/upload', { method: 'POST', body: fd })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                W.hideSpinner('upload-spinner');
                if (data.error) {
                    document.getElementById('upload-result').innerHTML =
                        '<div class="alert alert-danger">' + t('Erreur :') + ' ' + data.error + '</div>';
                } else {
                    document.getElementById('upload-result').innerHTML =
                        '<div class="alert alert-success">' + t('Fichier téléversé. Rechargement…') + '</div>';
                    W.reloadAfter(1000);
                }
            })
            .catch(function (err) {
                W.hideSpinner('upload-spinner');
                console.error('[TranscrIA] uploadFile error:', err);
                document.getElementById('upload-result').innerHTML =
                    '<div class="alert alert-danger">' + t('Erreur réseau : %(msg)s', { msg: err.message }) + '</div>';
            });
    };

    W.analyzeAudio = function () {
        console.log('[TranscrIA] analyzeAudio() appelé');
        W.showSpinner('analyze-spinner');
        W.api('/api/jobs/' + JOB_ID + '/analyze').then(function (r) {
            W.hideSpinner('analyze-spinner');
            if (r.data.error) {
                document.getElementById('analyze-result').innerHTML =
                    '<div class="alert alert-danger">' + r.data.error + '</div>';
            } else {
                location.reload();
            }
        });
    };

    W.generateSummary = function () {
        console.log('[TranscrIA] generateSummary() appelé');
        W.showSpinner('summary-spinner');
        var minEl = document.getElementById('speaker-min');
        var maxEl = document.getElementById('speaker-max');
        var hint = {
            min: (minEl && minEl.value !== '') ? parseInt(minEl.value, 10) : null,
            max: (maxEl && maxEl.value !== '') ? parseInt(maxEl.value, 10) : null
        };
        var inviteEl = document.getElementById('meeting-invite');
        var inviteText = inviteEl ? inviteEl.value.trim() : '';
        // Mémoriser la fourchette de locuteurs puis, le cas échéant, le brief
        // d'invitation, avant de lancer le résumé (diarisation + LLM).
        W.api('/api/jobs/' + JOB_ID + '/speaker-hint', 'POST', hint).then(function () {
            if (inviteText) {
                return W.api('/api/jobs/' + JOB_ID + '/meeting-invite', 'POST', { text: inviteText });
            }
            return null;
        }).then(function () {
            return W.api('/api/jobs/' + JOB_ID + '/summary');
        }).then(function (r) {
            W.hideSpinner('summary-spinner');
            if (r.data.vram_wait || r.data.queued) {
                // Résumé pris en charge côté SERVEUR (nœud GPU) : soit en attente de VRAM
                // (reprise auto), soit enfilé parce que le frontal n'a pas de GPU (split).
                // Dans les deux cas le client se contente de POLLER l'état — il NE relance
                // PAS /summary (sinon course avec le worker).
                var msg = r.data.message ||
                    t('VRAM insuffisante : l\'administrateur a été prévenu. Le résumé reprendra automatiquement dès que la mémoire GPU sera libérée.');
                W.showVramWaitBanner(msg);
                W.pollSummaryResume();
                return;
            }
            if (r.data.summary_llm_failed) {
                // La LLM n'a rien produit après 3 tentatives : transcript conservé, job
                // non validé mais relançable (la relance réutilise le STT en cache).
                var failMsg = r.data.message ||
                    t('Le résumé n\'a pas pu être généré (LLM sans production après 3 tentatives). La transcription est conservée — vous pouvez relancer.');
                document.getElementById('summary-result').innerHTML =
                    '<div class="alert alert-warning">' + W.escapeHtml(failMsg) +
                    '<div class="mt-2"><button class="btn btn-sm btn-primary" onclick="TranscrIA.generateSummary()">' +
                    t('Relancer le résumé') + '</button></div></div>';
                return;
            }
            if (r.data.error) {
                document.getElementById('summary-result').innerHTML =
                    '<div class="alert alert-danger">' + r.data.error + '</div>';
            } else {
                location.reload();
            }
        });
    };

    W.showWaitBanner = function (elId, msg) {
        var el = document.getElementById(elId);
        if (!el) { return; }
        el.innerHTML =
            '<div class="alert alert-warning"><span class="spinner-border spinner-border-sm me-2"></span>' +
            W.escapeHtml(msg) + '</div>';
    };
    W.showVramWaitBanner = function (msg) { W.showWaitBanner('summary-result', msg); };

    // Poll en lecture seule pendant qu'une étape GPU s'exécute côté SERVEUR (worker GPU) :
    // reprise après attente VRAM, ou frontal sans GPU qui a délégué au nœud de ressources.
    // Recharge la page dès que l'exécution n'est plus active (terminée — succès OU échec ;
    // le rechargement affiche l'état réel). Aucun POST de relance ici (pas de course).
    W.pollServerStep = function () {
        W.api('/api/jobs/' + JOB_ID + '/status').then(function (r) {
            var es = (r.data && r.data.execution_status) || 'idle';
            if (es === 'idle') { location.reload(); return; }   // terminé → recharger
            setTimeout(W.pollServerStep, 20000);
        });
    };
    W.pollSummaryResume = W.pollServerStep;  // compat (résumé)

    // Auto-sauvegarde de l'invitation au blur : un collé survit ainsi à une étape
    // échouée/abandonnée (la sauvegarde au clic « Générer le résumé » reste en place).
    (function () {
        var inviteEl = document.getElementById('meeting-invite');
        if (!inviteEl) { return; }
        inviteEl.addEventListener('blur', function () {
            var text = inviteEl.value.trim();
            if (!text) { return; }
            W.api('/api/jobs/' + JOB_ID + '/meeting-invite', 'POST', { text: text });
        });
    })();

    W.validateSummary = function (choice) {
        console.log('[TranscrIA] validateSummary(' + choice + ')');
        if (choice === 'no') {
            W.api('/api/jobs/' + JOB_ID + '/process', 'POST', { mode: 'cancel' }).then(function () {
                location.reload();
            });
            return;
        }
        location.reload();
    };

    W.prefillContext = function () {
        console.log('[TranscrIA] prefillContext()');
        var root = document.getElementById('wizard-root');
        var title = root.dataset.titleSuggere || '';
        var topic = root.dataset.sujetSuggere || '';
        var objective = root.dataset.objectifSuggere || '';
        var notes = root.dataset.notesSuggeres || '';
        var typeSuggere = root.dataset.typeSuggere || '';

        if (title) document.querySelector('input[name="title"]').value = title;
        if (topic) document.getElementById('topic_input').value = topic;
        if (objective) document.getElementById('objective_input').value = objective;
        if (notes) document.getElementById('notes_input').value = notes;
        if (typeSuggere) {
            var sel = document.getElementById('meeting_type_select');
            for (var i = 0; i < sel.options.length; i++) {
                if (sel.options[i].value === typeSuggere) { sel.selectedIndex = i; break; }
            }
        }
    };

    var promoteRow = null;

    W.openPromoteLexicon = function (btn) {
        promoteRow = btn.closest('.lexicon-row');
        var term = (promoteRow.querySelector('.lex-term') || {}).value || '';
        if (!term.trim()) { alert(t('Renseignez d\'abord la forme validée.')); return; }
        document.getElementById('lex-promote-term').textContent = term.trim();
        document.getElementById('lex-promote-error').classList.add('d-none');
        var sel = document.getElementById('lex-promote-select');
        var newBox = document.getElementById('lex-promote-new');
        if (sel) {
            sel.onchange = function () { newBox.classList.toggle('d-none', sel.value !== ''); };
            newBox.classList.toggle('d-none', sel.value !== '');
        }
        new bootstrap.Modal(document.getElementById('lex-promote-modal')).show();
    };

    W.confirmPromoteLexicon = function () {
        if (!promoteRow) { return; }
        var sel = document.getElementById('lex-promote-select');
        var payload = {
            term: (promoteRow.querySelector('.lex-term') || {}).value || '',
            variants: ((promoteRow.querySelector('.lex-variants') || {}).value || '')
                .split(',').map(function (s) { return s.trim(); }).filter(Boolean),
            category: (promoteRow.querySelector('.lex-cat') || {}).value || '',
            priority: (promoteRow.querySelector('.lex-prio') || {}).value || 'normale'
        };
        if (sel && sel.value) {
            payload.lexicon_id = sel.value;
        } else {
            payload.new_lexicon_name = (document.getElementById('lex-promote-name') || {}).value || '';
            var grp = document.getElementById('lex-promote-group');
            if (grp) { payload.group_id = grp.value; }
        }
        W.api('/api/jobs/' + JOB_ID + '/lexicon/promote', 'POST', payload).then(function (r) {
            var err = document.getElementById('lex-promote-error');
            if (r.status !== 200) {
                err.textContent = (r.data && r.data.error) || 'Échec de l\'ajout.';
                err.classList.remove('d-none');
                return;
            }
            bootstrap.Modal.getInstance(document.getElementById('lex-promote-modal')).hide();
            var badge = document.createElement('span');
            badge.className = 'badge text-bg-success ms-1';
            badge.textContent = '→ ' + r.data.lexicon.name + (r.data.created_lexicon ? ' ' + t('(créé)') : '');
            var btn = promoteRow.querySelector('.lex-promote');
            if (btn) { btn.replaceWith(badge); } else { promoteRow.appendChild(badge); }
            if (r.data.created_lexicon && document.getElementById('lex-promote-select')) {
                var opt = document.createElement('option');
                opt.value = r.data.lexicon.id;
                opt.textContent = r.data.lexicon.name;
                document.getElementById('lex-promote-select').insertBefore(
                    opt, document.getElementById('lex-promote-select').lastElementChild);
            }
            promoteRow = null;
        });
    };

    W.saveContext = function () {
        console.log('[TranscrIA] saveContext()');
        var form = document.getElementById('context-form');
        var fd = new FormData(form);
        var data = {};
        fd.forEach(function (v, k) { data[k] = v; });

        // Collecter les champs spécifiques au type
        var tsData = {};
        var tsFields = window.__TYPE_SPECIFIC_FIELDS__ || {};
        var currentType = (document.getElementById('meeting_type_select') || {}).value || '';
        var fieldsForType = tsFields[currentType] || [];
        fieldsForType.forEach(function (field) {
            var el = document.getElementById('ts_' + field.key);
            if (el) { tsData[field.key] = el.value; }
        });
        if (Object.keys(tsData).length > 0) {
            data['type_specific_data'] = tsData;
        }

        W.api('/api/jobs/' + JOB_ID + '/context', 'POST', data).then(function (r) {
            if (r.status === 200) {
                document.getElementById('context-saved').classList.remove('d-none');
                W.reloadAfter(500);
            }
        });
    };

    W.updateTypeSpecificFields = function (meetingType) {
        var tsFields = window.__TYPE_SPECIFIC_FIELDS__ || {};
        var tsData   = window.__TYPE_SPECIFIC_DATA__   || {};
        var fields   = tsFields[meetingType] || [];
        var container = document.getElementById('type-specific-fields');
        var body      = document.getElementById('type-specific-body');
        var title     = document.getElementById('type-specific-title');

        if (!container || !body) { return; }

        if (fields.length === 0) {
            container.style.display = 'none';
            return;
        }

        // Titre contextuel
        var titles = {
            'CSE': 'Informations légales PV',
            'CSE extraordinaire': 'Informations légales PV — Séance extraordinaire',
            'Point projet': 'Contexte projet',
            'CODIR / COMEX': 'Ordre du jour & indicateurs',
            'Réunion client': 'Informations client',
            'Entretien individuel': 'Informations entretien',
            'Formation': 'Informations formation',
            'Réunion de crise': 'Informations incident',
            'Séminaire / atelier': 'Informations séminaire',
            'Négociation': 'Informations négociation'
        };
        if (title) { title.textContent = t(titles[meetingType] || 'Informations complémentaires'); }

        // Générer les champs
        var html = '<div class="row g-2">';
        fields.forEach(function (field) {
            var val = tsData[field.key] || '';
            var colClass = field.type === 'textarea' ? 'col-12' : 'col-md-6';
            html += '<div class="' + colClass + '">';
            html += '<label class="form-label small mb-1">' + field.label + '</label>';
            if (field.type === 'textarea') {
                html += '<textarea id="ts_' + field.key + '" class="form-control form-control-sm" rows="3">' + W._escHtml(val) + '</textarea>';
            } else {
                html += '<input type="' + field.type + '" id="ts_' + field.key + '" class="form-control form-control-sm" value="' + W._escHtml(val) + '">';
            }
            html += '</div>';
        });
        html += '</div>';

        // CSE : indicateur quorum calculé en temps réel
        if (meetingType === 'CSE' || meetingType === 'CSE extraordinaire') {
            html += '<div class="mt-2 small" id="ts-quorum-indicator"></div>';
        }

        body.innerHTML = html;
        container.style.display = 'block';

        // Listener quorum dynamique pour CSE
        if (meetingType === 'CSE' || meetingType === 'CSE extraordinaire') {
            ['ts_membres_presents', 'ts_membres_total'].forEach(function (id) {
                var el = document.getElementById(id);
                if (el) { el.addEventListener('input', W._updateQuorum); }
            });
            W._updateQuorum();
        }
    };

    W._escHtml = function (str) {
        return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    };

    W._updateQuorum = function () {
        var ind = document.getElementById('ts-quorum-indicator');
        if (!ind) { return; }
        var presents = parseInt((document.getElementById('ts_membres_presents') || {}).value || '0', 10);
        var total    = parseInt((document.getElementById('ts_membres_total')    || {}).value || '0', 10);
        if (!presents || !total) { ind.innerHTML = ''; return; }
        var pct = Math.round(100 * presents / total);
        var ok  = presents > total / 2;
        ind.innerHTML = '<span class="badge ' + (ok ? 'bg-success' : 'bg-danger') + '">'
            + (ok ? '✓ Quorum atteint' : '✗ Quorum non atteint')
            + ' (' + presents + '/' + total + ' — ' + pct + '%)</span>';
    };

    W.addParticipantRow = function () {
        console.log('[TranscrIA] addParticipantRow()');
        var container = document.getElementById('participants-list');
        var row = document.createElement('div');
        row.className = 'speaker-item';
        row.innerHTML = '<span class="text-muted small">' + t('nouveau') + '</span>' +
            '<input type="text" class="form-control form-control-sm speaker-name" placeholder="' + t('Nom') + '" style="max-width:150px;">' +
            '<input type="text" class="form-control form-control-sm speaker-func" placeholder="' + t('Fonction') + '" style="max-width:130px;">' +
            '<input type="text" class="form-control form-control-sm speaker-role" placeholder="' + t('Rôle dans la réunion') + '" style="max-width:150px;">' +
            '<button type="button" class="btn btn-sm btn-outline-danger" onclick="this.parentElement.remove()">×</button>';
        container.appendChild(row);
    };

    W.saveParticipantsAndSpeakers = function () {
        console.log('[TranscrIA] saveParticipantsAndSpeakers()');
        var items = document.querySelectorAll('#participants-list .speaker-item');
        var participants = [];
        var mapping = {};
        var pi = 0;
        items.forEach(function (row) {
            var nameEl = row.querySelector('.speaker-name');
            var funcEl = row.querySelector('.speaker-func');
            var roleEl = row.querySelector('.speaker-role');
            var genderEl = row.querySelector('.speaker-gender');
            var name = (nameEl && nameEl.value || '').trim();
            var func = (funcEl && funcEl.value || '').trim();
            var role = (roleEl && roleEl.value || '').trim();
            var gender = (genderEl && genderEl.value || '').trim();
            if (name) {
                pi++;
                var pid = 'p' + pi;
                participants.push({ id: pid, name: name, function: func, role: role,
                    is_animator: false, expected: true });
                var spkLabel = row.querySelector('strong');
                if (spkLabel) {
                    mapping[spkLabel.textContent] = { name: name, participant_id: pid,
                        function: func, role: role, gender: gender };
                }
            }
        });
        W.api('/api/jobs/' + JOB_ID + '/participants', 'POST', participants).then(function (participantsResult) {
            if (participantsResult.status !== 200 || participantsResult.data.error) {
                alert(participantsResult.data.error || 'Erreur lors de l\'enregistrement des participants.');
                return;
            }
            if (Object.keys(mapping).length === 0) {
                document.getElementById('participants-saved').classList.remove('d-none');
                W.reloadAfter(500);
                return;
            }
            W.api('/api/jobs/' + JOB_ID + '/speakers/map', 'POST', mapping).then(function (mappingResult) {
                if (mappingResult.status !== 200 || mappingResult.data.error) {
                    alert(mappingResult.data.error || 'Erreur lors de l\'enregistrement du mapping locuteurs.');
                    return;
                }
                document.getElementById('participants-saved').classList.remove('d-none');
                W.reloadAfter(500);
            });
        });
    };

    W.loadClips = function (speakerId) {
        console.log('[TranscrIA] loadClips(' + speakerId + ')');
        var container = document.getElementById('spk-' + speakerId);
        if (!container) return;
        var existing = container.parentElement.querySelector('.clips-audio');
        if (existing) { existing.remove(); return; }

        var audioDiv = document.createElement('div');
        audioDiv.className = 'clips-audio';
        audioDiv.innerHTML = '<small class="text-muted"><span class="spinner"></span> Chargement...</small>';
        container.parentElement.appendChild(audioDiv);

        fetch('/api/jobs/' + JOB_ID + '/speakers/clips')
            .then(function (r) {
                return r.json().then(function (data) {
                    if (!r.ok) throw new Error(data.error || 'Chargement impossible');
                    return data;
                });
            })
            .then(function (data) {
                var clips = (data.clips || {})[speakerId] || [];
                if (clips.length === 0) {
                    audioDiv.innerHTML = '<small class="text-muted">Aucun extrait disponible</small>';
                    return;
                }
                var html = '';
                clips.forEach(function (clipName, i) {
                    var safeName = String(clipName || '').split('/').map(encodeURIComponent).join('/');
                    html += '<small class="text-muted">Extrait ' + (i+1) + ' :</small>' +
                        '<audio controls preload="none">' +
                        '<source src="/api/jobs/' + JOB_ID + '/speakers/clip/' + safeName +
                        '" type="audio/wav"></audio>';
                });
                audioDiv.innerHTML = html;
            })
            .catch(function (error) {
                audioDiv.innerHTML = '<small class="text-danger">' + W.escapeHtml(error.message || 'Erreur chargement') + '</small>';
            });
    };

    W.detectSpeakers = function () {
        console.log('[TranscrIA] detectSpeakers()');
        W.showSpinner('speaker-spinner');
        W.api('/api/jobs/' + JOB_ID + '/speakers/detect').then(function (r) {
            W.hideSpinner('speaker-spinner');
            if (r.data && r.data.queued) {
                // Frontal sans GPU : la détection a été déléguée au worker GPU. On poll
                // l'état et on recharge dès qu'elle est terminée (pas de re-POST).
                W.showWaitBanner('speaker-result', r.data.message ||
                    t('Détection des locuteurs lancée sur le nœud GPU — la page se rafraîchira.'));
                W.pollServerStep();
                return;
            }
            if (r.status === 200) { location.reload(); }
            else {
                document.getElementById('speaker-result').innerHTML =
                    '<div class="alert alert-warning">' +
                    (r.data.error || r.data.message || 'Indisponible') + '</div>';
            }
        });
    };

    W.matchKnownVoices = function () {
        console.log('[TranscrIA] matchKnownVoices()');
        W.showSpinner('voice-match-spinner');
        W.api('/api/jobs/' + JOB_ID + '/speakers/voice-match').then(function (r) {
            W.hideSpinner('voice-match-spinner');
            var target = document.getElementById('voice-match-result');
            if (r.status === 200) {
                if (target) target.textContent = (r.data.matches || []).length + ' suggestion(s) disponible(s).';
                W.reloadAfter(700);
                return;
            }
            if (target) {
                target.textContent = r.data.message || r.data.error || 'Aucune suggestion disponible.';
                target.classList.remove('text-muted');
                target.classList.add('text-warning');
            }
        });
    };

    W.applyVoiceSuggestion = function (button) {
        var speakerId = button && button.dataset ? button.dataset.speaker : '';
        var suggestedName = button && button.dataset ? button.dataset.name : '';
        var suggestedGender = button && button.dataset ? button.dataset.gender : '';
        if (!speakerId || !suggestedName) return;
        var row = document.getElementById('spk-' + speakerId);
        if (!row) return;
        var nameInput = row.querySelector('.speaker-name');
        if (nameInput) nameInput.value = suggestedName;
        var genderInput = row.querySelector('.speaker-gender');
        if (genderInput && suggestedGender) genderInput.value = suggestedGender;
        button.classList.remove('btn-outline-success');
        button.classList.add('btn-success');
        button.textContent = 'Voix retenue';
    };

    W.formatLexiconVariants = function (variants) {
        if (Array.isArray(variants)) return variants.join(', ');
        return variants || '';
    };

    W.parseLexiconContexts = function (row) {
        var node = row.querySelector('.lex-contexts-json');
        if (!node) return [];
        try {
            var parsed = JSON.parse(node.textContent || '[]');
            return Array.isArray(parsed) ? parsed : [];
        } catch (e) {
            return [];
        }
    };

    W.writeLexiconContexts = function (row, contexts) {
        var node = row && row.querySelector('.lex-contexts-json');
        if (!node) return;
        node.textContent = JSON.stringify(Array.isArray(contexts) ? contexts : []);
    };

    W.getRowValue = function (row, selector) {
        var node = row && row.querySelector(selector);
        return node ? (node.value || '') : '';
    };

    W.updateLexiconContextCounter = function (row) {
        if (!row) return;
        var inputs = row.querySelectorAll('.lex-context-listened-input');
        var listened = Array.from(inputs).filter(function (input) { return input.checked; }).length;
        var counter = row.querySelector('.lex-context-counter');
        if (counter) {
            counter.dataset.listened = String(listened);
            counter.dataset.total = String(inputs.length);
            counter.textContent = ' ' + t('· %(listened)s/%(total)s écoutés', { listened: listened, total: inputs.length });
        }
    };

    W.setLexiconContextListened = function (input) {
        var row = input && input.closest('.lexicon-row');
        if (!row) return;
        var index = Number(input.dataset.contextIndex || -1);
        var contexts = W.parseLexiconContexts(row);
        if (index >= 0 && contexts[index]) {
            contexts[index].listened = !!input.checked;
            W.writeLexiconContexts(row, contexts);
        }
        W.updateLexiconContextCounter(row);
    };

    W.stopLexiconContextAudio = function () {
        var current = W._currentLexiconAudio;
        if (current && current.audio) {
            current.audio.pause();
        }
        if (current && current.button) {
            current.button.classList.remove('active');
            current.button.innerHTML = '<i class="bi bi-play-fill"></i>';
        }
        W._currentLexiconAudio = null;
    };

    W.toggleLexiconContextAudio = function (button) {
        if (!button || button.disabled) return;
        var item = button.closest('.lex-context-item');
        if (!item) return;
        var timecode = button.dataset.timecode || '';
        var quote = button.dataset.quote || '';
        if (!timecode.trim() && !quote.trim()) {
            var missingError = item.querySelector('.lex-context-audio-error');
            if (!missingError) {
                missingError = document.createElement('span');
                missingError.className = 'text-danger lex-context-audio-error';
                item.querySelector('.lex-context-actions').appendChild(missingError);
            }
            missingError.textContent = 'Extrait indisponible';
            return;
        }

        if (W._currentLexiconAudio && W._currentLexiconAudio.button === button) {
            if (W._currentLexiconAudio.audio.paused) {
                W._currentLexiconAudio.audio.play();
                button.innerHTML = '<i class="bi bi-pause-fill"></i>';
            } else {
                W.stopLexiconContextAudio();
            }
            return;
        }

        W.stopLexiconContextAudio();
        var audio = new Audio(
            '/api/jobs/' + JOB_ID + '/audio/excerpt?pad=5&timecode=' +
            encodeURIComponent(timecode) + '&quote=' + encodeURIComponent(quote)
        );
        W._currentLexiconAudio = { audio: audio, button: button };
        button.classList.add('active');
        button.innerHTML = '<span class="spinner"></span>';

        audio.addEventListener('playing', function () {
            button.innerHTML = '<i class="bi bi-pause-fill"></i>';
        });
        audio.addEventListener('ended', function () {
            W.stopLexiconContextAudio();
        });
        audio.addEventListener('error', function () {
            W.stopLexiconContextAudio();
            var error = item.querySelector('.lex-context-audio-error');
            if (!error) {
                error = document.createElement('span');
                error.className = 'text-danger lex-context-audio-error';
                item.querySelector('.lex-context-actions').appendChild(error);
            }
            error.textContent = 'Extrait indisponible';
        });
        audio.play().catch(function () {
            W.stopLexiconContextAudio();
        });
    };

    W.renderLexiconContexts = function (contexts) {
        if (!Array.isArray(contexts) || contexts.length === 0) return '';
        var html = '<details class="lex-contexts mt-2">' +
            '<summary class="small text-muted">' + t('Contexte proposé (%(n)s)', { n: contexts.length }) +
            '<span class="lex-context-counter"> ' + t('· %(listened)s/%(total)s écoutés', { listened: 0, total: contexts.length }) + '</span></summary>' +
            '<div class="small mt-2">';
        contexts.forEach(function (c) {
            var meta = (c.timecode || t('sans timecode')) + (c.speaker ? ' — ' + c.speaker : '');
            html += '<div class="lex-context-item">' +
                '<span class="text-muted"></span>' +
                '<div class="lex-context-quote"></div>' +
                (c.reason ? '<div class="text-muted lex-context-reason"></div>' : '') +
                '<div class="lex-context-actions">' +
                '<button type="button" class="btn btn-sm btn-outline-primary lex-context-play" title="' + t('Écouter 5 secondes avant et après') + '" onclick="TranscrIA.toggleLexiconContextAudio(this)"><i class="bi bi-play-fill"></i></button>' +
                '<label class="lex-context-listened"><input type="checkbox" class="form-check-input lex-context-listened-input" onchange="TranscrIA.setLexiconContextListened(this)"> ' + t('Écouté') + '</label>' +
                '</div>' +
                '</div>';
        });
        html += '</div></details>';
        return html;
    };

    W.fillLexiconContexts = function (row, contexts) {
        var items = row.querySelectorAll('.lex-context-item');
        contexts.forEach(function (c, index) {
            var item = items[index];
            if (!item) return;
            var meta = (c.timecode || t('sans timecode')) + (c.speaker ? ' — ' + c.speaker : '');
            var metaEl = item.querySelector('span');
            var quoteEl = item.querySelector('.lex-context-quote');
            var reasonEl = item.querySelector('.lex-context-reason');
            var playBtn = item.querySelector('.lex-context-play');
            var listenedInput = item.querySelector('.lex-context-listened-input');
            if (metaEl) metaEl.textContent = meta;
            if (quoteEl) quoteEl.textContent = '« ' + (c.quote || '') + ' »';
            if (reasonEl) reasonEl.textContent = c.reason || '';
            item.dataset.contextIndex = String(index);
            if (playBtn) {
                playBtn.dataset.timecode = c.timecode || '';
                playBtn.dataset.quote = c.quote || '';
                // Préférer audio_available (calculé côté serveur, fiable) quand présent ;
                // fallback sur la présence du timecode pour les lignes ajoutées manuellement.
                playBtn.disabled = Object.prototype.hasOwnProperty.call(c, 'audio_available')
                    ? !c.audio_available
                    : !(c.timecode || '').trim();
            }
            if (listenedInput) {
                listenedInput.dataset.contextIndex = String(index);
                listenedInput.checked = !!c.listened;
            }
        });
        W.updateLexiconContextCounter(row);
    };

    W.renderLexiconRow = function (term) {
        var container = document.getElementById('lexicon-list');
        var row = document.createElement('div');
        var td = term || {};   // NB : `t` reste le helper i18n global — la donnée est `td`.
        row.className = 'lexicon-row lexicon-card';
        row.innerHTML = '<div class="lexicon-grid">' +
            '<label class="form-label small mb-1">' + t('Forme validée') + '</label>' +
            '<label class="form-label small mb-1">' + t('Formes suspectes observées') + '</label>' +
            '<label class="form-label small mb-1">' + t('Catégorie') + '</label>' +
            '<label class="form-label small mb-1">' + t('Priorité') + '</label>' +
            '<input type="text" class="form-control form-control-sm lex-term" placeholder="' + t('Ex: Forme validée') + '" value="" data-field="term">' +
            '<input type="text" class="form-control form-control-sm lex-variants" placeholder="' + t('Ex: Forme douteuse A, forme douteuse B') + '" value="" data-field="variants">' +
            '<input type="text" class="form-control form-control-sm lex-cat" list="lexicon-cat-list" placeholder="' + t('Catégorie libre') + '" value="" data-field="category">' +
            '<select class="form-select form-select-sm lex-prio" style="max-width:110px;">' +
            (document.getElementById('lexicon-prio-tpl') ? document.getElementById('lexicon-prio-tpl').innerHTML : '') +
            '</select>' +
            '</div>' +
            '<input type="hidden" class="lex-replace" value="">' +
            '<input type="hidden" class="lex-source" value="">' +
            '<input type="hidden" class="lex-central-entry-id" value="">' +
            '<input type="hidden" class="lex-central-lexicon-id" value="">' +
            '<input type="hidden" class="lex-central-lexicon-name" value="">' +
            '<input type="hidden" class="lex-display-reason" value="">' +
            '<div class="mt-2 lex-central-badge d-none"></div>' +
            '<script type="application/json" class="lex-contexts-json">[]</script>' +
            '<textarea class="form-control form-control-sm lex-comment mt-2" rows="2" placeholder="' + t('Pourquoi cette forme doit être validée') + '"></textarea>' +
            W.renderLexiconContexts(td.contexts || []) +
            (document.getElementById('lex-promote-modal')
                ? '<button type="button" class="btn btn-sm btn-outline-secondary lex-promote" title="' + t('Ajouter cette forme validée à un lexique central, partagé et réutilisé sur les prochains jobs') + '" onclick="TranscrIA.openPromoteLexicon(this)"><i class="bi bi-journal-plus"></i> ' + t('Au lexique central') + '</button>'
                : '') +
            '<button type="button" class="btn btn-sm btn-outline-danger lex-remove" onclick="this.parentElement.remove()">×</button>';
        container.appendChild(row);

        row.querySelector('.lex-term').value = td.term || '';
        row.querySelector('.lex-variants').value = W.formatLexiconVariants(td.variants);
        row.querySelector('.lex-cat').value = td.category || '';
        row.querySelector('.lex-prio').value = td.priority || 'normale';
        row.querySelector('.lex-replace').value = td.replace_by || '';
        row.querySelector('.lex-source').value = td.source || '';
        row.querySelector('.lex-central-entry-id').value = td.central_entry_id || '';
        row.querySelector('.lex-central-lexicon-id').value = td.central_lexicon_id || '';
        row.querySelector('.lex-central-lexicon-name').value = td.central_lexicon_name || '';
        row.querySelector('.lex-display-reason').value = td._display_reason || '';
        var centralBadge = row.querySelector('.lex-central-badge');
        if (centralBadge && td.central_lexicon_name) {
            centralBadge.classList.remove('d-none');
            centralBadge.innerHTML = '<span class="badge text-bg-light border">' + t('Lexique central : %(name)s', { name: W.escapeHtml(td.central_lexicon_name) }) + '</span>';
        }
        if (centralBadge && td.source === 'document') {
            centralBadge.classList.remove('d-none');
            centralBadge.insertAdjacentHTML('beforeend',
                '<span class="badge text-bg-info-subtle border text-info-emphasis ms-1" title="' + t('Proposé à partir d\'un document que vous avez joint à la réunion') + '"><i class="bi bi-paperclip"></i> ' + t('issu des documents fournis') + '</span>');
        }
        row.querySelector('.lex-comment').value = td.comment || '';
        row.querySelector('.lex-contexts-json').textContent = JSON.stringify(td.contexts || []);
        W.fillLexiconContexts(row, td.contexts || []);
        return row;
    };

    W.addLexiconTerm = function () {
        console.log('[TranscrIA] addLexiconTerm()');
        W.renderLexiconRow({});
    };

    W.importLexiconFile = function () {
        console.log('[TranscrIA] importLexiconFile()');
        document.getElementById('lexicon-file-input').click();
    };

    W.handleLexiconFile = function (input) {
        console.log('[TranscrIA] handleLexiconFile()');
        var file = input.files[0];
        if (!file) return;
        var reader = new FileReader();
        reader.onload = function (e) {
            fetch('/api/jobs/' + JOB_ID + '/lexicon', {
                method: 'POST', headers: { 'Content-Type': 'text/plain' },
                body: e.target.result
            }).then(function (r) { if (r.ok) location.reload(); });
        };
        reader.readAsText(file);
    };

    W.renderMeetingDocuments = function (documents) {
        var list = document.getElementById('meeting-doc-list');
        if (!list) return;
        list.innerHTML = '';
        (documents || []).forEach(function (doc, i) {
            var meta = (doc.format || '').toUpperCase();
            if (doc.pages) meta += ', ' + t('%(n)s page(s)', { n: doc.pages });
            if (doc.slides) meta += ', ' + t('%(n)s diapo(s)', { n: doc.slides });
            if (doc.images_skipped) meta += ', ' + t('%(n)s image(s) ignorée(s)', { n: doc.images_skipped });
            if (doc.truncated) meta += ' — ' + t('tronqué');
            var li = document.createElement('li');
            li.className = 'd-flex align-items-center justify-content-between border rounded px-2 py-1 mb-1';
            li.setAttribute('data-doc-index', i);
            var name = document.createElement('span');
            name.innerHTML = '<i class="bi bi-file-earmark-text"></i> ';
            name.appendChild(document.createTextNode(doc.name || 'document'));
            var metaSpan = document.createElement('span');
            metaSpan.className = 'text-muted';
            metaSpan.textContent = ' — ' + meta;
            name.appendChild(metaSpan);
            var btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'btn btn-link btn-sm text-danger p-0';
            btn.title = t('Retirer');
            btn.innerHTML = '<i class="bi bi-x-lg"></i>';
            btn.onclick = function () { W.removeMeetingDocument(i); };
            li.appendChild(name);
            li.appendChild(btn);
            list.appendChild(li);
        });
    };

    // Parse robuste : une réponse 413 (dépassement de MAX_CONTENT_LENGTH) ou toute
    // erreur serveur renvoie du HTML, pas du JSON — r.json() lèverait et masquerait la
    // vraie cause derrière « Erreur réseau ».
    W.parseDocResponse = function (r) {
        if (r.status === 413) {
            return Promise.resolve({ ok: false, body: { error: t('Fichier trop volumineux (dépasse la limite du serveur).') } });
        }
        return r.json().then(
            function (b) { return { ok: r.ok, body: b }; },
            function () { return { ok: r.ok, body: { error: t('Réponse serveur inattendue (HTTP %(status)s).', { status: r.status }) } }; }
        );
    };

    W.handleMeetingDocument = function (input) {
        console.log('[TranscrIA] handleMeetingDocument()');
        var errEl = document.getElementById('meeting-doc-error');
        if (errEl) errEl.textContent = '';
        var file = input.files[0];
        if (!file) return;
        var fd = new FormData();
        fd.append('file', file);
        fetch('/api/jobs/' + JOB_ID + '/meeting-invite/document', { method: 'POST', body: fd })
            .then(W.parseDocResponse)
            .then(function (res) {
                if (res.ok) {
                    W.renderMeetingDocuments(res.body.documents);
                } else if (errEl) {
                    errEl.textContent = res.body.error || t('Échec de l\'ajout du document.');
                }
            })
            .catch(function () { if (errEl) errEl.textContent = t('Erreur réseau.'); })
            .finally(function () { input.value = ''; });
    };

    W.removeMeetingDocument = function (index) {
        console.log('[TranscrIA] removeMeetingDocument()', index);
        var errEl = document.getElementById('meeting-doc-error');
        if (errEl) errEl.textContent = '';
        fetch('/api/jobs/' + JOB_ID + '/meeting-invite/document/' + index, { method: 'DELETE' })
            .then(W.parseDocResponse)
            .then(function (res) {
                if (res.ok) {
                    W.renderMeetingDocuments(res.body.documents);
                } else if (errEl) {
                    errEl.textContent = res.body.error || 'Échec de la suppression.';
                }
            });
    };

    W.saveLexicon = function () {
        console.log('[TranscrIA] saveLexicon()');
        var rows = document.querySelectorAll('#lexicon-list .lexicon-row');
        var data = [];
        rows.forEach(function (row) {
            var item = {
                term: W.getRowValue(row, '.lex-term'),
                category: W.getRowValue(row, '.lex-cat') || 'mot suspect',
                priority: W.getRowValue(row, '.lex-prio') || 'normale',
                replace_by: W.getRowValue(row, '.lex-replace'),
                variants: W.getRowValue(row, '.lex-variants').split(/[;,]/).map(function (v) { return v.trim(); }).filter(Boolean),
                comment: W.getRowValue(row, '.lex-comment'),
                contexts: W.parseLexiconContexts(row)
            };
            [
                ['source', '.lex-source'],
                ['central_entry_id', '.lex-central-entry-id'],
                ['central_lexicon_id', '.lex-central-lexicon-id'],
                ['central_lexicon_name', '.lex-central-lexicon-name'],
                ['_display_reason', '.lex-display-reason']
            ].forEach(function (pair) {
                var value = W.getRowValue(row, pair[1]).trim();
                if (value) item[pair[0]] = value;
            });
            data.push(item);
        });
        W.api('/api/jobs/' + JOB_ID + '/lexicon', 'POST', data).then(function (r) {
            if (r.status === 200) {
                document.getElementById('lexicon-saved').classList.remove('d-none');
                W.reloadAfter(500);
            }
        });
    };

    W.applySelectedLexicons = function () {
        console.log('[TranscrIA] applySelectedLexicons()');
        var selected = Array.from(document.querySelectorAll('.central-lexicon-toggle:checked'))
            .map(function (input) { return input.value; })
            .filter(Boolean);
        W.api('/api/jobs/' + JOB_ID + '/selected-lexicons', 'POST', {
            selected_lexicon_ids: selected
        }).then(function (r) {
            if (r.status === 200) {
                W.reloadAfter(300);
            }
        });
    };

    W.skipLexicon = function () {
        console.log('[TranscrIA] skipLexicon()');
        W.api('/api/jobs/' + JOB_ID + '/lexicon', 'POST', []).then(function () {
            location.reload();
        });
    };

    var _STATE_LABELS = {
        'ready_to_process': 'Démarrage…',
        'transcribing':     'Transcription ASR en cours…',
        'diarizing':        'Identification des locuteurs…',
        'arbitrating':      'Correction LLM en cours…',
        'quality_checking': 'Rapport qualité…',
        'quality_checked':  'Export en cours…',
        'export_ready':     'Finalisation…',
        'completed':        'Terminé.',
        'failed':           'Échec du traitement.',
        'cancelled':        'Traitement annulé.',
    };
    var _TERMINAL_STATES = ['completed', 'export_ready', 'failed', 'cancelled'];
    var _REPROCESSABLE_STATES = ['completed', 'quality_checked', 'export_ready', 'failed', 'cancelled'];
    var _LIVE_STATUS_STATES = [
        'summary_running',
        'speaker_detection_running',
        'transcribing',
        'diarizing',
        'arbitrating',
        'quality_checking'
    ];

    var _LLM_TIMEOUT_S = parseInt(
        (document.getElementById('wizard-root') || {}).dataset.llmTimeout || '7200', 10
    );
    var _LLM_WARN_S = Math.floor(_LLM_TIMEOUT_S * 0.5); // warning à 50% du timeout

    function _buildProcessingPoller(div, startTime) {
        var _warnShown = false;

        function elapsedS() { return Math.floor((Date.now() - startTime) / 1000); }
        function elapsedStr() {
            var s = elapsedS();
            return s < 60 ? s + 's' : Math.floor(s / 60) + 'min ' + (s % 60) + 's';
        }

        function setInfo(msg, progress) {
            var detail = '';
            if (progress && progress.message) {
                detail = '<span class="text-muted">·</span><span>' + W.escapeHtml(progress.message) + '</span>';
                if (typeof progress.percent === 'number') {
                    detail += '<span class="badge text-bg-light border">' + progress.percent + '%</span>';
                }
            }
            div.innerHTML = '<div class="alert alert-info d-flex align-items-center gap-2">' +
                '<span class="spinner-border spinner-border-sm" role="status"></span>' +
                '<span>' + W.escapeHtml(msg) + '</span>' + detail + '</div>';
        }

        function showLlmWarning() {
            div.innerHTML =
                '<div class="alert alert-warning">' +
                '<strong>' + t('⚠ Le traitement LLM prend plus de temps que prévu (%(elapsed)s).', { elapsed: elapsedStr() }) + '</strong><br>' +
                t('La LLM est peut-être en boucle. Si les fichiers de correction sont déjà produits, vous pouvez annuler — le job sera récupéré automatiquement au redémarrage du service.') +
                '<div class="mt-2 d-flex gap-2">' +
                '<button class="btn btn-sm btn-warning" onclick="TranscrIA.cancelProcessing()">' + t('Annuler le traitement') + '</button>' +
                '<button class="btn btn-sm btn-outline-secondary" onclick="TranscrIA._resumePolling()">' + t('Continuer à attendre') + '</button>' +
                '</div></div>';
        }

        function poll() {
            W.api('/api/jobs/' + JOB_ID + '/status', 'GET').then(function (r) {
                if (!r || r.data.error) { setTimeout(poll, 4000); return; }
                var state = r.data.state;
                var label = _STATE_LABELS[state] ? t(_STATE_LABELS[state]) : state;
                if (_TERMINAL_STATES.indexOf(state) !== -1) {
                    if (state === 'failed') {
                        div.innerHTML = '<div class="alert alert-danger">' + t('Échec du traitement après %(elapsed)s.', { elapsed: elapsedStr() }) + '</div>';
                    } else if (state === 'cancelled') {
                        div.innerHTML = '<div class="alert alert-warning">' + t('Traitement annulé après %(elapsed)s.', { elapsed: elapsedStr() }) + '</div>';
                    } else {
                        div.innerHTML = '<div class="alert alert-success">' + t('Traitement terminé en %(elapsed)s. Chargement…', { elapsed: elapsedStr() }) + '</div>';
                        location.reload();
                    }
                } else {
                    var progress = r.data.progress || null;
                    // Afficher le warning si on est en arbitrating depuis trop longtemps
                    if (state === 'arbitrating' && !_warnShown && elapsedS() >= _LLM_WARN_S) {
                        _warnShown = true;
                        showLlmWarning();
                    } else if (!_warnShown) {
                        setInfo(t('%(label)s (%(elapsed)s écoulées)', { label: label, elapsed: elapsedStr() }), progress);
                    }
                    setTimeout(poll, 4000);
                }
            });
        }

        return { setInfo: setInfo, poll: poll };
    }

    function _ensureWorkflowStatusBanner() {
        var banner = document.getElementById('workflow-status-banner');
        if (banner) return banner;
        var currentSection = document.querySelector('.step-section.current-step');
        var progress = document.querySelector('.progress-container');
        var anchor = currentSection || progress;
        if (!anchor || !anchor.parentNode) return null;
        banner = document.createElement('div');
        banner.id = 'workflow-status-banner';
        banner.className = 'alert alert-info workflow-status-banner mb-3';
        banner.setAttribute('role', 'status');
        banner.setAttribute('aria-live', 'polite');
        if (currentSection) {
            var heading = currentSection.querySelector('h3');
            currentSection.insertBefore(banner, heading ? heading.nextSibling : currentSection.firstChild);
        } else {
            anchor.parentNode.insertBefore(banner, anchor.nextSibling);
        }
        return banner;
    }

    function _formatProgressAge(updatedAt) {
        if (!updatedAt) return '';
        var parsed = Date.parse(updatedAt);
        if (Number.isNaN(parsed)) return '';
        var seconds = Math.max(0, Math.floor((Date.now() - parsed) / 1000));
        if (seconds < 60) return t('mis à jour il y a %(n)ss', { n: seconds });
        var minutes = Math.floor(seconds / 60);
        if (minutes < 60) return t('mis à jour il y a %(n)smin', { n: minutes });
        return t('mis à jour il y a %(n)sh', { n: Math.floor(minutes / 60) });
    }

    function _renderWorkflowStatusBanner(banner, state, progress) {
        var isLive = _LIVE_STATUS_STATES.indexOf(state) !== -1;
        if (!isLive && !(progress && progress.message)) {
            banner.classList.remove('is-visible');
            banner.innerHTML = '';
            return;
        }

        var label = _STATE_LABELS[state] || state || 'Traitement en cours';
        var message = progress && progress.message ? progress.message : label;
        var phase = progress && progress.phase ? progress.phase : '';
        var pct = progress && typeof progress.percent === 'number' ? Math.max(0, Math.min(100, progress.percent)) : null;
        var age = progress ? _formatProgressAge(progress.updated_at) : '';
        var pctHtml = pct === null ? '' : '<span class="badge text-bg-light border">' + pct.toFixed(1).replace('.0', '') + '%</span>';
        var phaseHtml = phase ? '<span class="text-muted small">' + W.escapeHtml(phase) + '</span>' : '';
        var ageHtml = age ? '<span class="text-muted small">' + W.escapeHtml(age) + '</span>' : '';
        var progressHtml = pct === null ? '' :
            '<div class="progress mt-2" aria-hidden="true">' +
            '<div class="progress-bar" style="width:' + pct + '%"></div>' +
            '</div>';

        banner.classList.add('is-visible');
        banner.innerHTML =
            '<div class="d-flex align-items-center justify-content-between gap-2 flex-wrap">' +
            '<div class="d-flex align-items-center gap-2 flex-wrap">' +
            '<span class="spinner-border spinner-border-sm text-primary" role="status"></span>' +
            '<strong>' + W.escapeHtml(label) + '</strong>' +
            '<span>' + W.escapeHtml(message) + '</span>' +
            phaseHtml +
            '</div>' +
            '<div class="d-flex align-items-center gap-2">' + pctHtml + ageHtml + '</div>' +
            '</div>' +
            progressHtml;
    }

    W.initWorkflowStatusBanner = function () {
        var banner = _ensureWorkflowStatusBanner();
        if (!banner || !W.api) return;

        function poll() {
            W.api('/api/jobs/' + JOB_ID + '/status', 'GET').then(function (r) {
                if (!r || r.data.error) {
                    setTimeout(poll, 8000);
                    return;
                }
                var state = r.data.state;
                var progress = r.data.progress || null;
                _renderWorkflowStatusBanner(banner, state, progress);
                if (_LIVE_STATUS_STATES.indexOf(state) !== -1) {
                    // Phase active (résumé, traitement…) : rafraîchissement rapide.
                    setTimeout(poll, 4000);
                } else if (_TERMINAL_STATES.indexOf(state) === -1) {
                    // État intermédiaire (analyzed, summary_done, context_done…) : continuer
                    // à surveiller, sinon le démarrage d'une phase synchrone (ex. génération
                    // du résumé) ne serait jamais affiché car le polling se serait arrêté.
                    setTimeout(poll, 10000);
                }
                // État terminal (completed/export_ready/failed/cancelled) → arrêt du polling.
            });
        }

        poll();
    };

    W.cancelProcessing = function () {
        W.api('/api/jobs/' + JOB_ID + '/process', 'POST', { mode: 'cancel' }).then(function () {
            location.reload();
        });
    };

    W._resumePolling = function () {
        // L'utilisateur choisit de continuer à attendre : on masque le warning,
        // on recrée un poller et on reprend le polling.
        var div = document.getElementById('processing-result');
        var startTime = Date.now();
        var poller = _buildProcessingPoller(div, startTime);
        poller.setInfo('En attente de la fin du traitement LLM…');
        setTimeout(poller.poll, 4000);
    };

    W.startProcessing = function (mode) {
        console.log('[TranscrIA] startProcessing(' + mode + ')');
        var div = document.getElementById('processing-result');
        var startTime = Date.now();
        var poller = _buildProcessingPoller(div, startTime);

        poller.setInfo('Soumission du traitement…');
        // `mode` porte un id de profil (sélecteur) ou rien (relance d'un job échoué → fast).
        var body = mode ? { processing_profile_id: mode } : { mode: 'fast' };
        W.api('/api/jobs/' + JOB_ID + '/process', 'POST', body).then(function (r) {
            if (r.status === 409 && _REPROCESSABLE_STATES.indexOf(r.data.current_state) !== -1) {
                // Job déjà terminé — proposer de relancer
                div.innerHTML =
                    '<div class="alert alert-warning">' +
                    '<strong>' + t('Ce job a déjà été traité.') + '</strong> ' +
                    t('Voulez-vous relancer le traitement ? (le lexique et les corrections actuels seront appliqués)') +
                    '<div class="mt-2 d-flex gap-2">' +
                    '<button class="btn btn-sm btn-primary" onclick="TranscrIA.confirmReprocess(\'' + mode + '\')">' + t('Oui, relancer') + '</button>' +
                    '<button class="btn btn-sm btn-outline-secondary" onclick="document.getElementById(\'processing-result\').innerHTML=\'\'">' + t('Annuler') + '</button>' +
                    '</div></div>';
            } else if (r.data.error) {
                div.innerHTML = '<div class="alert alert-danger">' + t('Erreur : %(e)s', { e: r.data.error }) + '</div>';
            } else {
                poller.setInfo(t('Traitement démarré. Transcription ASR en cours… (0s écoulées)'));
                setTimeout(poller.poll, 4000);
            }
        });
    };

    W.confirmReprocess = function (mode) {
        console.log('[TranscrIA] confirmReprocess(' + mode + ')');
        var div = document.getElementById('processing-result');
        var startTime = Date.now();
        var poller = _buildProcessingPoller(div, startTime);

        poller.setInfo(t('Relancement du traitement…'));
        var body = mode ? { processing_profile_id: mode } : { mode: 'fast' };
        W.api('/api/jobs/' + JOB_ID + '/reprocess', 'POST', body).then(function (r) {
            if (r.data.error) {
                div.innerHTML = '<div class="alert alert-danger">' + t('Erreur : %(e)s', { e: r.data.error }) + '</div>';
            } else {
                poller.setInfo(t('Traitement relancé. Transcription ASR en cours… (0s écoulées)'));
                setTimeout(poller.poll, 4000);
            }
        });
    };

    // ── Sélecteur de profil de traitement (Phase 6) ─────────────────────────
    // Les données viennent du backend (source unique : /api/profiles/availability,
    // injectées dans #profiles-data). Le JS ne fait que rendre et sélectionner.
    W._profilesData = null;
    W._selectedProfile = null;

    function _profilesData() {
        if (W._profilesData === null) {
            var el = document.getElementById('profiles-data');
            try { W._profilesData = el ? JSON.parse(el.textContent) : { profiles: [] }; }
            catch (e) { W._profilesData = { profiles: [] }; }
        }
        return W._profilesData;
    }

    function _esc(s) {
        var d = document.createElement('div');
        d.textContent = s == null ? '' : String(s);
        return d.innerHTML;
    }

    function _renderProfileDetail(p) {
        var chips = function (items, cls) {
            if (!items || !items.length) { return '<span class="text-muted">—</span>'; }
            return items.map(function (i) {
                return '<span class="badge rounded-pill bg-' + cls + ' me-1 mb-1">' + _esc(i) + '</span>';
            }).join('');
        };
        return '' +
            '<div class="card-body">' +
            '<h5 class="card-title mb-1">' + _esc(p.label) + '</h5>' +
            '<p class="text-muted mb-3">' + _esc(p.description) + '</p>' +
            '<div class="row g-3">' +
            '<div class="col-md-6"><div class="small text-uppercase text-muted mb-1">' +
            '<i class="bi bi-box-seam"></i> Produit</div>' + chips(p.deliverables, 'success') + '</div>' +
            '<div class="col-md-6"><div class="small text-uppercase text-muted mb-1">' +
            '<i class="bi bi-check2-square"></i> À valider</div>' + chips(p.validations, 'primary') + '</div>' +
            '</div>' +
            (p.available ? '' :
                '<div class="alert alert-warning mt-3 mb-0 py-2 small">' +
                '<i class="bi bi-exclamation-triangle"></i> ' + _esc((p.reasons || []).join(' · ')) + '</div>') +
            '</div>';
    }

    W.selectProfile = function (profileId) {
        var data = _profilesData();
        var p = (data.profiles || []).filter(function (x) { return x.id === profileId; })[0];
        if (!p || !p.available) { return; }
        W._selectedProfile = profileId;
        // Pastilles : surligner la sélection.
        var pills = document.querySelectorAll('#profile-selector .profile-pill');
        Array.prototype.forEach.call(pills, function (btn) {
            var active = btn.dataset.profileId === profileId;
            btn.classList.toggle('btn-success', active);
            btn.classList.toggle('active', active);
            if (!btn.disabled) { btn.classList.toggle('btn-outline-secondary', !active); }
        });
        var detail = document.getElementById('profile-detail');
        if (detail) { detail.innerHTML = _renderProfileDetail(p); }
        var btn = document.getElementById('profile-launch-btn');
        if (btn) { btn.disabled = false; }
    };

    W.startSelectedProfile = function () {
        if (!W._selectedProfile) { return; }
        W.startProcessing(W._selectedProfile);
    };

    // Choix du profil à l'étape 1 : on PERSISTE le choix puis on recharge, pour que le serveur
    // (source unique des règles) recalcule les étapes du wizard adaptées au profil. Aucune
    // logique de masquage d'étapes n'est dupliquée côté client.
    W.chooseProfile = function (profileId) {
        var data = _profilesData();
        var p = (data.profiles || []).filter(function (x) { return x.id === profileId; })[0];
        if (!p || !p.available) { return; }
        if (profileId === document.getElementById('profile-selector').dataset.selected) {
            return;  // déjà sélectionné : rien à refaire
        }
        W.selectProfile(profileId);  // retour visuel immédiat avant le rechargement
        W.api('/api/jobs/' + JOB_ID + '/profile', 'POST', { processing_profile_id: profileId })
            .then(function (r) {
                if (r.data && r.data.error) {
                    var detail = document.getElementById('profile-detail');
                    if (detail) {
                        detail.innerHTML = '<div class="card-body"><div class="alert alert-danger mb-0">' +
                            _esc(r.data.error) + '</div></div>';
                    }
                    return;
                }
                window.location.reload();
            });
    };

    W.initProfileSelector = function () {
        var selector = document.getElementById('profile-selector');
        if (!selector) { return; }
        var selected = selector.dataset.selected;
        if (selected) { W.selectProfile(selected); }
    };


})();

window.TranscrIA = TranscrIA;
TranscrIA.initWorkflowStatusBanner();
TranscrIA.initProfileSelector();
console.log('[TranscrIA] wizard.js loaded, functions: ' + Object.keys(TranscrIA).join(', '));
