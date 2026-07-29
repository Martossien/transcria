# scripts/ — carte du répertoire

Trois familles, trois destins. **La racine de `scripts/` est une API** : ses chemins sont
référencés par les `config.yaml` des installations existantes (`resource_node.engines`,
`llm.launch_script`…), les Dockerfiles, `install.sh` et la CI — on ne les déplace pas.
Les outils MANUELS, eux, sont rangés par famille.

## `scripts/` (racine) — exploitation & outillage référencé. NE PAS DÉPLACER.

| Famille | Fichiers | Qui les appelle |
|---|---|---|
| Lanceurs/arrêts LLM & STT | `launch_*.sh`, `stop_*.sh`, `switch_arbitrage_llm.sh`, `_stt_serve_lib.sh`, `test_stt.sh`, `check_arbitrage_llm.sh` | le service (config `resource_node.engines`, gate GPU), les Dockerfiles, la doc d'exploitation |
| Installation & config | `bootstrap_config.py`, `setup_opencode.py`, `detect_llama_server.py`, `plan_llm_placement.py`, `plan_stt_instances.py`, `migrate_sqlite_to_postgres.py`, `rotate_resource_node_key.py`, `verify_install_matrix.py` | `install.sh`, entrypoints Docker, doc INSTALL |
| Diagnostic | `doctor.py`, `smoke_resource_node.py`, `smoke_remote_stt.py`, `verify_split_topology.py` | l'exploitant (README, issues GitHub) |
| Bots (usage quotidien) | `bot.sh` | l'exploitant — la SEULE commande à connaître (docs/BOT_REUNION.md) |
| Docker | `docker_quickstart.sh`, `setup_docker_gpu.sh`, `release_bundled.sh` | README quickstart, CI, procédure de release |
| Gardes CI | `audit_imports.py`, `audit_front.py`, `i18n_check.py`, `generate_api_reference.py`, `form_fuzz.py`, `ui_walkthrough.py`, `seed_demo_dataset.py`, `seed_completed_job.py` | `.github/workflows/tests.yml` |
| Profils LLM | `arbitrage_profiles/` | `switch_arbitrage_llm.sh` |

## `scripts/gates/` — vérifications MANUELLES en conditions réelles

Un « gate » prouve qu'une brique marche contre le monde réel (une vraie réunion, un vrai
serveur) — jamais lancé par la CI. Codes de sortie documentés dans chaque en-tête.
`gate_bot_capture_selftest.py` (chaîne de capture sans réunion), `gate_bot_jitsi.py`
(réunion Jitsi complète, faux participants), `gate_zoom_auth.py` (identifiants Zoom seuls,
10 s), `gate_bot_zoom_sdk.py` (parcours Zoom SDK complet), `gate_visio_livekit.py`
(transport LiveKit). Mode d'emploi : `docs/BOT_REUNION.md`.

## `scripts/bench/` — bancs d'essai & analyse (reproductibles, jamais en prod)

`bench_audio.py` (matrice de runs) et ses analyseurs `bench_analyze.py` / `bench_eval.py` /
`score_reference_bench.py` ; corpus : `prepare_reference_windows.py`,
`extract_reference_docx.py`, `read_deliverables.py` ; charge : `load_test.py`,
`load_sampler.py`, `e2e_campaign.py`, `estimate_local_b5.py` ; prototypes hybrides :
`compare_stt_segments.py`, `arbitrate_hybrid_llm.py`, `build_hybrid_transcript.py`.
Mode d'emploi : `docs/BENCHMARKING.md`.

> Historique : jusqu'à la vague 0 de consolidation (2026-07), tout vivait à plat (63
> fichiers). Les chemins `scripts/gate_*` / `scripts/bench_*` cités par d'anciens documents
> (`docs/archive/`) ne sont plus valides — ces archives ne sont volontairement pas réécrites.
