# De zéro au premier compte-rendu

*([English version](QUICKSTART.en.md))*

Une page, deux chemins — choisissez-en **un**. Prérequis commun : une machine Linux avec
un GPU NVIDIA (compute capability ≥ 7.5 ; **dès 8 Go de VRAM** — natif comme Docker
slim ; l'image bundled, qui embarque une LLM plus grosse, demande **≥ 12 Go** — et fonctionne même **sans GPU** : transcription CPU via le
moteur Kroko, sans phases LLM) et son pilote installé (`nvidia-smi` doit répondre).

## Chemin 1 — Docker (essayer le projet, recommandé)

**Étape 0 — préparer l'hôte (une fois).** Docker **≥ 25** (Docker CE — le `docker.io`
des dépôts Ubuntu/Debian ne suffit pas), puis cloner le dépôt et donner l'accès GPU :

```bash
git clone https://github.com/Martossien/transcria.git && cd transcria
scripts/setup_docker_gpu.sh          # nvidia-container-toolkit + spec CDI + vérification
```

**Étape 1 — une commande.**

```bash
scripts/docker_quickstart.sh --bundled     # → http://localhost:7870
```

`--bundled` tire l'image à modèles **embarqués** (~57 Go — comme un gros jeu vidéo — mais
ensuite : zéro téléchargement, fonctionne hors-ligne). Sans `--bundled`, l'image slim est
légère mais télécharge les modèles au premier traitement.

**Étape 2 — premier login.** Ouvrir `http://localhost:7870`, se connecter avec
**`admin` / `CHANGE-ME`** — un bandeau permanent vous rappelle de le changer, faites-le.

**Étape 3 — premier compte-rendu.** « Nouveau traitement » → déposer l'audio de la
réunion (ou enregistrer au micro) → choisir un **profil** (ex. *Word corrigé*) → laisser
dérouler → télécharger le DOCX (et le SRT, le ZIP complet).

## Chemin 2 — Installation native (déployer sur un hôte GPU)

```bash
git clone https://github.com/Martossien/transcria.git && cd transcria
./install.sh          # mode EXPRESS : détections, un récapitulatif, une confirmation
./start.sh            # migrations puis serveur → http://localhost:7870
```

Le mode express détecte tout (GPU → palier LLM, psql → PostgreSQL, token HF → modèles)
et affiche « voilà ce que je vais faire » avant d'agir ; `./install.sh --expert` redonne
le pas-à-pas question par question. Sans token HF, l'install choisit **whisper +
Sortformer** (aucun compte requis) — la qualité de référence (Cohere + pyannote) s'active
plus tard depuis **Administration → Modèles** avec un token.

Après connexion, la **checklist de premier démarrage** sur l'accueil signale ce qui
manque (modèles absents, GPU non vu…) avec un lien pour corriger, et disparaît quand
tout est vert. Pour la prod : `sudo systemctl enable --now transcria`, et
`venv/bin/python scripts/doctor.py` valide l'installation à tout moment.

## Ensuite

- [INSTALL.md](INSTALL.md) — référence complète (options, rôles distribués, dépannage)
- [DOCKER.md](DOCKER.md) — images slim/bundled, GPU, compose, rollback
- [TESTERS.md](TESTERS.md) — le test de fumée en 15 minutes
