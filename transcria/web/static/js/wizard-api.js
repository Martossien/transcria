var TranscrIA = window.TranscrIA || {};

TranscrIA.api = function (endpoint, method, body) {
    var resolvedMethod = method || 'POST';
    var opts = { method: resolvedMethod, headers: {} };
    if (body instanceof FormData) {
        opts.body = body;
    } else if (body !== null && body !== undefined) {
        opts.headers['Content-Type'] = 'application/json';
        opts.body = JSON.stringify(body);
    }
    console.log('[TranscrIA] api ' + resolvedMethod + ' ' + endpoint);
    return fetch(endpoint, opts).then(function (r) {
        return r.text().then(function (text) {
            var data = {};
            if (text) {
                try {
                    data = JSON.parse(text);
                } catch (e) {
                    data = { error: r.ok ? 'Réponse serveur invalide.' : 'Erreur serveur non JSON.' };
                }
            }
            if (!r.ok && !data.error) {
                data.error = 'Erreur serveur (' + r.status + ').';
            }
            return { status: r.status, data: data };
        });
    }).catch(function (err) {
        console.error('[TranscrIA] api error:', err);
        return { status: 0, data: { error: 'Erreur réseau: ' + (err && err.message ? err.message : 'requête impossible') } };
    });
};

TranscrIA.showSpinner = function (id) {
    var el = document.getElementById(id);
    if (el) el.classList.remove('d-none');
    console.log('[TranscrIA] showSpinner ' + id);
};

TranscrIA.hideSpinner = function (id) {
    var el = document.getElementById(id);
    if (el) el.classList.add('d-none');
};

TranscrIA.reloadAfter = function (ms) {
    setTimeout(function () { location.reload(); }, ms || 500);
};

window.TranscrIA = TranscrIA;
console.log('[TranscrIA] wizard-api.js loaded');
