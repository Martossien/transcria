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
        // Session expirée/invalide : le serveur répond 401 JSON sur les routes /api/
        // (et certains proxys peuvent encore rediriger vers /login). Dans les deux cas,
        // on renvoie l'utilisateur se connecter au lieu d'afficher « Réponse serveur
        // invalide » et de repartir en boucle de polls non authentifiés.
        if (r.status === 401 || (r.redirected && r.url.indexOf('/login') !== -1)) {
            console.warn('[TranscrIA] session expirée — redirection vers /login');
            window.location.href = '/login?next=' + encodeURIComponent(window.location.pathname);
            return { status: 401, data: { error: t('Session expirée — redirection vers la connexion…') } };
        }
        return r.text().then(function (text) {
            var data = {};
            if (text) {
                try {
                    data = JSON.parse(text);
                } catch (e) {
                    data = { error: r.ok ? t('Réponse serveur invalide.') : t('Erreur serveur non JSON.') };
                }
            }
            if (!r.ok && !data.error) {
                data.error = t('Erreur serveur (%(status)s).', { status: r.status });
            }
            return { status: r.status, data: data };
        });
    }).catch(function (err) {
        console.error('[TranscrIA] api error:', err);
        return { status: 0, data: { error: t('Erreur réseau : %(msg)s', { msg: (err && err.message ? err.message : t('requête impossible')) }) } };
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
