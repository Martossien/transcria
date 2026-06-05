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
                        '<div class="alert alert-danger">Erreur: ' + data.error + '</div>';
                } else {
                    document.getElementById('upload-result').innerHTML =
                        '<div class="alert alert-success">Fichier téléversé. Rechargement...</div>';
                    W.reloadAfter(1000);
                }
            })
            .catch(function (err) {
                W.hideSpinner('upload-spinner');
                console.error('[TranscrIA] uploadFile error:', err);
                document.getElementById('upload-result').innerHTML =
                    '<div class="alert alert-danger">Erreur réseau: ' + err.message + '</div>';
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
        // Mémoriser la fourchette de locuteurs avant la diarisation (phase résumé).
        W.api('/api/jobs/' + JOB_ID + '/speaker-hint', 'POST', hint).then(function () {
            return W.api('/api/jobs/' + JOB_ID + '/summary');
        }).then(function (r) {
            W.hideSpinner('summary-spinner');
            if (r.data.error) {
                document.getElementById('summary-result').innerHTML =
                    '<div class="alert alert-danger">' + r.data.error + '</div>';
            } else {
                location.reload();
            }
        });
    };

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
        if (title) { title.textContent = titles[meetingType] || 'Informations complémentaires'; }

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
        row.innerHTML = '<span class="text-muted small">nouveau</span>' +
            '<input type="text" class="form-control form-control-sm speaker-name" placeholder="Nom" style="max-width:150px;">' +
            '<input type="text" class="form-control form-control-sm speaker-func" placeholder="Fonction" style="max-width:130px;">' +
            '<input type="text" class="form-control form-control-sm speaker-role" placeholder="Rôle dans la réunion" style="max-width:150px;">' +
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
            counter.textContent = ' · ' + listened + '/' + inputs.length + ' écoutés';
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
            '<summary class="small text-muted">Contexte proposé (' + contexts.length + ')' +
            '<span class="lex-context-counter"> · 0/' + contexts.length + ' écoutés</span></summary>' +
            '<div class="small mt-2">';
        contexts.forEach(function (c) {
            var meta = (c.timecode || 'sans timecode') + (c.speaker ? ' — ' + c.speaker : '');
            html += '<div class="lex-context-item">' +
                '<span class="text-muted"></span>' +
                '<div class="lex-context-quote"></div>' +
                (c.reason ? '<div class="text-muted lex-context-reason"></div>' : '') +
                '<div class="lex-context-actions">' +
                '<button type="button" class="btn btn-sm btn-outline-primary lex-context-play" title="Écouter 5 secondes avant et après" onclick="TranscrIA.toggleLexiconContextAudio(this)"><i class="bi bi-play-fill"></i></button>' +
                '<label class="lex-context-listened"><input type="checkbox" class="form-check-input lex-context-listened-input" onchange="TranscrIA.setLexiconContextListened(this)"> Écouté</label>' +
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
            var meta = (c.timecode || 'sans timecode') + (c.speaker ? ' — ' + c.speaker : '');
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
        var t = term || {};
        row.className = 'lexicon-row lexicon-card';
        row.innerHTML = '<div class="lexicon-grid">' +
            '<label class="form-label small mb-1">Forme validée</label>' +
            '<label class="form-label small mb-1">Formes suspectes observées</label>' +
            '<label class="form-label small mb-1">Catégorie</label>' +
            '<label class="form-label small mb-1">Priorité</label>' +
            '<input type="text" class="form-control form-control-sm lex-term" placeholder="Ex: Forme validée" value="" data-field="term">' +
            '<input type="text" class="form-control form-control-sm lex-variants" placeholder="Ex: Forme douteuse A, forme douteuse B" value="" data-field="variants">' +
            '<input type="text" class="form-control form-control-sm lex-cat" list="lexicon-cat-list" placeholder="Catégorie libre" value="" data-field="category">' +
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
            '<textarea class="form-control form-control-sm lex-comment mt-2" rows="2" placeholder="Pourquoi cette forme doit être validée"></textarea>' +
            W.renderLexiconContexts(t.contexts || []) +
            '<button type="button" class="btn btn-sm btn-outline-danger lex-remove" onclick="this.parentElement.remove()">×</button>';
        container.appendChild(row);

        row.querySelector('.lex-term').value = t.term || '';
        row.querySelector('.lex-variants').value = W.formatLexiconVariants(t.variants);
        row.querySelector('.lex-cat').value = t.category || '';
        row.querySelector('.lex-prio').value = t.priority || 'normale';
        row.querySelector('.lex-replace').value = t.replace_by || '';
        row.querySelector('.lex-source').value = t.source || '';
        row.querySelector('.lex-central-entry-id').value = t.central_entry_id || '';
        row.querySelector('.lex-central-lexicon-id').value = t.central_lexicon_id || '';
        row.querySelector('.lex-central-lexicon-name').value = t.central_lexicon_name || '';
        row.querySelector('.lex-display-reason').value = t._display_reason || '';
        var centralBadge = row.querySelector('.lex-central-badge');
        if (centralBadge && t.central_lexicon_name) {
            centralBadge.classList.remove('d-none');
            centralBadge.innerHTML = '<span class="badge text-bg-light border">Lexique central : ' + W.escapeHtml(t.central_lexicon_name) + '</span>';
        }
        row.querySelector('.lex-comment').value = t.comment || '';
        row.querySelector('.lex-contexts-json').textContent = JSON.stringify(t.contexts || []);
        W.fillLexiconContexts(row, t.contexts || []);
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
                '<strong>⚠ Le traitement LLM prend plus de temps que prévu (' + elapsedStr() + ').</strong><br>' +
                'La LLM est peut-être en boucle. Si les fichiers de correction sont déjà produits, ' +
                'vous pouvez annuler — le job sera récupéré automatiquement au redémarrage du service.' +
                '<div class="mt-2 d-flex gap-2">' +
                '<button class="btn btn-sm btn-warning" onclick="TranscrIA.cancelProcessing()">Annuler le traitement</button>' +
                '<button class="btn btn-sm btn-outline-secondary" onclick="TranscrIA._resumePolling()">Continuer à attendre</button>' +
                '</div></div>';
        }

        function poll() {
            W.api('/api/jobs/' + JOB_ID + '/status', 'GET').then(function (r) {
                if (!r || r.data.error) { setTimeout(poll, 4000); return; }
                var state = r.data.state;
                var label = _STATE_LABELS[state] || state;
                if (_TERMINAL_STATES.indexOf(state) !== -1) {
                    if (state === 'failed') {
                        div.innerHTML = '<div class="alert alert-danger">Échec du traitement après ' + elapsedStr() + '.</div>';
                    } else if (state === 'cancelled') {
                        div.innerHTML = '<div class="alert alert-warning">Traitement annulé après ' + elapsedStr() + '.</div>';
                    } else {
                        div.innerHTML = '<div class="alert alert-success">Traitement terminé en ' + elapsedStr() + '. Chargement…</div>';
                        location.reload();
                    }
                } else {
                    var progress = r.data.progress || null;
                    // Afficher le warning si on est en arbitrating depuis trop longtemps
                    if (state === 'arbitrating' && !_warnShown && elapsedS() >= _LLM_WARN_S) {
                        _warnShown = true;
                        showLlmWarning();
                    } else if (!_warnShown) {
                        setInfo(label + ' (' + elapsedStr() + ' écoulées)', progress);
                    }
                    setTimeout(poll, 4000);
                }
            });
        }

        return { setInfo: setInfo, poll: poll };
    }

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
        W.api('/api/jobs/' + JOB_ID + '/process', 'POST', { mode: mode }).then(function (r) {
            if (r.status === 409 && _REPROCESSABLE_STATES.indexOf(r.data.current_state) !== -1) {
                // Job déjà terminé — proposer de relancer
                div.innerHTML =
                    '<div class="alert alert-warning">' +
                    '<strong>Ce job a déjà été traité.</strong> ' +
                    'Voulez-vous relancer le traitement ? (le lexique et les corrections actuels seront appliqués)' +
                    '<div class="mt-2 d-flex gap-2">' +
                    '<button class="btn btn-sm btn-primary" onclick="TranscrIA.confirmReprocess(\'' + mode + '\')">Oui, relancer</button>' +
                    '<button class="btn btn-sm btn-outline-secondary" onclick="document.getElementById(\'processing-result\').innerHTML=\'\'">Annuler</button>' +
                    '</div></div>';
            } else if (r.data.error) {
                div.innerHTML = '<div class="alert alert-danger">Erreur : ' + r.data.error + '</div>';
            } else {
                poller.setInfo('Traitement démarré. Transcription ASR en cours… (0s écoulées)');
                setTimeout(poller.poll, 4000);
            }
        });
    };

    W.confirmReprocess = function (mode) {
        console.log('[TranscrIA] confirmReprocess(' + mode + ')');
        var div = document.getElementById('processing-result');
        var startTime = Date.now();
        var poller = _buildProcessingPoller(div, startTime);

        poller.setInfo('Relancement du traitement…');
        W.api('/api/jobs/' + JOB_ID + '/reprocess', 'POST', { mode: mode || 'fast' }).then(function (r) {
            if (r.data.error) {
                div.innerHTML = '<div class="alert alert-danger">Erreur : ' + r.data.error + '</div>';
            } else {
                poller.setInfo('Traitement relancé. Transcription ASR en cours… (0s écoulées)');
                setTimeout(poller.poll, 4000);
            }
        });
    };

    W.pushToEditor = function () {
        console.log('[TranscrIA] pushToEditor()');
        W.showSpinner('push-spinner');
        W.api('/api/jobs/' + JOB_ID + '/push-to-editor').then(function (r) {
            W.hideSpinner('push-spinner');
            var ediv = document.getElementById('export-result');
            if (r.data.error) {
                ediv.innerHTML = '<div class="alert alert-danger">' + r.data.error + '</div>';
            } else {
                var url = r.data.editor_url || document.getElementById('wizard-root').dataset.editorUrl || '';
                ediv.innerHTML = '<div class="alert alert-success">Fichiers envoyés. ' +
                    '<a href="' + url + '" target="_blank">Ouvrir SRT Editor</a></div>';
            }
        });
    };

})();

window.TranscrIA = TranscrIA;
console.log('[TranscrIA] wizard.js loaded, functions: ' + Object.keys(TranscrIA).join(', '));
