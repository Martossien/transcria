# Runtimes STT externes — brancher un serveur C++ sans une ligne de code

TranscrIA sait consommer n'importe quel serveur STT **compatible OpenAI**
(`POST {url}/audio/transcriptions`) via son client distant (`RemoteTranscriber`,
cf. `docs/CONFIG_REFERENCE.md` § `inference.stt`). Nous avons vérifié sur notre
corpus de réunions réelles (chiffres : `docs/STT_BENCHMARK_REAL_MEETINGS.md`)
que deux jeunes runtimes C++ se branchent ainsi **en configuration seule**, avec
de très bonnes surprises en qualité comme en vitesse :

- **[audio.cpp](https://github.com/0xShug0/audio.cpp)** — moteur audio ggml
  (« le llama.cpp de l'audio ») : Qwen3-ASR 0.6B y transcrit une fenêtre de
  5 minutes en 7-10 s sur une seule carte 24 Go, à ~5 % du WER de notre moteur
  de production.
- **[parakeet.cpp](https://github.com/mudler/parakeet.cpp)** — inférence ggml
  des familles NVIDIA Parakeet/Nemotron (par l'auteur de LocalAI) : Nemotron 3.5
  ASR 0.6B (1,4 Go de poids) y atteint un WER au niveau de nos moteurs de
  production, en ~7 s par fenêtre de 5 minutes, avec un serveur OpenAI-compatible
  intégré (`parakeet-server`).

Ces runtimes évoluent vite : qualifiez toujours le **couple modèle + runtime**
avec votre propre audio avant un usage en production (le protocole du benchmark
est reproductible, cf. `docs/BENCHMARKING.md`).

## Recette de branchement

1. Lancer le serveur du runtime (voir sa documentation), par exemple :

   ```bash
   # parakeet.cpp (port 8030)
   parakeet-server --model nemotron-3.5-asr-streaming-0.6b-f16.gguf --port 8030
   ```

2. Déclarer l'endpoint dans `config.yaml` — on réutilise une clé de moteur
   logique existante (`whisper` ci-dessous) pour désigner l'endpoint ; le nom
   du modèle servi est transmis tel quel au serveur :

   ```yaml
   models:
     stt_backend: whisper        # clé logique portant l'endpoint distant
   inference:
     mode: hybrid                # STT distant, reste du pipeline local
     stt:
       response_format: json     # la plupart des runtimes C++ renvoient du json simple
       backends:
         whisper:
           url: "http://127.0.0.1:8030/v1"   # DOIT finir par /v1
           model: "parakeet"
           response_format: json
   ```

3. C'est tout : le pipeline (diarisation, correction LLM, livrables) reste
   inchangé ; seuls les tours de parole partent vers le serveur externe.
   `inference.stt.fallback_local: true` (défaut) rebascule sur le moteur local
   si le serveur tombe.

## Cas particuliers

- **Langue** : ces serveurs n'exposent pas tous un paramètre de langue.
  Les modèles à détection automatique peuvent dériver vers l'anglais sur de
  l'audio dégradé — surveillez la colonne « EN % » du protocole de bench
  (piège documenté dans `docs/STT_BENCHMARK_REAL_MEETINGS.md`).
- **Formats d'entrée** : le WAV 16 kHz mono est le chemin le plus sûr sur les
  runtimes jeunes ; TranscrIA envoie l'audio des tours en WAV.
- **Option intégrée sans GPU** : pour un serveur *sans* carte graphique, le
  backend **`kroko`** (Kroko-ASR sur sherpa-onnx, CPU pur) est intégré
  nativement — aucun serveur externe à lancer (cf. `docs/CONFIG_REFERENCE.md`
  § `kroko`).
