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

- **Téléchargé au runtime** (non redistribué dans ce dépôt) par `torchaudio`,
  via `torchaudio.pipelines.SQUIM_OBJECTIVE`.
- **Licence des poids** : Creative Commons Attribution 4.0 International
  (**CC-BY-4.0**). Entraînés sur le *DNS 2020 Dataset* (Microsoft DNS-Challenge).
- **Référence** : Kumar et al., « TorchAudio-Squim: Reference-less Speech Quality
  and Intelligibility measures in TorchAudio », ICASSP 2023.

---

## Bibliothèques

| Bibliothèque | Licence | Usage |
|---|---|---|
| `onnxruntime` | MIT | Inférence du modèle DNSMOS ONNX |
| `torchaudio` | BSD-2-Clause | Chargement / inférence SQUIM, ré-échantillonnage |
| `scipy` | BSD-3-Clause | Ré-échantillonnage, métriques acoustiques |
| `numpy` | BSD-3-Clause | Calculs numériques |
| `librosa` | ISC | Analyse audio |

Les autres dépendances Python sont déclarées dans `requirements.txt` et
conservent leur licence respective.
