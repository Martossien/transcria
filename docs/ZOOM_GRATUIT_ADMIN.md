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

1. Connectez-vous sur **marketplace.zoom.us** avec votre compte Zoom.
2. En haut à droite : **Develop → Build App** → choisissez **General App** → **Create**.
3. Cliquez le nom auto-généré (en haut à gauche) pour le renommer (ex. `transcria-bot`).
4. Page **Basic Information** : relevez **Client ID** et **Client Secret** du jeu
   **Development** (suffisant tant que l'app n'est pas publiée — elle ne le sera jamais).
5. **Features → Embed** : activez **Meeting SDK**.
6. Ne publiez PAS l'app : elle reste interne à votre compte.
7. Dans TranscrIA : **Administration → Connecteurs → fiche Zoom** — collez Client ID et
   Client Secret (le secret ne sera plus jamais réaffiché), **Enregistrer les
   identités**, puis **Tester la connexion** (le portail vérifie le couple contre
   l'endpoint OAuth officiel de Zoom — verdict en clair).
8. Vérifiez que `zoom-sdk` figure dans `platforms:` du `runner.yaml` de l'exécutant
   (redémarrez-le après modification) — la carte « Réunion » propose alors Zoom.

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

- **Portée** : l'app couvre les réunions de TOUTE l'organisation, quel qu'en soit
  l'hôte. Aucune revue Zoom n'est nécessaire tant que le bot ne rejoint pas les
  réunions d'AUTRES organisations (dans ce cas : jetons ZAK/OBF + revue Zoom).
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
