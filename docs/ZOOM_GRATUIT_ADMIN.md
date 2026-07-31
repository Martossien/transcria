# Zoom avec TranscrIA — guide de l'administrateur (compte gratuit et entreprise)

> Vérifié contre la documentation officielle Zoom en juillet 2026 :
> [Get credentials (Meeting SDK)](https://developers.zoom.us/docs/meeting-sdk/get-credentials/),
> [Meeting SDK authorization](https://developers.zoom.us/docs/meeting-sdk/auth/),
> [Create a General app](https://developers.zoom.us/docs/integrations/create/).
> Le bot TranscrIA utilise le **Meeting SDK natif** (aucun navigateur, audio par
> participant) — il lui faut une « app » Zoom, c'est-à-dire un simple couple
> **Client ID / Client Secret** créé une fois sur le Marketplace. Rien n'est publié,
> rien n'est payant.

## 1. Compte GRATUIT (auto-hébergeur, association, essai)

Votre compte Zoom gratuit suffit — seule limite : **les réunions durent 40 min max**
(limite Zoom du plan gratuit, pas de TranscrIA).

Parcours VÉCU et validé le 2026-07-31 (nouvelle interface Marketplace) :

1. Connectez-vous sur **marketplace.zoom.us**. Si le site affiche la nouvelle interface,
   **désactivez « Try new experience »** (interrupteur en haut) — le menu **Develop**
   apparaît alors en haut à droite.
2. **Develop → Build App** ; la fenêtre *Zoom's API License and Terms of Use* s'affiche →
   **Agree**. Trois types d'app sont proposés : choisissez **General App** (pas
   Server-to-Server OAuth, pas Webhook Only) → **Create**.
3. Page **Basic Information** : laissez **User-Managed** (le jeton SDK est signé avec
   Client ID/Secret quel que soit ce choix — pas de flux d'installation OAuth chez
   nous) ; n'activez PAS « Use Public Client OAuth » (il nous faut le secret). Relevez
   **Client ID** et **Client Secret** (jeu **Development** — suffisant, l'app ne sera
   jamais publiée).
4. Vous pouvez IGNORER : le *Secret Token* de la page Access (il ne sert qu'aux
   webhooks), *Event Subscription* (laissez désactivé), *Plugin SDK* (intégration dans
   l'app Zoom Workplace — inutile ici), toute la page *Surface* (Home URL, produits,
   Mobile/Rooms/PWA, RTMS auto-start), *Connect*, *Custom Form*, *Scopes*, *Actions and
   Triggers* — rien à toucher.
5. Page **Embed** : activez **Meeting SDK** — c'est LA seule feature nécessaire.
   L'interrupteur « programmatic join use case » est déclaratif (revue Marketplace) :
   sans lui, l'entrée dans les réunions de VOTRE compte fonctionne (prouvé en gate réel).
6. Ne publiez PAS l'app : elle reste interne à votre compte.
7. Dans TranscrIA : **Administration → Connecteurs → fiche Zoom** — collez Client ID et
   Client Secret (le secret ne sera plus jamais réaffiché), **Enregistrer les
   identités**, puis **Tester la connexion** — attendu : « identifiants VALIDES — Zoom a
   délivré un jeton ».
8. Vérifiez que `zoom-sdk` figure dans `platforms:` du `runner.yaml` de l'exécutant
   (redémarrez-le après modification) — la carte « Réunion » propose alors Zoom.

⚠ **Portée du compte (règle Zoom depuis mars 2026)** : rejoindre les réunions de VOTRE
compte ne demande rien de plus ; rejoindre celles d'un AUTRE compte exige un jeton OBF
et la revue de l'app par Zoom ([FAQ OBF](https://developers.zoom.us/docs/meeting-sdk/obf-faq/)).

Pour capter : démarrez votre réunion, puis planifiez-la depuis la page d'accueil de
TranscrIA avec le **lien** et, si la réunion a un code, le **code EN CLAIR** dans le
champ dédié — ⚠ le `?pwd=…` des liens est une forme chiffrée que le SDK refuse (le bot
resterait muet en « attente de l'hôte »).

## 2. Compte ENTREPRISE — ce que doit faire l'admin Zoom de l'organisation

Sur un compte d'organisation, la création d'apps est généralement réservée aux
**administrateurs Zoom** de l'entreprise. L'admin TranscrIA n'a pas à avoir de compte
développeur : c'est **l'admin Zoom** qui fait les étapes 1-6 ci-dessus **avec un compte
admin de l'organisation**, puis transmet Client ID/Secret (canal sûr) à l'admin
TranscrIA (étape 7).

Points propres à l'entreprise :

- **Qui crée l'app** : sur un compte d'organisation, la création d'apps est souvent
  réservée aux admins Zoom — c'est le vrai enjeu du choix *User-Managed / Admin-Managed*
  (le jeton SDK, lui, fonctionne avec les deux : aucun flux d'installation OAuth).
- **Portée** : l'app couvre les réunions de TOUTE l'organisation, quel qu'en soit
  l'hôte. Aucune revue Zoom n'est nécessaire tant que le bot ne rejoint pas les
  réunions d'AUTRES organisations (dans ce cas : jeton OBF + revue Zoom — obligatoire
  depuis mars 2026).
- **Enregistrement** : portail Zoom admin → *Settings → Recording & Transcript* —
  activer « Record to computer files », et sous « Who can request host permission to
  record? » cocher « Internal meeting participants » (+ « Auto approve » pour éviter un
  clic de l'hôte à chaque réunion).
- **Révocation** : supprimer l'app (ou régénérer le secret) sur le Marketplace coupe
  immédiatement le bot ; côté TranscrIA, vider les champs de la fiche retire les
  identités.
- **Où vivent les secrets** : chiffrés d'affichage côté portail (write-only), remis aux
  exécutants au claim, jamais dans les arguments de processus ni les journaux. Volet
  couvert par la revue sécurité du projet.

## 3. Diagnostic rapide

| Symptôme | Cause la plus fréquente |
|---|---|
| « Tester la connexion » → identifiants REFUSÉS | Client ID/Secret erronés, ou jeu Production relevé au lieu de Development |
| Bot en « attente de l'hôte » sans fin | code de réunion pris du lien (`?pwd=` chiffré) — saisir le code EN CLAIR |
| Carte « Réunion » sans Zoom | `zoom-sdk` absent de `platforms:` du runner, ou exécutant non vivant (check-list) |
| Bot éjecté à ~40 min | limite du plan Zoom gratuit — normale |
