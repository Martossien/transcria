var TranscrIA = window.TranscrIA || {};

TranscrIA.api = function (endpoint, method, body) {
    var opts = { method: method || 'POST', headers: {} };
    if (body instanceof FormData) {
        opts.body = body;
    } else if (body !== null && body !== undefined) {
        opts.headers['Content-Type'] = 'application/json';
        opts.body = JSON.stringify(body);
    }
    console.log('[TranscrIA] api ' + method + ' ' + endpoint);
    return fetch(endpoint, opts).then(function (r) {
        return r.json().then(function (d) { return { status: r.status, data: d }; });
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
