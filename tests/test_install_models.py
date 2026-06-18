from __future__ import annotations

from pathlib import Path

import pytest

from transcria.install_models import (
    PYANNOTE_MODEL_ID,
    detect_local_models,
    download_pyannote_pipeline,
    find_first_gguf,
    find_pyannote_cache,
    is_non_empty_dir,
    main,
    parse_bool,
    render_cohere_setup_log,
    render_cohere_setup_prompt,
    render_local_model_detection_shell,
    render_model_detection_table,
    render_model_status_log,
    render_model_summary,
    render_pyannote_download_prompt,
    render_pyannote_setup_log,
    render_pyannote_token_prompt,
    resolve_repo_relative_path,
)


def test_resolve_repo_relative_path_handles_dot_slash():
    assert resolve_repo_relative_path("./models/cohere", Path("/opt/transcria")) == Path("/opt/transcria/models/cohere")
    assert resolve_repo_relative_path("/srv/models/cohere", Path("/opt/transcria")) == Path("/srv/models/cohere")


def test_is_non_empty_dir_requires_directory_with_content(tmp_path: Path):
    missing = tmp_path / "missing"
    empty = tmp_path / "empty"
    empty.mkdir()
    non_empty = tmp_path / "non-empty"
    non_empty.mkdir()
    (non_empty / "config.json").write_text("{}", encoding="utf-8")

    assert not is_non_empty_dir(missing)
    assert not is_non_empty_dir(empty)
    assert is_non_empty_dir(non_empty)


def test_parse_bool_accepts_shell_values():
    assert parse_bool("true")
    assert parse_bool("1")
    assert not parse_bool("false")
    assert not parse_bool("0")


def test_parse_bool_rejects_invalid_value():
    with pytest.raises(ValueError, match="booléen invalide"):
        parse_bool("maybe")


def test_find_pyannote_cache_returns_first_matching_directory(tmp_path: Path):
    hub = tmp_path / "hub"
    hub.mkdir()
    (hub / "models--other").mkdir()
    second = hub / "models--pyannote--speaker-diarization-community-2"
    first = hub / "models--pyannote--speaker-diarization-community-1"
    second.mkdir()
    first.mkdir()
    (hub / "models--pyannote--speaker-diarization-file").write_text("not a dir", encoding="utf-8")

    assert find_pyannote_cache(hub) == first


def test_find_first_gguf_returns_first_file_recursively(tmp_path: Path):
    models = tmp_path / "models"
    (models / "b").mkdir(parents=True)
    (models / "a").mkdir(parents=True)
    second = models / "b" / "z.gguf"
    first = models / "a" / "a.gguf"
    second.write_text("second", encoding="utf-8")
    first.write_text("first", encoding="utf-8")

    assert find_first_gguf(models) == first


def test_detect_local_models_returns_shell_ready_state(tmp_path: Path):
    cohere = tmp_path / "models" / "cohere"
    cohere.mkdir(parents=True)
    (cohere / "config.json").write_text("{}", encoding="utf-8")
    pyannote = tmp_path / "hf" / "models--pyannote--speaker-diarization-community-1"
    pyannote.mkdir(parents=True)
    squim = tmp_path / "torch" / "hub" / "torchaudio" / "models" / "squim_objective_dns2020.pth"
    squim.parent.mkdir(parents=True)
    squim.write_text("weights", encoding="utf-8")
    gguf = tmp_path / "models" / "arbitrage" / "model.gguf"
    gguf.parent.mkdir(parents=True)
    gguf.write_text("model", encoding="utf-8")

    detection = detect_local_models(
        cohere_path="./models/cohere",
        install_dir=tmp_path,
        hf_cache=tmp_path / "hf",
        torch_home=tmp_path / "torch",
        models_dir=tmp_path / "models",
        needs_llm=True,
    )

    assert detection.cohere_path == cohere
    assert detection.cohere_ok
    assert detection.pyannote_cache == pyannote
    assert detection.pyannote_ok
    assert detection.squim_path == squim
    assert detection.squim_ok
    assert detection.qwen_gguf == gguf
    assert detection.qwen_ok


def test_render_local_model_detection_shell_is_filterable(tmp_path: Path):
    detection = detect_local_models(
        cohere_path="./missing/cohere",
        install_dir=tmp_path,
        hf_cache=tmp_path / "hf",
        torch_home=tmp_path / "torch",
        models_dir=tmp_path / "models",
        needs_llm=False,
    )

    rendered = render_local_model_detection_shell(detection)

    assert f"COHERE_PATH={tmp_path}/missing/cohere" in rendered
    assert "COHERE_OK=false" in rendered
    assert "PYANNOTE_CACHE=''" in rendered
    assert "PYANNOTE_OK=false" in rendered
    assert "SQUIM_OK=false" in rendered
    assert "QWEN_GGUF=''" in rendered
    assert "QWEN_OK=false" in rendered


def test_render_model_summary_for_all_in_one_missing_values():
    rendered = render_model_summary(
        profile="all-in-one",
        needs_local_models=True,
        needs_llm=True,
        cohere_ok=False,
        pyannote_ok=True,
        qwen_ok=False,
        opencode_bin="",
    )

    assert rendered == """Modèles IA :
  [MANQUANT] Cohere ASR — huggingface-cli download CohereLabs/cohere-transcribe-03-2026
  [OK] pyannote diarization
  [MANQUANT] LLM d'arbitrage GGUF — choisir un palier dans install.sh
  [MANQUANT] opencode — résumé/correction LLM désactivé
"""


def test_render_model_summary_for_web_profile():
    rendered = render_model_summary(
        profile="web",
        needs_local_models=False,
        needs_llm=False,
        cohere_ok=False,
        pyannote_ok=False,
        qwen_ok=False,
        opencode_bin="",
    )

    assert rendered == """Modèles IA :
  [INFO] Modèles GPU locaux non requis pour le profil web
  [INFO] LLM/opencode non requis pour le profil web
"""


def test_render_model_detection_table_with_llm():
    rendered = render_model_detection_table(
        cohere_ok=True,
        cohere_path="/opt/transcria/models/cohere-asr",
        pyannote_ok=False,
        pyannote_cache="",
        needs_llm=True,
        qwen_ok=True,
        qwen_gguf="/opt/transcria/models/qwen/model.gguf",
        squim_ok=False,
    )

    assert rendered == """Modèles détectés :
  - Cohere ASR (STT ~6 Go): OK — cohere-asr
  - pyannote diarization (~2 Go): MANQUANT — HF_TOKEN requis + accepter conditions HF
  - LLM arbitrage GGUF: OK — model.gguf
  - SQUIM préflight (~28 Mo): MANQUANT — cf. docs/INSTALL.md § Réseau d'entreprise
"""


def test_render_model_detection_table_without_llm():
    rendered = render_model_detection_table(
        cohere_ok=False,
        cohere_path="",
        pyannote_ok=False,
        pyannote_cache="",
        needs_llm=False,
        qwen_ok=False,
        qwen_gguf="",
        squim_ok=True,
    )

    assert "LLM arbitrage GGUF" not in rendered
    assert "SQUIM préflight (~28 Mo): OK — cache torchaudio" in rendered


def test_render_model_status_log_for_local_model_checks():
    assert render_model_status_log(event="cohere-ok", value="/opt/models/cohere") == "OK:Cohere ASR       : /opt/models/cohere\n"
    assert render_model_status_log(event="cohere-missing", value="/opt/models/cohere") == (
        "WARN:Cohere ASR       : ABSENT  (/opt/models/cohere)\n"
    )
    assert render_model_status_log(event="pyannote-ok", value="/home/app/.cache/huggingface/hub/models--pyannote--speaker") == (
        "OK:pyannote cache   : models--pyannote--speaker\n"
    )
    assert render_model_status_log(event="pyannote-missing") == (
        "WARN:pyannote cache   : ABSENT  (téléchargement requis, HF_TOKEN nécessaire)\n"
    )
    assert render_model_status_log(event="squim-ok", value="/home/app/.cache/torch/squim.pth") == (
        "OK:SQUIM préflight  : /home/app/.cache/torch/squim.pth\n"
    )
    assert render_model_status_log(event="squim-missing") == (
        "WARN:SQUIM préflight  : ABSENT — téléchargé au 1er job (proxy requis si réseau filtré)\n"
    )


def test_render_model_status_log_for_llm_and_profile_skips():
    assert render_model_status_log(event="llm-ok", value="/opt/models/arbitrage.gguf") == (
        "OK:LLM arbitrage    : /opt/models/arbitrage.gguf\n"
    )
    assert render_model_status_log(event="llm-missing") == "WARN:LLM arbitrage    : ABSENT  (résumé/correction LLM non disponible)\n"
    assert render_model_status_log(event="llm-not-required", profile="web") == "INFO:LLM d'arbitrage : non requis pour le profil web\n"
    assert render_model_status_log(event="local-models-skipped", profile="resource-node") == (
        "INFO:Profil resource-node : vérification des modèles GPU locaux sautée\n"
    )


def test_render_model_status_log_rejects_unknown_event():
    with pytest.raises(ValueError, match="événement modèle inconnu : bad"):
        render_model_status_log(event="bad")


def test_render_cohere_setup_log_for_known_events():
    assert render_cohere_setup_log(event="missing") == "WARN:Le modèle Cohere ASR est introuvable au chemin configuré.\n"
    assert render_cohere_setup_log(event="current-path", value="./models/cohere") == (
        "INFO:Chemin actuel dans config.yaml : ./models/cohere\n"
    )
    assert render_cohere_setup_log(event="path-updated", value="/opt/cohere") == "OK:cohere_model_path mis à jour : /opt/cohere\n"
    assert render_cohere_setup_log(event="path-missing") == "WARN:Chemin introuvable — config inchangée\n"
    assert render_cohere_setup_log(event="download-start") == "INFO:Téléchargement de CohereLabs/cohere-transcribe-03-2026...\n"
    assert render_cohere_setup_log(event="download-ok") == "OK:Modèle Cohere téléchargé et configuré\n"
    assert render_cohere_setup_log(event="download-failed") == "ERROR:Téléchargement échoué — vérifiez vos accès HuggingFace\n"
    assert render_cohere_setup_log(event="cli-missing") == "WARN:huggingface-cli non trouvé — installer avec: pip install huggingface_hub\n"
    assert render_cohere_setup_log(event="manual-command-title") == "INFO:Commande manuelle :\n"
    assert render_cohere_setup_log(event="manual-command", value="/opt/models/cohere") == (
        "INFO:  huggingface-cli download CohereLabs/cohere-transcribe-03-2026 "
        "--local-dir /opt/models/cohere --local-dir-use-symlinks False\n"
    )
    assert render_cohere_setup_log(event="ignored") == "INFO:Modèle Cohere ignoré — pipeline STT désactivé\n"


def test_render_cohere_setup_log_rejects_unknown_event():
    with pytest.raises(ValueError, match="événement Cohere inconnu : bad"):
        render_cohere_setup_log(event="bad")


def test_render_cohere_setup_prompt_is_stable():
    rendered = render_cohere_setup_prompt()

    assert "Entrer le chemin où le modèle est déjà téléchargé" in rendered
    assert "Télécharger maintenant" in rendered
    assert rendered.endswith("  Votre choix [1/2/3] : ")


def test_render_pyannote_setup_log_for_known_events():
    assert render_pyannote_setup_log(event="missing-token") == "WARN:HF_TOKEN manquant — requis pour télécharger pyannote\n"
    assert render_pyannote_setup_log(event="create-token-url") == (
        "INFO:(Créer un token sur https://huggingface.co/settings/tokens)\n"
    )
    assert render_pyannote_setup_log(event="accept-terms-url") == (
        "INFO:(Accepter les conditions : https://huggingface.co/pyannote/speaker-diarization-community-1)\n"
    )
    assert render_pyannote_setup_log(event="token-saved") == "OK:HF_TOKEN sauvegardé dans .env\n"
    assert render_pyannote_setup_log(event="download-start") == "INFO:Téléchargement pyannote (peut prendre quelques minutes)...\n"
    assert render_pyannote_setup_log(event="download-ok") == "OK:pyannote téléchargé\n"
    assert render_pyannote_setup_log(event="download-failed") == (
        "ERROR:Téléchargement pyannote échoué — vérifiez le token et les conditions HF\n"
    )


def test_render_pyannote_setup_log_rejects_unknown_event():
    with pytest.raises(ValueError, match="événement pyannote inconnu : bad"):
        render_pyannote_setup_log(event="bad")


def test_render_pyannote_prompts_are_stable():
    assert render_pyannote_token_prompt() == "  HF_TOKEN (laisser vide pour ignorer) : "
    assert render_pyannote_download_prompt() == "Télécharger pyannote/speaker-diarization-community-1 maintenant ?"


def test_install_models_cli_checks_cohere_non_empty(tmp_path: Path):
    cohere = tmp_path / "models" / "cohere"
    cohere.mkdir(parents=True)
    assert main(["cohere-ok", "--path", "./models/cohere", "--install-dir", str(tmp_path)]) == 1

    (cohere / "config.json").write_text("{}", encoding="utf-8")
    assert main(["cohere-ok", "--path", "./models/cohere", "--install-dir", str(tmp_path)]) == 0


def test_install_models_cli_prints_found_paths(tmp_path: Path, capsys):
    pyannote = tmp_path / "hub" / "models--pyannote--speaker-diarization-community-1"
    pyannote.mkdir(parents=True)
    gguf = tmp_path / "models" / "model.gguf"
    gguf.parent.mkdir(parents=True)
    gguf.write_text("gguf", encoding="utf-8")

    assert main(["pyannote-cache", "--hf-cache", str(tmp_path / "hub")]) == 0
    assert capsys.readouterr().out == f"{pyannote}\n"

    assert main(["first-gguf", "--models-dir", str(tmp_path / "models")]) == 0
    assert capsys.readouterr().out == f"{gguf}\n"


def test_install_models_cli_returns_one_when_missing(tmp_path: Path):
    assert main(["pyannote-cache", "--hf-cache", str(tmp_path / "missing")]) == 1
    assert main(["first-gguf", "--models-dir", str(tmp_path / "missing")]) == 1


def test_install_models_cli_prints_summary(capsys):
    assert main([
        "summary",
        "--profile", "scheduler",
        "--needs-local-models", "true",
        "--needs-llm", "true",
        "--cohere-ok", "true",
        "--pyannote-ok", "true",
        "--qwen-ok", "true",
        "--opencode-bin", "/usr/local/bin/opencode",
    ]) == 0

    rendered = capsys.readouterr().out
    assert "[OK] Cohere ASR" in rendered
    assert "[OK] opencode : /usr/local/bin/opencode" in rendered


def test_install_models_cli_prints_detection_table(capsys):
    assert main([
        "detection-table",
        "--cohere-ok", "true",
        "--cohere-path", "/opt/models/cohere",
        "--pyannote-ok", "false",
        "--needs-llm", "false",
        "--qwen-ok", "false",
        "--squim-ok", "true",
    ]) == 0

    rendered = capsys.readouterr().out
    assert "Modèles détectés :" in rendered
    assert "LLM arbitrage GGUF" not in rendered


def test_install_models_cli_prints_status_log(capsys):
    assert main(["status-log", "--event", "llm-not-required", "--profile", "web"]) == 0

    assert capsys.readouterr().out == "INFO:LLM d'arbitrage : non requis pour le profil web\n"


def test_install_models_cli_prints_cohere_setup_log(capsys):
    assert main(["cohere-setup-log", "--event", "ignored"]) == 0

    assert capsys.readouterr().out == "INFO:Modèle Cohere ignoré — pipeline STT désactivé\n"


def test_install_models_cli_prints_cohere_setup_prompt(capsys):
    assert main(["cohere-setup-prompt"]) == 0

    assert capsys.readouterr().out.endswith("  Votre choix [1/2/3] : ")


def test_install_models_cli_prints_pyannote_setup_log(capsys):
    assert main(["pyannote-setup-log", "--event", "download-ok"]) == 0

    assert capsys.readouterr().out == "OK:pyannote téléchargé\n"


def test_install_models_cli_prints_pyannote_prompts(capsys):
    assert main(["pyannote-token-prompt"]) == 0
    assert capsys.readouterr().out == "  HF_TOKEN (laisser vide pour ignorer) : "

    assert main(["pyannote-download-prompt"]) == 0
    assert capsys.readouterr().out == "Télécharger pyannote/speaker-diarization-community-1 maintenant ?"


def test_download_pyannote_pipeline_uses_token_and_model_id():
    calls: list[tuple[str, str]] = []

    class FakePipeline:
        @staticmethod
        def from_pretrained(model_id: str, use_auth_token: str):
            calls.append((model_id, use_auth_token))
            return object()

    download_pyannote_pipeline("hf_secret", pipeline_cls=FakePipeline)

    assert calls == [(PYANNOTE_MODEL_ID, "hf_secret")]


def test_download_pyannote_pipeline_rejects_empty_token():
    with pytest.raises(ValueError, match="HF_TOKEN"):
        download_pyannote_pipeline("", pipeline_cls=object)


def test_install_models_cli_download_pyannote_prints_status(capsys, monkeypatch):
    calls: list[tuple[str, str]] = []

    def fake_download(token: str, *, model_id: str):
        calls.append((token, model_id))

    monkeypatch.setattr("transcria.install_models.download_pyannote_pipeline", fake_download)

    assert main(["download-pyannote", "--hf-token", "hf_secret", "--model-id", "custom/model"]) == 0

    assert calls == [("hf_secret", "custom/model")]
    assert capsys.readouterr().out == "pyannote téléchargé\n"
