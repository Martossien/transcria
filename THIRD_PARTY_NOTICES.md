# Mentions et licences des composants tiers

Ce fichier recense les modèles, jeux de poids et bibliothèques tiers utilisés par
TranscrIA, avec leur licence. Il satisfait notamment les obligations
d'**attribution** des licences Creative Commons (CC-BY-4.0).

---

## Modèles et poids embarqués ou téléchargés

### Modèle d'évaluation de qualité de parole — DNSMOS P.835 (`sig_bak_ovr`)

- **Fichier embarqué** : `transcria/audio/models/dnsmos_sig_bak_ovr.onnx`
  (SHA-256 `269fbebdb513aa23cddfbb593542ecc540284a91849ac50516870e1ac78f6edd`)
- **Auteur / source** : Microsoft Corporation — projet *DNS-Challenge*
  (<https://github.com/microsoft/DNS-Challenge>)
- **Licence** : Creative Commons Attribution 4.0 International (**CC-BY-4.0**)
  — texte intégral dans `transcria/audio/models/DNSMOS_MODEL_LICENSE.txt`
  (<https://creativecommons.org/licenses/by/4.0/>)
- **Modifications** : aucune. Le fichier est redistribué tel quel.
- **Référence** : Reddy, Gopal, Cutler, « DNSMOS P.835: A non-intrusive
  perceptual objective speech quality metric to evaluate noise suppressors »,
  ICASSP 2022.

### Poids d'évaluation de qualité non-intrusive — SQUIM Objective

- **Téléchargé au runtime** (non versionné dans ce dépôt) par `torchaudio`,
  via `torchaudio.pipelines.SQUIM_OBJECTIVE`. **Baké** dans l'image `:bundled` (cf. section
  « Modèles BAKÉS » plus bas) — la licence CC-BY-4.0 autorise cette redistribution avec attribution.
- **Licence des poids** : Creative Commons Attribution 4.0 International
  (**CC-BY-4.0**). Entraînés sur le *DNS 2020 Dataset* (Microsoft DNS-Challenge).
- **Référence** : Kumar et al., « TorchAudio-Squim: Reference-less Speech Quality
  and Intelligibility measures in TorchAudio », ICASSP 2023.

---

## Bibliothèques Python (venv embarqué dans les images)

La majorité des dépendances sont sous licences **permissives** (MIT, BSD, ISC, Apache-2.0).
Les composants à **attribution renforcée ou copyleft faible** présents au runtime sont listés
explicitement ci-dessous ; aucune dépendance runtime n'est sous **GPL/AGPL** (copyleft fort) —
le code de TranscrIA reste sous Apache-2.0 sans contamination.

| Bibliothèque | Licence | Usage / note |
|---|---|---|
| `onnxruntime` | MIT | Inférence du modèle DNSMOS ONNX |
| `torchaudio` | BSD-2-Clause | Chargement / inférence SQUIM, ré-échantillonnage |
| `torch`, `torchcodec` | BSD-3-Clause | Inférence GPU, décodage audio |
| `scipy`, `numpy`, `pandas` | BSD-3-Clause | Calcul numérique / acoustique |
| `librosa` | ISC | Analyse audio |
| `transformers`, `accelerate`, `huggingface_hub`, `datasets`, `vllm`, `nemo_toolkit`, `pyannote.audio` | **Apache-2.0** | STT/diarisation/serving — leur fichier `NOTICE` est conservé dans le venv (`site-packages`) ; obligation Apache-2.0 §4 respectée |
| `faster-whisper`, `demucs`, `pyannote.*` | MIT | STT de repli, débruitage, diarisation |
| `sherpa-onnx` | **Apache-2.0** | Runtime du backend STT `kroko` (zipformer2 streaming, CPU) |
| `lameenc` | **LGPL-3.0** | Encodeur MP3 (via `demucs`) — lié dynamiquement, redistribué tel quel (le code appelant n'est pas dérivé) |
| `certifi` | **MPL-2.0** | Bundle d'autorités de certification (copyleft *au fichier*, redistribué tel quel) |
| `text-unidecode` | Artistic-1.0 | Translittération (via `inflect`) |

> Dépendances **de développement uniquement** (`requirements-dev.txt`, **absentes des images
> runtime**) : `pytest-postgresql` (LGPLv3+), `pathspec`/outils de lint, etc. — non redistribuées.

Toutes les autres dépendances sont déclarées dans `requirements.txt` / `requirements-freeze.txt`
et conservent leur licence respective.

---

## Composants embarqués dans les images Docker

Au-delà du venv Python, les images applicatives embarquent :

- **opencode** (agent LLM des phases résumé/correction/relecture) — installé via l'installateur
  officiel SST. Licence **MIT** (projet `sst/opencode`) ; embarque ses propres modules npm, eux-mêmes
  sous licences permissives (MIT/ISC/BSD), avec leurs fichiers `LICENSE` conservés dans `node_modules`.
- **ffmpeg** (décodage audio, paquet Debian `ffmpeg` installé par `apt`) — **GPL-2.0+ / LGPL-2.1+**
  selon les composants (build Debian). TranscrIA **invoque** ffmpeg en sous-processus (aucune liaison
  de code) ; le binaire est redistribué **inchangé** tel que packagé par Debian, dont les **sources
  sont publiquement disponibles** (https://www.debian.org/distrib/packages). `libpq5` (client
  PostgreSQL) est sous la licence PostgreSQL (type BSD/MIT).

---

## Modèles téléchargés au runtime (NON redistribués dans le dépôt ni les images)

Ces poids ne sont **ni versionnés ni bakés dans les images** : ils sont téléchargés depuis Hugging Face
au premier usage (cache `HF_HOME` monté), sous le `HF_TOKEN` de l'utilisateur. La licence et les
**conditions d'accès (modèles *gated*)** sont celles de chaque carte de modèle ; à l'utilisateur de les
accepter avant usage :

| Modèle | Rôle | Accès / licence |
|---|---|---|
| CohereLabs `cohere-transcribe-03-2026` | STT principal | *gated* — licence de la carte de modèle Cohere (acceptation requise) |
| `pyannote/speaker-diarization-community-1` (+ segmentation/embeddings) | Diarisation | *gated* — MIT, conditions d'accès pyannote |
| `faster-whisper large-v3` | STT de repli | MIT |
| Qwen3.6 (LLM d'arbitrage, ex. `Qwen/Qwen3.6-27B-FP8`) | Résumé/correction LLM | licence de la carte de modèle Qwen (cf. Hugging Face) — servie hors image (endpoint externe) |
| `Banafo/Kroko-ASR` (modèles community, un par langue) | STT `kroko` (CPU pur) | **CC-BY-SA** (community) — attribution : Banafo (<https://kroko.ai/>) ; des variantes commerciales existent chez l'éditeur |

> TranscrIA ne distribue aucun de ces poids : il s'interface avec eux. Vérifiez et acceptez la
> licence de chaque modèle que vous déployez.

## Modèles BAKÉS dans l'image Docker `:bundled` (redistribués)

L'image `transcria-allinone:bundled` (cf. `Dockerfile.allinone-bundled`) **embarque** les modèles
par défaut **non gated** pour un usage « pull & run » hors-ligne. Leurs licences **autorisent la
redistribution** ; les textes/attributions sont bakés dans **`/licenses/`** de l'image (et versionnés
dans `licenses/` du dépôt). Les modèles *gated* (Cohere, pyannote) ne sont **PAS** embarqués.

| Modèle (dépôt) | Rôle | Licence | Attribution embarquée |
|---|---|---|---|
| `unsloth/Qwen3.5-9B-GGUF` (`Qwen3.5-9B-Q5_K_M.gguf`) | LLM d'arbitrage | **Apache-2.0** | `LICENSE` (Apache-2.0) |
| `Systran/faster-whisper-large-v3` | STT | **MIT** | `licenses/MIT-faster-whisper.txt` |
| `nvidia/diar_streaming_sortformer_4spk-v2.1` | Diarisation | **NVIDIA Open Model License** | `licenses/NVIDIA-Open-Model-License.txt` + notice « Licensed by NVIDIA Corporation under the NVIDIA Open Model License » |
| SQUIM Objective (`torchaudio.pipelines.SQUIM_OBJECTIVE`) | Qualité audio | **CC-BY-4.0** | attribution + référence ci-dessus (§ SQUIM Objective) |

> Conformément à la NVIDIA Open Model License (section *Redistribution*), une copie de l'accord et la
> notice d'attribution sont jointes dans `/licenses/` ; l'usage doit rester conforme aux *Trustworthy
> AI terms* de NVIDIA et les mécanismes de sûreté du modèle ne sont pas modifiés.
