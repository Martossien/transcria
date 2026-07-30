"""Doctor — LLM d'arbitrage et opencode : script, serveur, binaire, résolution de modèle."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from transcria.diagnostics.checks.common import FAIL, OK, WARN, CheckResult, _t
from transcria.diagnostics.checks.probes import _probe_openai_models


def check_arbitrage_script(
    cfg: dict,
    *,
    is_file: Callable[[str], bool] = os.path.isfile,
    is_executable: Callable[[str], bool] = lambda p: os.access(p, os.X_OK),
) -> CheckResult:
    name = _t("chk_arb_script")
    services = cfg.get("services", {})
    script = os.environ.get("TRANSCRIA_ARBITRAGE_SCRIPT") or services.get("arbitrage_script", "")
    if not script:
        return CheckResult(name, WARN, _t("arbs_none"),
                           hint=_t("arbs_none_hint"))
    if not is_file(script):
        return CheckResult(
            name, FAIL, _t("arbs_missing", script=script),
            hint=_t("arbs_missing_hint"),
        )
    if not is_executable(script):
        return CheckResult(name, WARN, _t("arbs_not_exec", script=script),
                           hint=f"chmod +x {script}")
    return CheckResult(name, OK, _t("arbs_ok", script=script))

def _tensor_split_card_count(script_text: str) -> int | None:
    """Nombre de cartes NON NULLES déclarées par le `--tensor-split` du script (None si
    absent — un script mono-GPU ne le passe pas, et llama.cpp sans ce flag s'étale par
    défaut sur toutes les cartes visibles : indécidable statiquement)."""
    import re

    m = re.search(r"--tensor-split[=\s]+([0-9][0-9.,\s]*)", script_text)
    if not m:
        return None
    values = [v for v in re.split(r"[,\s]+", m.group(1).strip()) if v]
    try:
        count = sum(1 for v in values if float(v) > 0)
    except ValueError:
        return None
    return count or None

def check_llm_placement_declaration(
    cfg: dict,
    *,
    is_file: Callable[[str], bool] = os.path.isfile,
    read_text: Callable[[str], str] | None = None,
) -> CheckResult:
    """La config déclare-t-elle le MÊME placement LLM que le script de lancement ?

    Incident du 2026-07-30 (job fc268816) : script en `--tensor-split 1,1,1` (3 cartes)
    mais `gpu.llm_gpu_indices: [0]` — l'allocateur ne protégeait qu'une carte, une façade
    STT s'est posée sur une carte du split, et llama-server a segfaulté (cudaMalloc OOM).
    La divergence était SILENCIEUSE ; ce contrôle statique la rend visible au doctor.
    """
    name = _t("chk_llm_placement")
    services = cfg.get("services", {})
    script = os.environ.get("TRANSCRIA_ARBITRAGE_SCRIPT") or services.get("arbitrage_script", "")
    if not script or not is_file(script):
        return CheckResult(name, OK, _t("place_no_script"))
    reader = read_text or (lambda p: Path(p).read_text(encoding="utf-8", errors="replace"))
    try:
        text = reader(script)
    except OSError:
        return CheckResult(name, OK, _t("place_unreadable"))
    cards = _tensor_split_card_count(text)
    if cards is None:
        return CheckResult(name, OK, _t("place_no_split"))
    indices = (cfg.get("gpu", {}) or {}).get("llm_gpu_indices") or []
    if not indices:
        return CheckResult(
            name, WARN, _t("place_undeclared", cards=cards),
            hint=_t("place_undeclared_hint"),
        )
    if len(indices) != cards:
        return CheckResult(
            name, WARN,
            _t("place_mismatch", cards=cards, declared=len(indices), indices=list(indices)),
            hint=_t("place_mismatch_hint"),
        )
    # P1.c (audit 2026-07-30) : `llm_gpu_indices` est déclaré dans le référentiel
    # nvidia-smi de la MACHINE. Un CUDA_VISIBLE_DEVICES non trivial sur le process du
    # service ferait diverger ce référentiel de celui des phases (`cuda:N` visibles) —
    # divergence DORMANTE tant que personne ne le pose : on la rend visible ici.
    cvd = (os.environ.get("CUDA_VISIBLE_DEVICES") or "").strip()
    if cvd:
        return CheckResult(
            name, WARN, _t("place_cvd_set", cvd=cvd, indices=list(indices)),
            hint=_t("place_cvd_set_hint"),
        )
    return CheckResult(name, OK, _t("place_ok", cards=cards, indices=list(indices)))

def check_arbitrage_llm(
    cfg: dict,
    *,
    probe: Callable[[int], dict | None] | None = None,
) -> CheckResult:
    name = _t("chk_arb_llm")
    services = cfg.get("services", {})
    port = int(services.get("arbitrage_llm_port", services.get("qwen_port", 8080)))
    expected_model = (services.get("arbitrage_api_model_id") or "").strip()
    log_path = services.get("arbitrage_log_path") or f"/tmp/arbitrage_llm_{port}.log"

    probe = probe or _probe_openai_models
    try:
        models = probe(port)
    except Exception as exc:  # noqa: BLE001
        models = None
        _ = exc

    if not models:
        # Non bloquant : la LLM est lancée à la demande par le workflow.
        return CheckResult(
            name, WARN, _t("arbl_down", port=port),
            hint=_t("arbl_down_hint", log=log_path),
        )
    active = ""
    data = models.get("data") or []
    if data:
        active = data[0].get("id", "")
    if expected_model and active and active != expected_model:
        return CheckResult(
            name, WARN, _t("arbl_mismatch", port=port, active=active, expected=expected_model),
            hint=_t("arbl_mismatch_hint"),
        )
    return CheckResult(name, OK, _t("arbl_ok", port=port) + (_t("arbl_ok_model", active=active) if active else ""))

def check_opencode(
    cfg: dict,
    *,
    finder: Callable[..., str | None] | None = None,
) -> CheckResult:
    name = _t("chk_opencode")
    workflow = cfg.get("workflow", {})
    summary_on = workflow.get("summary_llm", {}).get("enabled", False)
    arbitration_on = workflow.get("arbitration_llm", {}).get("enabled", False)
    if not (summary_on or arbitration_on):
        return CheckResult(name, OK, _t("oc_disabled"))

    config_bin = workflow.get("arbitration_llm", {}).get("opencode_bin")
    if finder is None:
        # Différé §8.3(c) : repli du seam injectable — chargé seulement si ce check tourne.
        from transcria.llm_tools.opencode_setup import find_opencode_binary

        finder = find_opencode_binary
    resolved = finder(config_bin=config_bin)
    if not resolved:
        return CheckResult(
            name, FAIL, _t("oc_missing"),
            hint=_t("oc_missing_hint"),
        )
    return CheckResult(name, OK, _t("oc_found", resolved=resolved))

def _opencode_config_path() -> str:
    """Chemin du opencode.json qu'opencode lirait pour CET utilisateur.

    Suit la résolution d'opencode : ``OPENCODE_CONFIG`` explicite, sinon
    ``$XDG_CONFIG_HOME/opencode/opencode.json``, sinon ``~/.config/opencode/...``.
    """
    env = os.environ.get("OPENCODE_CONFIG")
    if env:
        return env
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "opencode", "opencode.json")

def _read_opencode_config(path: str) -> dict | None:
    """Lit et parse le opencode.json ; None si absent/illisible/non-objet."""
    import json

    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None

def check_opencode_model_resolution(
    cfg: dict,
    *,
    config_path: str | None = None,
    reader: Callable[[str], dict | None] | None = None,
) -> CheckResult:
    """Vérifie (statiquement, sans LLM) que le `model_id` du pipeline se RÉSOUT côté opencode.

    Le pipeline lance ``opencode run --model <workflow.arbitration_llm.model_id>``
    (ex. ``local/arbitrage``). Si le provider opencode n'expose pas une clé de modèle
    portant ce nom, l'appel échoue par « aucun texte produit » — panne silencieuse,
    diagnostiquée seulement au 1ᵉʳ résumé en prod (incident du 16/06/2026 : config
    opencode keyée ``qwen3-35b-arbitrage`` alors que le pipeline demandait ``arbitrage``).
    Ce contrôle est GPU-free et ne démarre PAS la LLM (contrairement au smoke opt-in) :
    il attrape le décalage à l'install / au doctor par défaut.
    """
    name = _t("chk_model_resolution")
    workflow = cfg.get("workflow", {})
    summary_on = workflow.get("summary_llm", {}).get("enabled", False)
    arbitration_on = workflow.get("arbitration_llm", {}).get("enabled", False)
    if not (summary_on or arbitration_on):
        return CheckResult(name, OK, _t("mr_disabled"))

    model_id = ((workflow.get("arbitration_llm", {}) or {}).get("model_id") or "").strip()
    if not model_id:
        return CheckResult(
            name, WARN, _t("mr_no_id"),
            hint=_t("mr_no_id_hint"),
        )
    if "/" not in model_id:
        return CheckResult(
            name, WARN, _t("mr_no_provider", model_id=model_id),
            hint=_t("mr_no_provider_hint"),
        )
    provider, _, model_key = model_id.partition("/")

    path = config_path or _opencode_config_path()
    reader = reader or _read_opencode_config
    data = reader(path)
    if data is None:
        return CheckResult(
            name, FAIL, _t("mr_no_config", path=path),
            hint=_t("mr_no_config_hint"),
        )
    providers = data.get("provider") or {}
    prov = providers.get(provider)
    if not isinstance(prov, dict) or not isinstance(prov.get("models"), dict):
        return CheckResult(
            name, FAIL, _t("mr_no_prov_key", provider=provider, path=path),
            hint=_t("mr_no_prov_key_hint", provider=provider),
        )
    models = prov["models"]
    if model_key not in models:
        available = ", ".join(models) or "(aucun)"
        return CheckResult(
            name, FAIL,
            _t("mr_no_model", provider=provider, model_key=model_key, available=available, model_id=model_id),
            hint=_t("mr_no_model_hint"),
        )
    return CheckResult(name, OK, _t("mr_ok", model_id=model_id, provider=provider, model_key=model_key))

def check_opencode_smoke(
    cfg: dict,
    *,
    runner_factory: Callable[..., Any] | None = None,
    probe: Callable[[int], dict | None] | None = None,
) -> CheckResult:
    """Test RÉEL opencode → LLM → texte (opt-in `--llm-smoke`).

    Lance opencode avec une consigne triviale et vérifie qu'il **produit du texte**.
    Attrape la classe de panne « opencode exit 0 mais 0 texte » (incident e62295c1).
    Nécessite la LLM d'arbitrage up et consomme de la VRAM — d'où l'opt-in (ce test
    rompt le contrat GPU-free / sans effet de bord du préflight par défaut).

    Pré-sonde le serveur LLM (`/v1/models`) AVANT opencode : si la LLM n'écoute pas, on
    échoue **immédiatement** avec une consigne claire au lieu d'attendre le timeout
    opencode (jusqu'à 120 s).
    """
    name = _t("chk_smoke")
    workflow = cfg.get("workflow", {})
    if not (workflow.get("summary_llm", {}).get("enabled", False)
            or workflow.get("arbitration_llm", {}).get("enabled", False)):
        return CheckResult(name, OK, _t("smoke_disabled"))

    import tempfile

    services = cfg.get("services", {})
    port = int(services.get("arbitrage_llm_port", services.get("qwen_port", 8080)))
    log_path = services.get("arbitrage_log_path") or f"/tmp/arbitrage_llm_{port}.log"

    # Pré-vol rapide : éviter un timeout opencode de ~120 s si la LLM est down.
    probe = probe or _probe_openai_models
    try:
        models = probe(port)
    except Exception:  # noqa: BLE001
        models = None
    if not models:
        return CheckResult(
            name, FAIL, _t("smoke_down", port=port),
            hint=_t("smoke_down_hint", log=log_path),
        )

    if runner_factory is None:
        # Différé §8.3(c) : repli du seam injectable — chargé seulement si ce check tourne.
        from transcria.llm_tools.opencode_runner import OpenCodeRunner

        runner_factory = OpenCodeRunner

    try:
        timeout_s = int(workflow.get("arbitration_llm", {}).get("smoke_timeout_seconds", 120))
    except (TypeError, ValueError):
        timeout_s = 120

    with tempfile.TemporaryDirectory(prefix="transcria_doctor_smoke_") as tmp:
        work = Path(tmp)
        prompt_file = work / "smoke_prompt.txt"
        prompt_file.write_text(_t("smoke_prompt_sys"), encoding="utf-8")
        runner = runner_factory(str(work), config=cfg)
        result = runner.run(
            _t("smoke_prompt_task"),
            str(prompt_file),
            timeout=timeout_s,
        )
        if not result.get("success"):
            return CheckResult(
                name, FAIL, _t("smoke_failed", error=result.get("error", _t("smoke_unknown_error"))),
                hint=_t("smoke_failed_hint", port=port, log=log_path),
            )
        smoke = work / "smoke.md"
        produced = bool(result.get("output")) or (
            smoke.is_file() and bool(smoke.read_text(encoding="utf-8").strip())
        )
        if not produced:
            return CheckResult(
                name, FAIL,
                _t("smoke_notext"),
                hint=_t("smoke_notext_hint", log=log_path),
            )
    return CheckResult(name, OK, _t("smoke_ok", port=port))
