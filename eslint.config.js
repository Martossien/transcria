/* Lint du JavaScript servi par Flask.
 *
 * PÉRIMÈTRE VOLONTAIREMENT ÉTROIT. Ces fichiers sont des scripts CLASSIQUES chargés par des
 * gabarits Jinja — pas de modules, pas de bundler, pas d'étape de build. On vérifie donc ce
 * qui casse vraiment : variable non définie, retour manquant, `case` qui déborde. Pas de
 * style : le projet n'a pas de formateur JS et en imposer un maintenant produirait un diff
 * de 4 000 lignes sans rapport avec la qualité.
 */
import globals from "globals";

export default [
  {
    files: ["transcria/web/static/js/**/*.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "script",
      globals: {
        ...globals.browser,
        // Objets posés par un script chargé PLUS TÔT dans le gabarit : ce ne sont pas des
        // variables non définies, c'est le mode de composition de ce front.
        TranscrIA: "readonly",
        TranscrIATime: "readonly",
        bootstrap: "readonly",
        t: "readonly",
      },
    },
    rules: {
      "no-undef": "error",
      "no-unused-vars": ["error", { args: "none", varsIgnorePattern: "^_" }],
      "no-fallthrough": "error",
      "no-dupe-keys": "error",
      "no-dupe-args": "error",
      "no-unreachable": "error",
      "no-const-assign": "error",
      "no-self-compare": "error",
      "valid-typeof": "error",
      eqeqeq: ["warn", "smart"],
    },
  },
];
