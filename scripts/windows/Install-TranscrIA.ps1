# Install-TranscrIA.ps1 -- installation guidee de TranscrIA sur Windows 11 (WSL2 + Docker).
#
# Usage (PowerShell en ADMINISTRATEUR) :
#   powershell -ExecutionPolicy Bypass -File .\Install-TranscrIA.ps1
#
# Le script est IDEMPOTENT : si Windows demande un redemarrage en cours de route,
# redemarrez puis relancez-le -- il detecte ce qui est deja fait et reprend.
#
# Ce qu'il fait : verifications (Windows, carte NVIDIA, RAM, disques) -> questions
# simples (quel disque ? quelle image ?) -> WSL2 + Ubuntu sur le disque choisi ->
# .wslconfig adapte a la RAM -> Docker Desktop (donnees sur le disque choisi) ->
# test GPU -> telechargement et demarrage de TranscrIA -> ouvre le navigateur.
#
# NB : messages volontairement sans accents (compatibilite encodage PowerShell 5.1).

$ErrorActionPreference = "Stop"
$RepoUrl = "https://github.com/Martossien/transcria.git"
$PortalUrl = "http://localhost:7870"

function Info($m)   { Write-Host "[INFO]   $m" -ForegroundColor Cyan }
function Ok($m)     { Write-Host "[OK]     $m" -ForegroundColor Green }
function Etape($m)  { Write-Host ""; Write-Host "=== $m ===" -ForegroundColor Yellow }
function Attention($m) { Write-Host "[ATTENTION] $m" -ForegroundColor Magenta }
function Stop-Erreur($m) { Write-Host "[ERREUR] $m" -ForegroundColor Red; exit 1 }

# -- 0. Prerequis de lancement ------------------------------------------------
Etape "Verifications prealables"

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Stop-Erreur "Ce script doit tourner en ADMINISTRATEUR. Clic droit sur PowerShell > 'Executer en tant qu'administrateur', puis relancez."
}

$build = [System.Environment]::OSVersion.Version.Build
if ($build -lt 19041) {
    Stop-Erreur "Windows trop ancien (build $build). Il faut Windows 11, ou Windows 10 build 19041 minimum."
}
Ok "Windows build $build"

$gpus = Get-CimInstance Win32_VideoController | Where-Object { $_.Name -match "NVIDIA" }
$hasGpu = [bool]$gpus
if ($hasGpu) {
    Ok ("Carte NVIDIA detectee : " + ($gpus[0].Name))
    Info "Rappel : le driver NVIDIA s'installe COTE WINDOWS uniquement (nvidia.com). Jamais dans Ubuntu/WSL."
} else {
    Attention "Aucune carte NVIDIA detectee. TranscrIA fonctionnera en mode CPU (transcription seule, plus lente)."
}

$ramGB = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
Ok "RAM : $ramGB Go"
if ($ramGB -lt 16) { Attention "Moins de 16 Go de RAM : les traitements longs risquent d'echouer." }

# -- 1. Choix du disque et de l'image -----------------------------------------
Etape "Choix du disque et de l'image"

$disks = Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" |
         Select-Object DeviceID, @{n = "FreeGB"; e = { [math]::Round($_.FreeSpace / 1GB) } }
foreach ($d in $disks) { Info ("Disque " + $d.DeviceID + "  libre : " + $d.FreeGB + " Go") }
$defaultDisk = ($disks | Sort-Object FreeGB -Descending | Select-Object -First 1).DeviceID

$answer = Read-Host "Sur quel disque installer (WSL + donnees Docker) ? [defaut : $defaultDisk]"
$targetDisk = if ($answer) { ($answer.TrimEnd(':', '\') + ":").ToUpper() } else { $defaultDisk }
$diskInfo = $disks | Where-Object { $_.DeviceID -eq $targetDisk }
if (-not $diskInfo) { Stop-Erreur "Disque $targetDisk introuvable." }
$freeGB = $diskInfo.FreeGB

Write-Host ""
Info "Deux images au choix :"
Info "  [1] bundled (~60 Go, RECOMMANDEE) : modeles inclus, zero configuration, fonctionne hors-ligne. Necessite ~130 Go libres."
Info "  [2] slim    (~22 Go)             : legere, les modeles se telechargent ensuite depuis le portail. Necessite ~60 Go libres."
$imgChoice = Read-Host "Votre choix [defaut : 1]"
$bundled = ($imgChoice -ne "2")
$needGB = if ($bundled) { 130 } else { 60 }
if ($freeGB -lt $needGB) {
    Stop-Erreur "Seulement $freeGB Go libres sur $targetDisk -- il en faut ~$needGB. Liberez de la place ou choisissez l'autre image/disque."
}
Ok "Cible : $targetDisk ($freeGB Go libres), image $(if ($bundled) { 'bundled' } else { 'slim' })"

$wslDir = "$targetDisk\TranscrIA\wsl"
$dockerDataDir = "$targetDisk\TranscrIA\docker-wsl"

# -- 2. WSL2 + Ubuntu sur le disque choisi ------------------------------------
Etape "WSL2 + Ubuntu"

$wslOk = $false
try { wsl --status *> $null; $wslOk = ($LASTEXITCODE -eq 0) } catch { $wslOk = $false }
$hasUbuntu = $false
if ($wslOk) {
    $distros = (wsl -l -q) -replace "`0", ""   # sortie UTF-16 : purger les octets nuls
    $hasUbuntu = ($distros -match "Ubuntu")
}

if (-not $hasUbuntu) {
    Info "Installation de WSL2 + Ubuntu vers $wslDir (peut prendre plusieurs minutes)..."
    New-Item -ItemType Directory -Force -Path $wslDir | Out-Null
    wsl --install -d Ubuntu --location $wslDir --no-launch
    if ($LASTEXITCODE -ne 0) {
        # Vieille version de WSL sans --location : voie standard sur C:, deplacement ensuite.
        Attention "L'option --location a echoue (WSL ancien ?). Tentative d'installation standard puis deplacement."
        wsl --install -d Ubuntu --no-launch
        if ($LASTEXITCODE -ne 0) {
            Attention "Windows a probablement besoin d'un REDEMARRAGE pour activer WSL."
            Attention "Redemarrez, puis relancez ce script : il reprendra ici."
            exit 0
        }
        if ($targetDisk -ne "C:") {
            Info "Deplacement d'Ubuntu vers $wslDir..."
            wsl --manage Ubuntu --move $wslDir
        }
    }
    wsl --update
    Ok "Ubuntu installe."
} else {
    Ok "Ubuntu deja present."
}
wsl --set-default Ubuntu *> $null

# Premier contact non interactif : si l'initialisation exige un utilisateur, on la fait en root.
wsl -d Ubuntu -u root -- true *> $null
if ($LASTEXITCODE -ne 0) {
    Attention "Ubuntu doit etre initialise : une fenetre va s'ouvrir, choisissez un nom d'utilisateur"
    Attention "et un mot de passe (pour Linux), tapez 'exit', puis RELANCEZ ce script."
    Start-Process wsl -ArgumentList "-d", "Ubuntu"
    exit 0
}
Ok "Ubuntu operationnel."

# -- 3. .wslconfig adapte a la RAM --------------------------------------------
Etape "Reglage memoire WSL (.wslconfig)"

$wslConfigPath = "$env:USERPROFILE\.wslconfig"
$wslMemGB = [math]::Min(32, [math]::Max(12, $ramGB - 8))
if (Test-Path $wslConfigPath) {
    Attention ".wslconfig existe deja -- non modifie. Verifiez que [wsl2] memory >= ${wslMemGB}GB."
} else {
    @"
[wsl2]
memory=${wslMemGB}GB
swap=8GB
[experimental]
autoMemoryReclaim=gradual
sparseVhd=true
"@ | Set-Content -Path $wslConfigPath -Encoding ascii
    wsl --shutdown
    Ok ".wslconfig cree (memory=${wslMemGB}GB) et WSL relance."
}

# -- 4. Docker Desktop (donnees sur le disque choisi) -------------------------
Etape "Docker Desktop"

$dockerPresent = $false
try { docker --version *> $null; $dockerPresent = ($LASTEXITCODE -eq 0) } catch { $dockerPresent = $false }

if (-not $dockerPresent) {
    New-Item -ItemType Directory -Force -Path $dockerDataDir | Out-Null
    $override = "install --quiet --accept-license --wsl-default-data-root=$dockerDataDir"
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        Info "Installation de Docker Desktop via winget (plusieurs minutes)..."
        winget install --id Docker.DockerDesktop --accept-source-agreements --accept-package-agreements --override $override
    } else {
        Info "winget absent -- telechargement direct de l'installeur Docker Desktop..."
        $installer = "$env:TEMP\DockerDesktopInstaller.exe"
        Invoke-WebRequest -Uri "https://desktop.docker.com/win/main/amd64/Docker%20Desktop%20Installer.exe" -OutFile $installer
        Start-Process $installer -Wait -ArgumentList "install", "--quiet", "--accept-license", "--wsl-default-data-root=$dockerDataDir"
    }
    Ok "Docker Desktop installe (donnees : $dockerDataDir)."
} else {
    Ok "Docker deja present."
    if ($targetDisk -ne "C:") {
        Attention "Docker etait deja installe : verifiez dans Docker Desktop > Settings > Resources"
        Attention "que le 'Disk image location' pointe bien vers $targetDisk si vous manquez de place sur C:."
    }
}

# Demarrer Docker Desktop et attendre que le moteur reponde.
$dockerExe = "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe"
if (Test-Path $dockerExe) {
    $engineUp = $false
    try { docker info *> $null; $engineUp = ($LASTEXITCODE -eq 0) } catch { $engineUp = $false }
    if (-not $engineUp) {
        Info "Demarrage de Docker Desktop (jusqu'a 3 minutes au premier lancement)..."
        Start-Process $dockerExe
        $deadline = (Get-Date).AddMinutes(4)
        do {
            Start-Sleep -Seconds 5
            try { docker info *> $null; $engineUp = ($LASTEXITCODE -eq 0) } catch { $engineUp = $false }
        } until ($engineUp -or (Get-Date) -gt $deadline)
        if (-not $engineUp) {
            Attention "Le moteur Docker ne repond pas encore. Au premier lancement, Docker Desktop peut"
            Attention "afficher une fenetre (acceptation des conditions). Acceptez, attendez l'icone verte,"
            Attention "puis RELANCEZ ce script : il reprendra ici."
            exit 0
        }
    }
    Ok "Moteur Docker operationnel."
} else {
    Attention "Docker Desktop vient d'etre installe : une deconnexion/reconnexion de session Windows"
    Attention "est parfois necessaire. Reconnectez-vous puis relancez ce script."
    exit 0
}

# -- 5. Test GPU dans un conteneur --------------------------------------------
if ($hasGpu) {
    Etape "Test GPU dans un conteneur"
    docker run --rm --gpus all nvidia/cuda:13.3.1-base-ubuntu24.04 nvidia-smi
    if ($LASTEXITCODE -ne 0) {
        Attention "Le conteneur ne voit pas la carte. Causes classiques : driver NVIDIA Windows"
        Attention "pas a jour (nvidia.com), ou un driver NVIDIA installe PAR ERREUR dans Ubuntu/WSL."
        Stop-Erreur "Corrigez puis relancez ce script (docs/QUICKSTART_WINDOWS.md, section Depannage)."
    }
    Ok "Le conteneur voit la carte."
}

# -- 6. TranscrIA -------------------------------------------------------------
Etape "Telechargement et demarrage de TranscrIA"

if ($bundled) { $flag = "--bundled" } elseif ($hasGpu) { $flag = "--gpu" } else { $flag = "--cpu" }
if ($bundled) {
    Info "L'image bundled pese ~60 Go : comptez de longues minutes a quelques heures selon la connexion."
    Info "Evitez la mise en veille pendant le telechargement (Parametres > Systeme > Alimentation)."
}
$bash = "set -e; cd ~; if [ ! -d transcria ]; then git clone $RepoUrl; fi; cd transcria; git pull --ff-only || true; scripts/docker_quickstart.sh $flag"
wsl -d Ubuntu -- bash -lc "$bash"
if ($LASTEXITCODE -ne 0) {
    Stop-Erreur "Le demarrage de TranscrIA a echoue -- relancez ce script pour reessayer (le telechargement deja fait n'est pas perdu). En cas de blocage : docs/QUICKSTART_WINDOWS.md, section Depannage."
}

# -- 7. C'est pret ------------------------------------------------------------
Etape "Termine"
Ok "TranscrIA tourne : $PortalUrl"
Info "Premier login : a la premiere visite du portail, une page vous demande de CREER le compte administrateur (identifiant + mot de passe de votre choix)."
Info "Arreter : dans Ubuntu, ~/transcria/scripts/docker_quickstart.sh --down  (ou via Docker Desktop)."
Info "Redemarrer apres un reboot du PC : lancez Docker Desktop, puis relancez simplement ce script."
Start-Process $PortalUrl
