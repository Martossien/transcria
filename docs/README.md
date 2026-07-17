# TranscrIA — Documentation

Reference documentation for operators, integrators, and contributors. The **product itself
is bilingual French / English** (interface, deliverables, installer, doctor — pick the
language at install time or from the navbar). The **project README is available in both
languages** ([English](../README.md) · [français](../README.fr.md)) and covers the full
install + Docker quickstart. These deeper reference documents are written in **French**;
this index is the English entry point (English summaries below).

New here? Start with the [project README](../README.md), then
[INSTALL.md](INSTALL.md) or [DOCKER.md](DOCKER.md) to get a running instance.
Non-technical readers (business owners, project managers, decision makers) have a
dedicated overview: [PRESENTATION.md](PRESENTATION.md) (in French) — use cases, benefits,
example results and the user journey.

## Deployment and operations

| Document | What it covers |
|---|---|
| [TESTERS.md](TESTERS.md) | **In English** — testing TranscrIA: what to expect (disk, first startup, models, GPU floor), the 15-minute smoke test, topologies we need tested, diagnostics and the report template |
| [INSTALL.md](INSTALL.md) | Host installation (`install.sh`), hardware and CUDA detection, models, `systemd` service, distributed roles, troubleshooting |
| [DOCKER.md](DOCKER.md) | Containerized deployment: turnkey quickstart, slim vs. bundled images, Compose, GPU access, variables, rollback |
| [UPGRADE.md](UPGRADE.md) | Upgrade and rollback procedure, obsolete configuration keys, database migrations |
| [SERVICE_RESSOURCES_GPU.md](SERVICE_RESSOURCES_GPU.md) | Split topology (web frontend + GPU resource node): remote inference, VRAM autonomy, admission and degraded modes |
| [STOCKAGE_PARTAGE_JOBS.md](STOCKAGE_PARTAGE_JOBS.md) | PostgreSQL-backed job file store for deployments without a shared filesystem |

## Reference

| Document | What it covers |
|---|---|
| [TECHNICAL.md](TECHNICAL.md) | Architecture, pipeline, modules, GPU orchestration, timing model, database |
| [API_REFERENCE.md](API_REFERENCE.md) | **Generated** HTTP API reference (all routes, auth, ⭐ scriptable contract) — regenerate with `scripts/generate_api_reference.py`, guarded in CI |
| [CONFIG_REFERENCE.md](CONFIG_REFERENCE.md) | Complete `config.yaml` reference (generated from the schema) |
| [DATA_MODEL.md](DATA_MODEL.md) | Database schema, job states, per-job files |
| [LLM_TIERS.md](LLM_TIERS.md) | **In English** — the benchmarked arbitration-LLM VRAM tiers (12 → 64 GB): validated model, quant and context per tier, backend recommendation |
| [LLM_BACKENDS.md](LLM_BACKENDS.md) | Arbitration LLM backends (Ollama / llama.cpp / vLLM) and hardware-driven selection |
| [I18N_MULTILANGUE.md](I18N_MULTILANGUE.md) | Bilingual FR/EN architecture (interface, deliverables, installer, doctor) and how to add a language |

## Features

| Document | What it covers |
|---|---|
| [PROFILS_TRAITEMENT_WORKFLOW.md](PROFILS_TRAITEMENT_WORKFLOW.md) | The six processing profiles and the human-in-the-loop wizard |
| [TYPES_REUNION_PERSONNALISES.md](TYPES_REUNION_PERSONNALISES.md) | Custom meeting types: catalog, detection, extracted fields, DOCX theming |
| [EDITEUR_SRT_INTEGRE.md](EDITEUR_SRT_INTEGRE.md) | Built-in SRT editor: versioned transcript correction |
| [PIPELINE_REPRISE.md](PIPELINE_REPRISE.md) | Resumable pipeline: phase checkpoints and provenance fingerprints |

## Security and compliance

| Document | What it covers |
|---|---|
| [SECURITY_MODEL.md](SECURITY_MODEL.md) | Authentication, RBAC, rate limiting, security headers |
| [AUDIT_DPO.md](AUDIT_DPO.md) | Audit trail, retention and purge, data-protection posture |

## Speech-to-text and audio

| Document | What it covers |
|---|---|
| [STT_ADAPTATIF_ET_HYBRIDE.md](STT_ADAPTATIF_ET_HYBRIDE.md) | Adaptive and hybrid STT: quality-driven backend selection |
| [EXTERNAL_STT_RUNTIMES.md](EXTERNAL_STT_RUNTIMES.md) | **Served STT runtimes** — audio.cpp (`qwen3asr`) and parakeet.cpp (`nemotron`) as first-class engines: pinned installer builds, on-demand start before jobs, per-engine health, native fallback |

## Benchmarking and validation

| Document | What it covers |
|---|---|
| [BENCHMARKING.md](BENCHMARKING.md) | How to run a reproducible bench: the `bench_audio.py` matrix runner and its four analyzers (metrics, LLM quality, WER vs reference, concurrency) |
| [STT_BENCHMARK_REAL_MEETINGS.md](STT_BENCHMARK_REAL_MEETINGS.md) | **In English** — published STT benchmark on real French meetings vs a professional human transcript: all engines and external runtimes, traps, failure modes |
| [BENCH_LLM_PALIERS.md](BENCH_LLM_PALIERS.md) | Per-VRAM-tier model benchmarks (the source for tier selection) |

## Concurrency, scale, and distributed inference

| Document | What it covers |
|---|---|
| [MIGRATION_API_SERVEUR_GPU.md](MIGRATION_API_SERVEUR_GPU.md) | Remote GPU resource-node HTTP API semantics (`inference_service`) — route list lives in [API_REFERENCE.md](API_REFERENCE.md) |
| [PLAN_TEST_CHARGE.md](PLAN_TEST_CHARGE.md) | Load-test procedure and campaigns (mandatory net for GPU-concurrency changes) |
| [PLAN_TEST_SPLIT_VLLM.md](PLAN_TEST_SPLIT_VLLM.md) | Split-topology (vLLM) validation procedure |

## Engineering plans

| Document | What it covers |
|---|---|
| [REFACTORING_QUALITE.md](REFACTORING_QUALITE.md) | Code-quality master plan: measured state (god modules, import graph, hotspots), target layering, refactoring waves and permanent guardrails |
| [PISTES_AMELIORATION.md](PISTES_AMELIORATION.md) | Post-0.3.7 improvement analysis: measured time breakdown, engine choices, CPU-fallback trade-offs, UX and operations gaps, suggested roadmap |

## History

Superseded planning documents, benchmarks, and analyses are kept under
[`archive/`](archive/) for provenance. They do not describe current behaviour; use the
documents above instead. Recently archived (2026-07-16): the Phase B concurrency plan
([CONCURRENCE_ET_CHARGE_PHASE_B.md](archive/CONCURRENCE_ET_CHARGE_PHASE_B.md), delivered —
decisions absorbed into `AGENTS.md`), the Parakeet integration scoping
([PARAKEET_STT_INTEGRATION.md](archive/PARAKEET_STT_INTEGRATION.md), superseded by
[EXTERNAL_STT_RUNTIMES.md](EXTERNAL_STT_RUNTIMES.md)), the VAD study
([VAD_OR_NOT.md](archive/VAD_OR_NOT.md), decision record) and the LLM tier validation
records ([LLM_PROFILS_VALIDATION.md](archive/LLM_PROFILS_VALIDATION.md) — the living
protocol is [BENCH_LLM_PALIERS.md](BENCH_LLM_PALIERS.md)).
