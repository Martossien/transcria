/* Micro direct (record-then-transcribe) — TranscrIA.
 *
 * Enregistre l'audio du micro (getUserMedia + MediaRecorder), propose une écoute,
 * puis dépose le blob par le MÊME endpoint que l'upload fichier
 * (POST /api/jobs/<id>/upload, champ file + source=mic). Le markup et les libellés
 * vivent dans wizard/_step_file.html (i18n template) ; ici, uniquement le comportement.
 *
 * Contraintes : pas de logique inline (CSP stricte → data-action) ; jamais de
 * console.error sur un chemin attendu (le walkthrough CI échoue sur console.error) —
 * un refus de micro est une action utilisateur, pas une erreur.
 */
var TranscrIA = window.TranscrIA || {};

(function () {
    var W = TranscrIA;
    var root = document.getElementById('wizard-root');
    if (!root) { return; }
    var JOB_ID = window.__JOB_ID__ || root.dataset.jobId;
    if (!JOB_ID) { return; }

    // État de session d'enregistrement (remis à zéro à chaque démarrage).
    var stream = null;
    var recorder = null;
    var chunks = [];
    var blob = null;
    var timerId = null;
    var seconds = 0;

    function el(id) { return document.getElementById(id); }
    function show(id) { var e = el(id); if (e) { e.classList.remove('d-none'); } }
    function hide(id) { var e = el(id); if (e) { e.classList.add('d-none'); } }
    function setResult(html) { var e = el('mic-result'); if (e) { e.innerHTML = html; } }
    function danger(msg) { setResult('<div class="alert alert-danger py-2 mb-0">' + msg + '</div>'); }

    // MediaRecorder : préférer webm/opus (Chrome), retomber sur ogg/opus (Firefox).
    function pickMime() {
        var prefs = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus'];
        if (window.MediaRecorder && MediaRecorder.isTypeSupported) {
            for (var i = 0; i < prefs.length; i++) {
                if (MediaRecorder.isTypeSupported(prefs[i])) { return prefs[i]; }
            }
        }
        return '';
    }

    function extFor(mime) { return (mime && mime.indexOf('ogg') !== -1) ? '.ogg' : '.webm'; }

    function stopTracks() {
        if (stream) { stream.getTracks().forEach(function (t) { t.stop(); }); stream = null; }
    }

    function tick() {
        seconds += 1;
        var t = el('mic-timer');
        if (t) { t.textContent = String(seconds); }
    }

    W.startMicRecording = function () {
        setResult('');
        hide('mic-preview');
        blob = null;
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.MediaRecorder) {
            danger(t("Votre navigateur ne permet pas l'enregistrement audio."));
            return;
        }
        navigator.mediaDevices.getUserMedia({ audio: true }).then(function (s) {
            stream = s;
            var mime = pickMime();
            recorder = mime ? new MediaRecorder(s, { mimeType: mime }) : new MediaRecorder(s);
            chunks = [];
            recorder.ondataavailable = function (e) { if (e.data && e.data.size > 0) { chunks.push(e.data); } };
            recorder.onstop = function () {
                stopTracks();
                if (timerId) { clearInterval(timerId); timerId = null; }
                blob = new Blob(chunks, { type: recorder.mimeType || 'audio/webm' });
                var audio = el('mic-audio');
                if (audio) { audio.src = URL.createObjectURL(blob); }
                hide('mic-recording');
                show('mic-preview');
            };
            seconds = 0;
            var timer = el('mic-timer');
            if (timer) { timer.textContent = '0'; }
            recorder.start();
            timerId = setInterval(tick, 1000);
            show('mic-recording');
        }).catch(function () {
            // Refus de permission ou aucun périphérique : message utilisateur, pas une erreur.
            danger(t('Micro inaccessible : autorisez le microphone puis réessayez.'));
        });
    };

    W.stopMicRecording = function () {
        if (recorder && recorder.state !== 'inactive') { recorder.stop(); }
    };

    W.uploadMicRecording = function () {
        if (!blob) { return; }
        W.showSpinner('mic-upload-spinner');
        var mime = (recorder && recorder.mimeType) || 'audio/webm';
        var fd = new FormData();
        fd.append('file', blob, 'micro-' + Date.now() + extFor(mime));
        fd.append('source', 'mic');
        fetch('/api/jobs/' + JOB_ID + '/upload', { method: 'POST', body: fd })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                W.hideSpinner('mic-upload-spinner');
                if (data.error) {
                    danger(t('Erreur :') + ' ' + W.escapeHtml(data.error));
                } else {
                    setResult('<div class="alert alert-success py-2 mb-0">' + t('Fichier téléversé. Rechargement…') + '</div>');
                    W.reloadAfter(1000);
                }
            })
            .catch(function (err) {
                W.hideSpinner('mic-upload-spinner');
                danger(t('Erreur réseau : %(msg)s', { msg: W.escapeHtml(err && err.message) }));
            });
    };
})();
