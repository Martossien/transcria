# Types de réunion communautaires

Ce répertoire rassemble des **types de réunion** partagés par la communauté TranscrIA.
Un type définit l'apparence et les champs du compte-rendu Word (bannière, palette,
badge, champs de saisie, indices de suggestion, extractions supplémentaires) — voir
`docs/TYPES_REUNION_PERSONNALISES.md`.

## Utiliser un type

Dans TranscrIA : menu **Types de réunion → Importer**, choisissez le fichier
`.transcria-type.json`. Le type arrive **privé et « à relire »** : ouvrez-le,
vérifiez-le, enregistrez pour l'activer. Un admin peut ensuite le partager à un
groupe ou à toute l'installation.

## Contribuer un type

1. Créez votre type dans TranscrIA, testez-le sur une vraie réunion.
2. Exportez-le (bouton <i>Exporter</i> de sa carte) → `mon-type.transcria-type.json`.
3. Ouvrez une pull request ajoutant le fichier ici, nommé d'après son slug
   (ex. `conseil-municipal.json`).

## Règles (vérifiées par la CI et à l'import)

- Le fichier est l'**enveloppe d'échange** : `{"schema_version": 1, "type": {...}}` ;
- **jamais de `branding`** (pied de page, logo) : c'est local à chaque installation ;
- **jamais de contenu réel de réunion** — les `detection_hints` et les
  `instruction` d'extraction sont des descriptions génériques, pas des extraits ;
- bornes du schéma : couleurs hex 6 chiffres, badge ≤ 16, bannière ≤ 80,
  ≤ 12 champs, ≤ 8 indices, ≤ 6 extractions (instructions ≤ 200 caractères,
  sans guillemets doubles, backticks ni accolades) ;
- pas de collision de nom avec les 18 types intégrés.

La revue de pull request fait office de modération : un type contribué doit être
compréhensible, utile au-delà d'une seule organisation, et rédigé en français
(langue du produit).
