/* Minutages SRT — fonctions PURES, extraites de srt_editor.js pour être testables.
 *
 * POURQUOI CETTE EXTRACTION. L'éditeur SRT fait 1 300 lignes et n'avait aucun test propre :
 * les tests Python vérifient que la page s'affiche et que les routes répondent, pas que
 * `01:02:03,450` se relit correctement. Or un défaut ici ne casse rien de visible — il
 * DÉCALE des sous-titres, ce qui se découvre à la lecture du livrable, longtemps après.
 *
 * L'extraction est volontairement minuscule : quatre fonctions sans état, sans DOM, sans
 * réseau. On couvre ce qui casse ; on ne redessine pas le fichier.
 *
 * Chargé comme un script classique (pas un module) : il pose son objet sur `globalThis`,
 * ce que l'éditeur consomme et ce que les tests importent.
 */
(function () {
  "use strict";

  /** Échappe le HTML — la seule barrière entre un texte de transcription et le DOM. */
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));

  /** Millisecondes → `HH:MM:SS`. Les négatifs sont ramenés à zéro : un minutage ne recule pas. */
  const fmt = (ms) => {
    ms = Math.max(0, Math.round(ms));
    const h = Math.floor(ms / 3600000), m = Math.floor(ms / 60000) % 60,
          s = Math.floor(ms / 1000) % 60;
    return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  };

  /** Millisecondes → `HH:MM:SS,mmm` (forme SRT). */
  const fmtMs = (ms) => `${fmt(ms)},${String(Math.round(Math.max(0, ms)) % 1000).padStart(3, "0")}`;

  /**
   * `[HH:]MM:SS[,mmm]` → millisecondes, ou `null` si la forme n'est pas reconnue.
   *
   * `null` et non `0` : un `0` silencieux placerait le sous-titre au début du fichier au
   * lieu de signaler la saisie fautive.
   *
   * Les millisecondes sont complétées à DROITE (`,5` vaut 500 ms, pas 5) — c'est la
   * convention SRT, et l'inverse décalerait d'une demi-seconde.
   */
  const parseTs = (v) => {
    const m = String(v).trim().match(/^(?:(\d+):)?(\d{1,2}):(\d{1,2})(?:[,.](\d{1,3}))?$/);
    if (!m) return null;
    return ((+(m[1] || 0) * 60 + +m[2]) * 60 + +m[3]) * 1000 + +String(m[4] || "0").padEnd(3, "0");
  };

  globalThis.TranscrIATime = { esc, fmt, fmtMs, parseTs };
})();
