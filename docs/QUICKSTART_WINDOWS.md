# TranscrIA sur Windows 11 — WSL2 + Docker

*([English version](QUICKSTART_WINDOWS.en.md))*

> **Statut : guide vérifié sur les documentations officielles (Microsoft, NVIDIA, Docker,
> août 2026), validation sur machine réelle en cours.** Un retour d'expérience est
> bienvenu dans les [Discussions](https://github.com/Martossien/transcria/discussions).

TranscrIA est une application Linux — mais Windows 11 sait exécuter Linux avec accès
GPU complet via **WSL2**, et nos images Docker tournent dessus **telles quelles** (le
conteneur atteint 90-100 % des performances natives en inférence : le goulot est la
carte, pas la virtualisation). Ce guide part d'un Windows 11 nu et arrive au premier
compte-rendu.

**Prérequis :**

| Quoi | Combien |
|---|---|
| Windows | Windows 11 (ou Windows 10 build 19041+), droits administrateur |
| Carte NVIDIA | GTX 10xx ou plus récente ; **dès 8 Go de VRAM** pour le workflow complet |
| Disque libre | **~130 Go** pour l'image bundled (recommandée) ; ~60 Go pour la slim — sur le disque de **votre choix** (C:, D:, E:…), l'installation guidée le demande |
| Connexion | stable (l'image recommandée pèse ~60 Go — comme un gros jeu vidéo) |

**Quelle image ?** Pour Windows nous recommandons la **bundled** : les modèles sont
embarqués, zéro configuration, fonctionne hors-ligne ensuite — c'est le moins de pièces
mobiles pour une première installation. Prenez la **slim** (~22 Go) si le disque ou la
connexion est juste : les modèles se téléchargent alors depuis la page
**Administration → Modèles** du portail.

## Voie recommandée — l'installation guidée (un script fait tout)

Ouvrez **PowerShell en administrateur** (menu Démarrer → tapez « PowerShell » → clic
droit → *Exécuter en tant qu'administrateur*), puis collez ces deux commandes :

```powershell
irm https://raw.githubusercontent.com/Martossien/transcria/main/scripts/windows/Install-TranscrIA.ps1 -OutFile "$env:USERPROFILE\Downloads\Install-TranscrIA.ps1"
powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\Downloads\Install-TranscrIA.ps1"
```

Le script vérifie votre machine (Windows, carte NVIDIA, RAM, espace libre), puis pose
**deux questions** : *sur quel disque installer* (C:, D:, E:… — il propose par défaut
celui qui a le plus de place, et tout ira dessus : Ubuntu ET les données Docker) et
*quelle image* (bundled recommandée / slim). Ensuite il fait tout : WSL2 + Ubuntu sur
le disque choisi, réglage mémoire, Docker Desktop, test GPU, téléchargement et
démarrage de TranscrIA — et ouvre le navigateur sur le portail à la fin.

**Principe important : le script se relance.** Si Windows demande un redémarrage (ou
si Docker affiche une fenêtre au premier lancement), faites ce qui est demandé puis
**relancez la même commande** — le script détecte ce qui est déjà fait et reprend où
il en était. Aucune étape n'est perdue, y compris un téléchargement interrompu.

> Statut : ce script suit exactement les étapes manuelles ci-dessous (vérifiées sur
> les documentations officielles) ; sa validation sur machine réelle est en cours. En
> cas de blocage, la voie manuelle fonctionne toujours — et un retour en
> [Discussions](https://github.com/Martossien/transcria/discussions) nous aide.

## Voie manuelle — le pas-à-pas

### Étape 1 — WSL2 (PowerShell administrateur)

```powershell
wsl --install
```

Redémarrez quand Windows le demande, laissez Ubuntu se créer (nom d'utilisateur + mot de
passe Linux), puis :

```powershell
wsl --update
wsl -l -v        # Ubuntu doit être en VERSION 2
```

Si l'installation fige à 0 % : `wsl --install --web-download -d Ubuntu`.

### Étape 2 — Le driver NVIDIA (côté Windows, et SEULEMENT côté Windows)

Installez ou mettez à jour le driver GeForce/RTX normal depuis
[nvidia.com](https://www.nvidia.com/drivers) (tout driver de 2022+ convient, R495 minimum).

> **Règle d'or : n'installez JAMAIS un driver NVIDIA ni les paquets `cuda`/`cuda-drivers`
> DANS Ubuntu/WSL.** Le driver Windows est projeté dans WSL automatiquement
> (`/usr/lib/wsl/lib/`) ; un driver Linux l'écraserait et casserait tout. C'est la panne
> n°1 constatée sur les forums — la doc NVIDIA l'interdit explicitement.

### Étape 3 — Docker Desktop

Installez [Docker Desktop](https://www.docker.com/products/docker-desktop/) (backend
WSL2 par défaut — ne changez rien). Le support GPU est **intégré** : rien d'autre à
installer. Licence : gratuite pour l'usage personnel (et les structures < 250 employés
et < 10 M$ de CA).

### Étape 4 — Vérifier que le conteneur voit la carte

Dans un terminal Ubuntu (menu Démarrer → Ubuntu) :

```bash
docker run --rm --gpus all nvidia/cuda:13.3.1-base-ubuntu24.04 nvidia-smi
```

Vous devez voir le tableau `nvidia-smi` avec votre carte. Si « NVIDIA-SMI has failed » :
relisez l'étape 2 (un driver Linux a probablement été installé dans WSL).

### Étape 5 — Donner de la RAM à WSL (machines 32 Go surtout)

Par défaut WSL prend 50 % de la RAM — trop juste sur 32 Go pour un traitement complet.
Créez `C:\Users\<vous>\.wslconfig` :

```ini
[wsl2]
memory=24GB
processors=8
swap=8GB
[experimental]
autoMemoryReclaim=gradual
sparseVhd=true
```

(Sur 64 Go, le défaut de 32 Go suffit.) Puis `wsl --shutdown` dans PowerShell et
rouvrez Ubuntu.

### Étape 6 — TranscrIA

Toujours dans Ubuntu — et **dans le home Linux** (`~`), jamais sous `/mnt/c` (le
système de fichiers Windows vu de Linux est ~5-7× plus lent) :

```bash
git clone https://github.com/Martossien/transcria.git && cd transcria
scripts/docker_quickstart.sh --bundled     # → http://localhost:7870
```

> Avec Docker Desktop, **sautez `scripts/setup_docker_gpu.sh`** (il installe le
> NVIDIA Container Toolkit, déjà intégré au backend). Il ne sert que pour la voie
> avancée docker-ce décrite en fin de page.

Pendant le téléchargement (~60 Go) : branchez-vous en Ethernet si possible et
**désactivez la mise en veille** (Paramètres → Système → Alimentation) — un pull
interrompu par la veille repart de zéro, alors qu'une simple relance de la commande
réutilise les couches déjà complétées.

### Étape 7 — Premier compte-rendu

Ouvrez `http://localhost:7870` **dans votre navigateur Windows** (le port traverse
WSL2 tout seul). Connectez-vous avec les **identifiants affichés à la fin de
l'installation** (`admin` + mot de passe généré, écrit dans `config.yaml` côté Ubuntu,
clé `auth.first_admin_password` — changez-le, le bandeau insistera), puis :
« Nouveau traitement » → déposer l'audio → choisir un profil →
télécharger le DOCX. Le parcours détaillé est dans le [QUICKSTART](QUICKSTART.md).

## Dépannage — les 5 pannes classiques

1. **`nvidia-smi` cassé dans le conteneur** → un driver/paquet NVIDIA a été installé
   dans WSL. Purgez-le (`sudo apt purge '*nvidia*' '*cuda*'`) ou réinitialisez la
   distro (`wsl --unregister Ubuntu` puis étape 1).
2. **Le disque `C:` se remplit et ne se vide jamais** → le disque virtuel WSL grossit
   mais ne rétrécit pas seul, même après `docker rmi`. Nettoyez depuis Docker Desktop
   (Settings → Resources → Disk usage), et compactez : `wsl --shutdown` puis
   `Optimize-VHD -Path <chemin du ext4.vhdx> -Mode Full` (PowerShell admin). Le
   `sparseVhd=true` de l'étape 5 automatise cela. L'image Docker peut aussi être
   déplacée sur un autre disque : Docker Desktop → Settings → Resources → Disk image
   location.
3. **Le job est tué en plein traitement (OOM)** → RAM WSL insuffisante, relisez
   l'étape 5.
4. **Après une veille : téléchargements/TLS qui échouent, horodatages faux** → dérive
   d'horloge WSL2 connue. `wsl --shutdown` puis relancez ; pendant un long traitement,
   désactivez la veille.
5. **Sous VPN d'entreprise : plus de réseau dans WSL** → sur Windows 11 récent le
   `dnsTunneling` (actif par défaut) règle la plupart des cas ; sinon ajoutez
   `dnsTunneling=true` sous `[wsl2]` dans `.wslconfig`. Certains antivirus ralentissent
   fortement WSL : une exclusion sur `%LOCALAPPDATA%\Docker` aide.

**Accéder au portail depuis une autre machine du foyer** : le NAT de WSL2 ne l'expose
pas tout seul. Soit une redirection
`netsh interface portproxy add v4tov4 listenport=7870 connectport=7870 connectaddress=localhost`
(PowerShell admin, + règle de pare-feu), soit le mode `networkingMode=mirrored`
(Windows 11 22H2+) dans `.wslconfig`.

## Et Windows Server ?

Possible, avec trois réserves. **Windows Server 2022 (à jour — KB de juin 2022) et
2025** ont WSL2 et `wsl --install` fonctionne comme sur Windows 11 (Server 2019 :
non — WSL 1 seulement). Mais **Docker Desktop n'est pas supporté sur les versions
Server** (position officielle de Docker) : la voie est donc l'annexe ci-dessous —
docker-ce **dans** la distro WSL2. Enfin, le GPU dans WSL2 sur Server n'est **pas
officiellement couvert par NVIDIA** (leur doc CUDA-on-WSL ne cite que Windows 10/11) :
les retours de terrain le donnent fonctionnel sur Server 2022 en mode WDDM, mais
validez `nvidia-smi` dans la distro avant d'aller plus loin — et sachez que la licence
du driver GeForce exclut contractuellement le déploiement en datacenter (les cartes
pro/RTX A ne sont pas concernées). Parcours non testé par nous.

## Annexe — la voie avancée sans Docker Desktop

Pour éviter Docker Desktop (licence en entreprise, ou préférence) : installez
**docker-ce** dans Ubuntu/WSL (dépôt Docker officiel — systemd est actif par défaut
dans l'Ubuntu de WSL2) puis `scripts/setup_docker_gpu.sh` du dépôt, qui installe le
NVIDIA Container Toolkit et vérifie l'accès GPU. La suite est identique à partir de
l'étape 6.
