# Modèles embarqués — attributions et licences (image `transcria-allinone:bundled`)

Cette image **embarque** les poids des modèles par défaut (non gated) pour un usage
« pull & run » hors-ligne. Chaque modèle reste sous sa propre licence, reproduite/référencée
ci-dessous. L'application TranscrIA elle-même est sous **Apache-2.0** (`/app/LICENSE`).

> ⚠️ Les modèles **gated** (Cohere ASR, pyannote) ne sont **PAS** embarqués : ils restent
> en opt-in via `HF_TOKEN` (après acceptation de leurs conditions sur huggingface.co) et
> ne sont donc pas couverts par ce NOTICE.

---

## 1. LLM d'arbitrage — Qwen3.5-9B (quantisé GGUF)

- **Dépôt** : `unsloth/Qwen3.5-9B-GGUF` — fichier `Qwen3.5-9B-Q5_K_M.gguf`
- **Licence** : **Apache License 2.0** (héritée de `Qwen/Qwen3.5-9B`)
- **Texte** : voir `/app/LICENSE` (Apache-2.0 intégral, identique pour le projet et ce modèle).

## 2. Transcription (STT) — faster-whisper large-v3

- **Dépôt** : `Systran/faster-whisper-large-v3` (conversion CTranslate2 du Whisper large-v3 d'OpenAI)
- **Licence** : **MIT**
- **Texte** : voir `MIT-faster-whisper.txt` (ce répertoire).

## 2 bis. Transcription secondaire (multi-STT) — Mistral Voxtral Mini 3B

- **Dépôt** : `mistralai/Voxtral-Mini-3B-2507`
- **Licence** : **Apache License 2.0**
- **Texte** : voir `/app/LICENSE` (Apache-2.0 intégral, identique pour le projet et ce modèle).
- Rôle dans l'image : moteur secondaire du **multi-STT ciblé** (retranscription arbitrée des
  segments acoustiquement dégradés), **activé par défaut** depuis 0.3.4. Utilisable
  aussi comme backend principal (`models.stt_backend: voxtral`).

## 2 ter. Transcription une passe (opt-in) — MOSS-Transcribe-Diarize 0,9B

- **Dépôt** : `OpenMOSS-Team/MOSS-Transcribe-Diarize`
- **Licence** : **Apache License 2.0**
- **Texte** : voir `/app/LICENSE` (Apache-2.0 intégral, identique pour le projet et ce modèle).
- Rôle dans l'image : backend STT **opt-in** (`models.stt_backend: moss`) — transcription +
  étiquettes locuteur + timestamps en une passe. Le site Transformers 5 isolé requis est baké
  dans `/opt/transcria-moss-site` (paquets pip sous leurs licences respectives, `dist-info`
  conservés) et symlinké au démarrage sur le défaut de configuration.

## 2 quater. Runtimes STT servis (binaires, 0.3.6)

- **audio.cpp** (`/opt/runtimes/audiocpp/bin/audiocpp_server`) — © ShugoAI LLC,
  **Apache License 2.0** (texte : `/app/LICENSE`). Commit épinglé : `/opt/runtimes/audiocpp/COMMIT`.
- **parakeet.cpp** (`/opt/runtimes/parakeetcpp/bin/parakeet-server`) — **MIT**
  (texte : `MIT-parakeet.cpp.txt`, ce répertoire). Commit épinglé : `/opt/runtimes/parakeetcpp/COMMIT`.
- Aucun POIDS de modèle de ces runtimes n'est baké : Qwen3-ASR-1.7B (Apache-2.0) et le GGUF
  Nemotron (NVIDIA Open Model License) se téléchargent au runtime, sous les licences de leurs
  cartes de modèle respectives.

## 3. Diarisation — NVIDIA Streaming Sortformer 4spk v2.1

- **Dépôt** : `nvidia/diar_streaming_sortformer_4spk-v2.1`
- **Licence** : **NVIDIA Open Model License Agreement**
- **Texte intégral de l'accord** : voir `NVIDIA-Open-Model-License.txt` (ce répertoire),
  joint conformément à la Section « Redistribution » de l'accord.

### Notice d'attribution NVIDIA requise

> **Licensed by NVIDIA Corporation under the NVIDIA Open Model License**

L'usage de ce modèle doit rester conforme aux *Trustworthy AI terms* de NVIDIA
(https://www.nvidia.com/en-us/agreements/trustworthy-ai/terms/). Les mécanismes de sûreté
(« Guardrails ») du modèle ne sont ni modifiés ni contournés par cette distribution.

---

## Composants logiciels (rappel)

- TranscrIA : Apache-2.0 (`/app/LICENSE`) — llama.cpp : MIT — NeMo : Apache-2.0 —
  opencode : MIT — PyTorch : BSD — base `nvidia/cuda` : redistribuable (EULA NVIDIA).
