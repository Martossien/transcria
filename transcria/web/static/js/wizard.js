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
        W.api('/api/jobs/' + JOB_ID + '/summary').then(function (r) {
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
        W.api('/api/jobs/' + JOB_ID + '/context', 'POST', data).then(function (r) {
            if (r.status === 200) {
                document.getElementById('context-saved').classList.remove('d-none');
                W.reloadAfter(500);
            }
        });
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
            var name = (nameEl && nameEl.value || '').trim();
            var func = (funcEl && funcEl.value || '').trim();
            var role = (roleEl && roleEl.value || '').trim();
            if (name) {
                pi++;
                var pid = 'p' + pi;
                participants.push({ id: pid, name: name, function: func, role: role,
                    is_animator: false, expected: true });
                var spkLabel = row.querySelector('strong');
                if (spkLabel) {
                    mapping[spkLabel.textContent] = { name: name, participant_id: pid,
                        function: func, role: role };
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
            .then(function (r) { return r.json(); })
            .then(function (data) {
                var clips = (data.clips || {})[speakerId] || [];
                if (clips.length === 0) {
                    audioDiv.innerHTML = '<small class="text-muted">Aucun extrait disponible</small>';
                    return;
                }
                var html = '';
                clips.forEach(function (path, i) {
                    var fname = path.split('/').pop();
                    html += '<small class="text-muted">Extrait ' + (i+1) + ' :</small>' +
                        '<audio controls preload="none">' +
                        '<source src="/api/jobs/' + JOB_ID + '/speakers/clip/' + fname +
                        '" type="audio/wav"></audio>';
                });
                audioDiv.innerHTML = html;
            })
            .catch(function () {
                audioDiv.innerHTML = '<small class="text-danger">Erreur chargement</small>';
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

    W.addLexiconTerm = function () {
        console.log('[TranscrIA] addLexiconTerm()');
        var container = document.getElementById('lexicon-list');
        var row = document.createElement('div');
        row.className = 'lexicon-row';
        row.innerHTML = '<input type="text" class="form-control form-control-sm lex-term" placeholder="Terme" style="max-width:150px;">' +
            '<select class="form-select form-select-sm lex-cat" style="max-width:120px;">' +
            (document.getElementById('lexicon-cat-tpl') ? document.getElementById('lexicon-cat-tpl').innerHTML : '') +
            '</select>' +
            '<select class="form-select form-select-sm lex-prio" style="max-width:110px;">' +
            (document.getElementById('lexicon-prio-tpl') ? document.getElementById('lexicon-prio-tpl').innerHTML : '') +
            '</select>' +
            '<input type="text" class="form-control form-control-sm lex-replace" placeholder="Remplacer par" style="max-width:150px;">' +
            '<button type="button" class="btn btn-sm btn-outline-danger" onclick="this.parentElement.remove()">×</button>';
        container.appendChild(row);
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
            var termEl = row.querySelector('.lex-term');
            var catEl = row.querySelector('.lex-cat');
            var prioEl = row.querySelector('.lex-prio');
            var replEl = row.querySelector('.lex-replace');
            data.push({
                term: (termEl && termEl.value || ''),
                category: (catEl && catEl.value || 'autre'),
                priority: (prioEl && prioEl.value || 'normale'),
                replace_by: (replEl && replEl.value || ''),
                variants: []
            });
        });
        W.api('/api/jobs/' + JOB_ID + '/lexicon', 'POST', data).then(function (r) {
            if (r.status === 200) {
                document.getElementById('lexicon-saved').classList.remove('d-none');
                W.reloadAfter(500);
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

    W.startProcessing = function (mode) {
        console.log('[TranscrIA] startProcessing(' + mode + ')');
        var div = document.getElementById('processing-result');
        var startTime = Date.now();

        function elapsed() {
            var s = Math.floor((Date.now() - startTime) / 1000);
            return s < 60 ? s + 's' : Math.floor(s / 60) + 'min ' + (s % 60) + 's';
        }

        function setInfo(msg) {
            div.innerHTML = '<div class="alert alert-info d-flex align-items-center gap-2">' +
                '<span class="spinner-border spinner-border-sm" role="status"></span>' +
                '<span>' + msg + '</span></div>';
        }

        function pollStatus() {
            W.api('/api/jobs/' + JOB_ID + '/status', 'GET').then(function (r) {
                if (!r || r.data.error) {
                    setTimeout(pollStatus, 4000);
                    return;
                }
                var state = r.data.state;
                var label = _STATE_LABELS[state] || state;
                if (_TERMINAL_STATES.indexOf(state) !== -1) {
                    if (state === 'failed') {
                        div.innerHTML = '<div class="alert alert-danger">Échec du traitement après ' + elapsed() + '.</div>';
                    } else if (state === 'cancelled') {
                        div.innerHTML = '<div class="alert alert-warning">Traitement annulé.</div>';
                    } else {
                        div.innerHTML = '<div class="alert alert-success">Traitement terminé en ' + elapsed() + '. Chargement…</div>';
                        location.reload();
                    }
                } else {
                    setInfo(label + ' (' + elapsed() + ' écoulées)');
                    setTimeout(pollStatus, 4000);
                }
            });
        }

        setInfo('Soumission du traitement…');
        W.api('/api/jobs/' + JOB_ID + '/process', 'POST', { mode: mode }).then(function (r) {
            if (r.data.error) {
                div.innerHTML = '<div class="alert alert-danger">Erreur : ' + r.data.error + '</div>';
            } else {
                setInfo('Traitement démarré. Transcription ASR en cours… (0s écoulées)');
                setTimeout(pollStatus, 4000);
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
