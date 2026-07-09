# TranscrIA — Documentation

Reference documentation for operators, integrators, and contributors. The **product itself
is bilingual French / English** (interface, deliverables, installer, doctor — pick the
language at install time or from the navbar). The **project README is available in both
languages** ([English](../README.md) · [français](../README.fr.md)) and covers the full
install + Docker quickstart. These deeper reference documents are written in **French**;
this index is the English entry point (English summaries below).

New here? Start with the [project README](../README.md), then
[INSTALL.md](INSTALL.md) or [DOCKER.md](DOCKER.md) to get a running instance.

## Deployment and operations

| Document | What it covers |
|---|---|
| [INSTALL.md](INSTALL.md) | Host installation (`install.sh`), hardware and CUDA detection, models, `systemd` service, distributed roles, troubleshooting |
| [DOCKER.md](DOCKER.md) | Containerized deployment: turnkey quickstart, slim vs. bundled images, Compose, GPU access, variables, rollback |
| [UPGRADE.md](UPGRADE.md) | Upgrade and rollback procedure, obsolete configuration keys, database migrations |
| [SERVICE_RESSOURCES_GPU.md](SERVICE_RESSOURCES_GPU.md) | Split topology (web frontend + GPU resource node): remote inference, VRAM autonomy, admission and degraded modes |
| [STOCKAGE_PARTAGE_JOBS.md](STOCKAGE_PARTAGE_JOBS.md) | PostgreSQL-backed job file store for deployments without a shared filesystem |

## Reference

| Document | What it covers |
|---|---|
| [TECHNICAL.md](TECHNICAL.md) | Architecture, pipeline, modules, HTTP API, GPU orchestration, timing model, database |
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
| [PARAKEET_STT_INTEGRATION.md](PARAKEET_STT_INTEGRATION.md) | NVIDIA Parakeet STT backend integration |
| [VAD_OR_NOT.md](VAD_OR_NOT.md) | Voice-activity-detection decision record and tuning |
| [STT_CORPUS.md](STT_CORPUS.md) | Contextual-biasing corpus format and use |

## Benchmarking and validation

| Document | What it covers |
|---|---|
| [BENCHMARKING.md](BENCHMARKING.md) | How to run a reproducible bench: the `bench_audio.py` matrix runner and its four analyzers (metrics, LLM quality, WER vs reference, concurrency) |
| [BENCH_LLM_PALIERS.md](BENCH_LLM_PALIERS.md) | Per-VRAM-tier model benchmarks (the source for tier selection) |
| [LLM_PROFILS_VALIDATION.md](LLM_PROFILS_VALIDATION.md) | Validation records for the LLM tier profiles |

## Concurrency, scale, and distributed inference

| Document | What it covers |
|---|---|
| [CONCURRENCE_ET_CHARGE_PHASE_B.md](CONCURRENCE_ET_CHARGE_PHASE_B.md) | PostgreSQL concurrency model: atomic claim, single scheduler, `LISTEN/NOTIFY` |
| [MIGRATION_API_SERVEUR_GPU.md](MIGRATION_API_SERVEUR_GPU.md) | Remote GPU resource-node HTTP API (`inference_service`) |
| [PLAN_TEST_CHARGE.md](PLAN_TEST_CHARGE.md) | Load-test procedure and campaigns |
| [PLAN_TEST_SPLIT_VLLM.md](PLAN_TEST_SPLIT_VLLM.md) | Split-topology (vLLM) validation procedure |

## History

Superseded planning documents, benchmarks, and analyses are kept under
[`archive/`](archive/) for provenance — including the `0.2.0` release plan. They do not
describe current behaviour; use the documents above instead.
