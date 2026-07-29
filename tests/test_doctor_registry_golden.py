"""GOLDEN du registre doctor — filet posé AVANT la découpe de doctor.py (vague 0, méthode B0).

Ce que la découpe en sous-modules ne doit PAS changer : la COMPOSITION du diagnostic —
quels checks tournent, dans quel ordre, pour quel profil — et la SURFACE publique du module
(les tests et scripts importent `check_*` depuis `transcria.diagnostics.doctor`, la façade
doit continuer de tout exposer). Un refactor qui perd un check en route passerait les tests
unitaires de chaque check ; c'est CE fichier qui rougirait.

Les listes ci-dessous sont des instantanés VOULUS : toute évolution du registre (nouveau
check, nouveau profil) doit les mettre à jour explicitement — c'est le prix d'un golden.
"""
from __future__ import annotations

from transcria.diagnostics import doctor as doc

# Instantané : l'ordre COMPLET du profil all-in-one (2026-07-29, avant découpe).
GOLDEN_ALL_CHECKS = [
    "check_database",
    "check_database_encoding",
    "check_arbitrage_script",
    "check_arbitrage_llm",
    "check_opencode",
    "check_opencode_model_resolution",
    "check_inference_nodes",
    "check_remote_stt_control_plane",
    "check_served_stt_runtimes",
    "check_meeting_scheduling",
    "check_stt_instances_vram",
    "check_identity_backend",
    "check_transport_security",
    "check_inference_node_gpus",
    "check_local_models",
    "check_storage",
    "check_disk_space",
    "check_shared_storage",
]

GOLDEN_PROFILES = {
    "all-in-one": GOLDEN_ALL_CHECKS,
    "web": [
        "check_database",
        "check_database_encoding",
        "check_inference_nodes",
        "check_remote_stt_control_plane",
        "check_meeting_scheduling",
        "check_inference_node_gpus",
        "check_storage",
        "check_shared_storage",
    ],
    "scheduler": GOLDEN_ALL_CHECKS,
    "resource-node": [
        "check_resource_node_auth",
        "check_resource_node_engines",
        "check_served_stt_runtimes",
        "check_resource_node_ports",
        "check_local_models",
    ],
    "migrate": [
        "check_database",
        "check_database_encoding",
    ],
}

# La surface que la façade `doctor` doit continuer d'exposer après découpe : tout ce que
# les tests, scripts/doctor.py et la doc importent aujourd'hui.
GOLDEN_PUBLIC_SURFACE = GOLDEN_ALL_CHECKS + [
    "check_resource_node_auth",
    "check_resource_node_engines",
    "check_resource_node_ports",
    "check_deployment_profile",
    "check_systemd_profile",
    "CheckResult",
    "OK",
    "WARN",
    "FAIL",
    "diff_live_schema",
    "expected_model_assets",
    "run_doctor",
    "compute_exit_code",
    "format_report",
    "main",
]


def test_all_in_one_registry_order_is_the_golden():
    assert [f.__name__ for f in doc._CHECKS] == GOLDEN_ALL_CHECKS


def test_profile_registries_match_the_golden():
    assert set(doc._PROFILE_CHECKS) == set(GOLDEN_PROFILES), "profils ajoutés/retirés sans MAJ du golden"
    for profile, expected in GOLDEN_PROFILES.items():
        names = [f.__name__ for f in doc._PROFILE_CHECKS[profile]]
        assert names == expected, f"profil {profile} : {names} ≠ golden"


def test_facade_exposes_the_public_surface():
    missing = [name for name in GOLDEN_PUBLIC_SURFACE if not hasattr(doc, name)]
    assert not missing, f"la façade doctor n'expose plus : {missing}"


def test_every_check_is_callable_with_a_single_cfg_argument():
    """Le contrat du registre : chaque entrée s'appelle `check(cfg)` (les dépendances
    effectives sont des kwargs injectables avec défauts). La découpe ne doit pas
    l'oublier en déplaçant une signature."""
    import inspect

    for func in doc._CHECKS:
        params = list(inspect.signature(func).parameters.values())
        assert params, f"{func.__name__} : aucun paramètre"
        required_positional = [
            p for p in params
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) and p.default is p.empty
        ]
        assert len(required_positional) == 1, (
            f"{func.__name__} : signature incompatible registre (positionnels requis : "
            f"{[p.name for p in required_positional]})"
        )
