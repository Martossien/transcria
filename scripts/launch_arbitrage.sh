#!/bin/bash
# ⚠ EXEMPLE spécifique à la machine du mainteneur (chemin llama-server, modèle,
#   CUDA_HOME, --threads, --tensor-split/--fit-target pour 3 GPUs…). Il NE marchera
#   PAS tel quel ailleurs : ADAPTEZ-LE, ou écrivez le vôtre selon votre install
#   (binaires compilés, paquets, 1 ou N GPUs, autre quantification, vLLM, …).
#
#   CONTRAT (seule chose qui compte pour TranscrIA) : exposer un serveur LLM
#   **compatible OpenAI** sur le port `services.arbitrage_llm_port` (défaut 8080),
#   servant un modèle dont l'alias correspond à `services.arbitrage_api_model_id`.
#   Pointez `services.arbitrage_script` vers VOTRE script.
#   (Idem pour le STT : voir scripts/launch_stt_*.sh, eux paramétrables par env.)
export CUDA_HOME=/usr/local/cuda-12.8
export PATH=$CUDA_HOME/bin:$PATH
export LD_LIBRARY_PATH=$CUDA_HOME/lib64:$LD_LIBRARY_PATH

numactl --interleave=all /home/admin_ia/llama.cpp/build/bin/llama-server \
--model /root/models/qwen3-35b-arbitrage/UD-Q8_K_XL/Qwen3.6-35B-A3B-UD-Q8_K_XL.gguf \
--alias arbitrage \
--host 0.0.0.0 --port 8080 \
--ctx-size 263144 \
--n-predict 81920 \
--no-mmap \
--threads 44 --threads-batch 88 \
--batch-size 2048 --ubatch-size 1024 \
--parallel 1 \
--flash-attn on \
--jinja \
--reasoning on \
--reasoning-budget 20480 \
--reasoning-budget-message "OK, I have thought enough. Let me provide the answer now." \
--no-prefill-assistant \
--verbose \
--n-gpu-layers all \
--split-mode layer \
--tensor-split 1,1,1 \
--numa distribute \
--cache-type-k q8_0 \
--cache-type-v q8_0 \
--temp 0.6 \
--top-p 0.95 \
--top-k 40 \
--min-p 0.01 \
--presence-penalty 0.0 \
--repeat-penalty 1.05 \
--fit on \
--fit-target 4000,4000,4000