from __future__ import annotations

from pathlib import Path

import pytest

from transcria.install_models import (
    PYANNOTE_MODEL_ID,
    download_pyannote_pipeline,
    find_first_gguf,
    find_pyannote_cache,
    is_non_empty_dir,
    main,
    parse_bool,
    render_model_summary,
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
