// Étape 6 du wizard — aperçu du sens de chaque remplacement, échange terme/variante,
// avertissements (2026-11-12). Chargé APRÈS wizard.js (qui rend les fiches et appelle
// refreshLexiconRowHints si présent). Zéro requête : la référence est la transcription rapide
// embarquée par la page (window.__WIZARD_LEX_REFERENCE__).
(function () {
    'use strict';
    var W = window.TranscrIA;
    if (!W) return;
    var t = window.t || function (s) { return s; };

    // ── Étape 6 : sens du remplacement, échange terme/variante, avertissements (2026-11-12) ──
    // Vécu sur un job réel : 11 fiches « mot suspect » arrivent à l'envers (la faute dans « Forme
        // validée », aucune variante) et se retournent à la main ; deux ont été retournées de travers
    // (variante = la bonne forme ; « Cloud », mot courant, en variante d'un bout de phrase) et le
    // filet déterministe les a appliquées. L'aperçu montre le SENS de chaque remplacement avec le
    // compte d'occurrences dans la transcription rapide ; le bouton fait l'échange sans ressaisie ;
    // les avertissements reprennent les règles du filet (context/lexicon.sans_variantes_douteuses)
    // pour que l'écran dise ce que le code fera. Jamais bloquant.
    var LEX_FREQUENT_VARIANT = 5;
    var LEX_PHRASE_PUNCT = /[,;:!?]/;

    W.lexiconReferenceText = function () {
        return String(window.__WIZARD_LEX_REFERENCE__ || '');
    };

    // Mot entier, casse ignorée, lettres Unicode (même règle que le filet Python : \w y est Unicode).
    W.countWholeWord = function (text, needle) {
        needle = String(needle || '').trim();
        if (!needle || !text) return 0;
        var esc = needle.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        var re = new RegExp('(?<![\\p{L}\\p{N}_])' + esc + '(?![\\p{L}\\p{N}_])', 'giu');
        var found = text.match(re);
        return found ? found.length : 0;
    };

    W.lexiconRowVariants = function (row) {
        return W.getRowValue(row, '.lex-variants').split(/[;,]/).map(function (v) { return v.trim(); }).filter(Boolean);
    };

    W.lexiconRowForm = function (row) {
        return (W.getRowValue(row, '.lex-replace') || W.getRowValue(row, '.lex-term')).trim();
    };

    W.refreshLexiconHintsFrom = function (input) {
        var row = input && input.closest ? input.closest('.lexicon-row') : null;
        if (row) W.refreshLexiconRowHints(row);
    };

    W.refreshAllLexiconHints = function () {
        document.querySelectorAll('#lexicon-list .lexicon-row').forEach(function (row) { W.refreshLexiconRowHints(row); });
    };

    W.refreshLexiconRowHints = function (row) {
        var zone = row.querySelector('[data-lex-hints]');
        if (!zone) return;
        var ref = W.lexiconReferenceText();
        var form = W.lexiconRowForm(row);
        var variants = W.lexiconRowVariants(row);
        var otherForms = {};
        document.querySelectorAll('#lexicon-list .lexicon-row').forEach(function (other) {
                if (other === row) return;
                var f = W.lexiconRowForm(other).toLowerCase();
                if (f) otherForms[f] = W.getRowValue(other, '.lex-term').trim() || f;
            });
        var hints = [];
        var warn = false;
        function add(text, isWarn) {
            hints.push('<span class="lex-hint' + (isWarn ? ' lex-hint-warn' : '') + '">' + text + '</span>');
            if (isWarn) warn = true;
        }
        if (form && LEX_PHRASE_PUNCT.test(form)) {
            add(t('La forme validée ressemble à une phrase : elle ne remplacera rien.'), true);
        }
        var formCount = (ref && form) ? W.countWholeWord(ref, form) : 0;
        variants.forEach(function (v) {
                if (!form || v.toLowerCase() === form.toLowerCase()) return;
                var ev = W.escapeHtml(v), ef = W.escapeHtml(form);
                if (otherForms[v.toLowerCase()]) {
                    add(t("« %(v)s » est la forme validée d'une autre ligne : cette variante ne sera pas appliquée.", { v: ev }), true);
                    return;
                }
                if (!ref) return;
                var n = W.countWholeWord(ref, v);
                add(t('« %(v)s » → « %(f)s » : %(n)s occurrence(s) dans la transcription rapide', { v: ev, f: ef, n: n }), false);
                if (n === 0 && formCount > 0) {
                    add(t("aucune occurrence de cette variante, mais %(n)s de la forme validée — la ligne est-elle à l'envers ?", { n: formCount }), true);
                } else if (n >= LEX_FREQUENT_VARIANT) {
                    add(t("« %(v)s » apparaît %(n)s fois : vérifiez qu'il s'agit bien d'une faute et non d'un mot courant.", { v: ev, n: n }), true);
                }
            });
        zone.innerHTML = hints.join('');
        row.classList.toggle('lex-row-warn', warn);
        // Fiche « mot suspect » sans variante : c'est le cas attendu de l'échange, on le met en avant.
        row.classList.toggle('lex-row-swap-first', !!form && variants.length === 0);
    };

    W.swapLexiconRow = function (btn) {
        var row = btn && btn.closest ? btn.closest('.lexicon-row') : null;
        if (!row) return;
        var termEl = row.querySelector('.lex-term');
        var term = (termEl.value || '').trim();
        if (!term) { termEl.focus(); return; }
        var variants = W.lexiconRowVariants(row);
        if (!variants.some(function (v) { return v.toLowerCase() === term.toLowerCase(); })) variants.push(term);
        row.querySelector('.lex-variants').value = variants.join(', ');
        termEl.value = '';
        W.refreshLexiconRowHints(row);
        termEl.focus();
    };

    W.refreshAllLexiconHints();
})();
