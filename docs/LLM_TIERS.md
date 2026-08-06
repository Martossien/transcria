# Arbitration-LLM VRAM tiers (English summary)

> English summary of the tier catalog. Full protocol, per-model review sheets and raw
> measurements live in [BENCH_LLM_PALIERS.md](archive/BENCH_LLM_PALIERS.md) (French); backend
> lifecycle details in [LLM_BACKENDS.md](LLM_BACKENDS.md) (French).

TranscrIA's summary / SRT-correction / final-review phases are driven by a **local
OpenAI-compatible LLM** ("arbitration LLM"). Which model to run is not guessed from
parameter count: each VRAM tier ships a **benchmarked profile** — model, quantization,
context size, and the official sampling parameters for that model — selected by **reading
the actual deliverables** (summary fidelity, correction faithfulness, lexicon application),
not by automated scores. Automated metrics (WER/BLEU, length) reward exactly the failure
modes that matter here: a model that *rewrites* instead of correcting, inverts speaker
roles, or produces a well-formed but empty summary can score well and still ruin the
deliverable. So every tier was judged against a fixed human reading grid, with
**Qwen3.6-35B-A3B (48/64 GB tier) as the reference**.

## The tiers

| VRAM tier | Model | Quant | Context | Status |
|---|---|---|---|---|
| 8 GB | **Qwen3.5-4B** | Q5_K_M | 128K ⁰ | ✅ validated (real E2E 17/17, conforming correction; LFM2.5-2.6B was tried first and discarded — it broke the SRT structure) |
| 12 GB | **Qwen3.5-9B** | Q5_K_M | 192K ¹ | ✅ validated (replaces LFM2.5-8B, which failed the agentic workflow) |
| 16 GB | Qwen3.5-9B | Q6_K | 256K | ✅ validated |
| 24 GB | **Qwen3.6-35B-A3B** | UD-IQ4_NL_XL | 256K | ✅ validated (replaces Gemma 4 12B: 5× slower, regressions) |
| 32 GB | **Qwen3.6-27B** | Q5_K_M | 192K ² | ✅ validated (reference-level output) |
| 48 GB | Qwen3.6-35B-A3B | UD-Q6_K | 256K | ⭐ reference — cleanest emission, finest summary |
| 64 GB | Qwen3.6-35B-A3B | UD-Q8_K_XL | 256K | reference |

⁰ 8 GB (gaming cards, 2026-08): 6 378 MiB measured at 131,072 context (Q8 KV) → ~1.8 GB headroom (native 262K = 9 194 MiB, does not fit). Same gated-delta family as the 12 GB reference. The express install pairs this tier with **Kroko (CPU STT)** so the whole GPU stays free for the LLM.
¹ 12 GB: 192K context = 10 401 MiB measured → ~1.9 GB headroom; 256K would leave ~0.5 GB (not recommended).
² 32 GB: 192K = 29 168 MiB measured → ~3.6 GB headroom on one 32 GB card, ~1.4 GB on the most-loaded card of a 2×16 GB split.

One profile script per tier (`scripts/arbitrage_profiles/<tier>.sh`) carries the model's
**official sampling parameters** (Qwen ≈ temp 0.6; never reuse another model's settings).
Switching models = switching the profile script — the app always talks to a generic
`arbitrage` alias, so `config.yaml` never changes.

Below 12 GB there is **no arbitration LLM**: TranscrIA still runs transcription and
diarization, and falls back to raw (uncorrected) deliverables.

## Backend per tier

| Tier | Recommended backend | Why |
|---|---|---|
| 12–24 GB (single GPU) | **llama.cpp** | Finer quants (Q5_K_M / Q6_K / IQ4_NL) than Ollama's default Q4_K_M, and **q8_0 KV cache** (half the KV VRAM of fp16) — the 9B Q5 fits on 12 GB where the Ollama Q4 does not, and corrects better |
| 32–64 GB (multi-GPU) | Ollama **or** llama.cpp | Both validated (Ollama 35B Q4_K_M: 98/100; llama.cpp Q8: 97/100 on the review grid) |
| Split topology (web front + GPU node) | **vLLM** | Native FP8, tensor-parallel, concurrent batching — 100/100 on the review grid |

The backend abstraction (`start` / `is_loaded` / `unload`) lets the VRAM manager preempt
the LLM when STT needs the GPU and bring it back afterwards — including the Ollama case
where the daemon stays up and only the model is unloaded.

## How a tier is picked at install

`install.sh` detects the GPUs, computes the usable VRAM for the LLM (per-card and total),
proposes the highest tier that fits, downloads the model, and wires the profile script.
The *Administration → Models* page does the same from the UI, including switching to a
bigger tier later (e.g. on the bundled Docker image, which ships the 12 GB tier baked in:
set `TRANSCRIA_LLM_TIER` or use the Models page — model volumes are writable).
