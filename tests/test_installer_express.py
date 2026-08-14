"""Mode express d'install.sh (transcria/installer/express) : décisions + récapitulatif.

Le module décide les défauts (PostgreSQL si faisable, whisper/sortformer sans token
sur config fraîche) et rédige le récapitulatif affiché avant l'UNIQUE confirmation.
Pré-venv : stdlib seulement — l'enrichissement palier LLM est best-effort.
"""
from __future__ import annotations

from pathlib import Path

from transcria.installer.express import ExpressPlan, build_express_plan, render_shell


def _plan(**overrides) -> ExpressPlan:
    kwargs = dict(
        gpu_count=2, total_vram_mb=48000, gpu_sizes_csv="24000,24000",
        psql_available=True, can_admin_pg=True, pg_server_reachable=True, has_hf_token=False,
        config_exists=False, install_service=True, service_user="alice",
        locale="fr",
    )
    kwargs.update(overrides)
    return build_express_plan(**kwargs)


class TestDecisions:
    def test_postgres_quand_psql_et_admin(self):
        assert _plan().setup_pg is True

    def test_sqlite_sans_psql_ou_sans_admin(self):
        assert _plan(psql_available=False).setup_pg is False
        assert _plan(can_admin_pg=False).setup_pg is False

    def test_sqlite_quand_serveur_pg_injoignable(self):
        # Leçon du 1er passage réel : psql + droits admin mais serveur arrêté → le
        # bootstrap du rôle échouerait en plein install. L'express replie sur SQLite
        # et le récapitulatif dit pourquoi (et comment revenir à PostgreSQL).
        plan = _plan(pg_server_reachable=False)
        assert plan.setup_pg is False
        recap = "\n".join(plan.recap)
        assert "serveur injoignable" in recap and "--postgres" in recap

    def test_open_models_seulement_config_fraiche_sans_token(self):
        assert _plan().open_models is True
        assert _plan(has_hf_token=True).open_models is False
        # Config existante : on ne bascule JAMAIS les backends d'un opérateur.
        assert _plan(config_exists=True).open_models is False


class TestRecap:
    def test_sans_token_annonce_whisper_et_la_page_modeles(self):
        recap = "\n".join(_plan().recap)
        assert "whisper + Sortformer" in recap and "page Modèles" in recap
        assert "PostgreSQL local" in recap
        assert "PREMIÈRE visite du portail" in recap  # issue #11 v2 : plus de mot de passe pré-annoncé
        assert "alice" in recap

    def test_avec_token_annonce_la_reference(self):
        recap = "\n".join(_plan(has_hf_token=True).recap)
        assert "Cohere + pyannote" in recap

    def test_config_existante_conservee(self):
        recap = "\n".join(_plan(config_exists=True).recap)
        assert "conservée" in recap

    def test_sans_gpu_pas_de_ligne_llm(self):
        recap = _plan(gpu_count=0, total_vram_mb=0, gpu_sizes_csv="").recap
        joined = "\n".join(recap)
        assert "aucun GPU" in joined and "Kroko" in joined
        assert "LLM d'arbitrage" not in joined  # « hw_none » a déjà tout dit

    def test_vram_sous_le_plancher_transcription_brute(self):
        # Sous le palier 8 Go (plancher 7500 depuis le palier LFM2.5-2.6B).
        recap = "\n".join(_plan(gpu_count=1, total_vram_mb=7000, gpu_sizes_csv="7000").recap)
        assert "transcription brute" in recap

    def test_carte_gaming_8go_resout_le_palier_8(self):
        recap = "\n".join(_plan(gpu_count=1, total_vram_mb=8192, gpu_sizes_csv="8192").recap)
        assert "palier 8" in recap and "Qwen3.5-4B" in recap

    def test_gros_gpu_resout_le_palier(self):
        # Dans le venv de test, PyYAML est là : la ligne LLM porte un palier résolu
        # (le modèle exact vient du catalogue — on n'ancre pas son nom ici).
        recap = "\n".join(_plan().recap)
        assert "llama.cpp" in recap and "palier" in recap

    def test_locale_en(self):
        recap = "\n".join(_plan(locale="en", has_hf_token=True).recap)
        assert "Database: local PostgreSQL" in recap and "reference quality" in recap

    def test_no_service(self):
        assert "--no-service" in "\n".join(_plan(install_service=False).recap)


class TestShellRendering:
    def test_lignes_machine(self):
        out = render_shell(_plan())
        assert out.splitlines() == [
            "EXPRESS_SETUP_PG=true", "EXPRESS_OPEN_MODELS=true",
            "EXPRESS_STT_BACKEND=whisper", "EXPRESS_DIAR_BACKEND=sortformer",
        ]
        out = render_shell(_plan(psql_available=False, has_hf_token=True))
        assert out.splitlines() == [
            "EXPRESS_SETUP_PG=false", "EXPRESS_OPEN_MODELS=false",
            "EXPRESS_STT_BACKEND=", "EXPRESS_DIAR_BACKEND=",
        ]


class TestBackendsSansToken:
    """Matrice matériel → backends du duo sans token (E2E réels du 2026-08-06)."""

    def test_gpu_confortable_whisper(self):
        p = _plan()  # 2× 24 Go
        assert (p.stt_backend, p.diarization_backend) == ("whisper", "sortformer")

    def test_petit_gpu_kroko_pour_laisser_le_gpu_a_la_llm(self):
        p = _plan(gpu_count=1, total_vram_mb=8192, gpu_sizes_csv="8192")
        assert p.stt_backend == "kroko" and p.diarization_backend == "sortformer"
        assert "Kroko" in "\n".join(p.recap)

    def test_sans_gpu_kroko_cpu(self):
        p = _plan(gpu_count=0, total_vram_mb=0, gpu_sizes_csv="")
        assert p.stt_backend == "kroko" and p.diarization_backend == "sortformer"

    def test_avec_token_ou_config_existante_aucune_bascule(self):
        assert _plan(has_hf_token=True).stt_backend == ""
        assert _plan(config_exists=True).stt_backend == ""


class TestLlmLineBestEffort:
    def test_catalogue_illisible_libelle_generique(self, monkeypatch):
        # Pré-venv (PyYAML absent) : l'import du recommandeur échoue → libellé générique,
        # jamais de crash du récapitulatif.
        import builtins
        real_import = builtins.__import__

        def block_yaml(name, *a, **k):
            if name.startswith("transcria.installer.arbitrage"):
                raise ImportError("yaml indisponible (python système nu)")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", block_yaml)
        recap = "\n".join(_plan().recap)
        assert "selon la VRAM" in recap


class TestCli:
    def test_express_subcommand_prints_shell_then_recap(self, tmp_path: Path, capsys, monkeypatch):
        from transcria.installer import cli

        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/psql" if name == "psql" else None)
        rc = cli.main([
            "express", "--gpu-count", "2", "--total-vram-mb", "48000",
            "--gpu-sizes-csv", "24000,24000", "--config", str(tmp_path / "config.yaml"),
            "--service-user", "alice", "--have-sudo",
        ])
        out = capsys.readouterr().out
        assert rc == 0
        assert "EXPRESS_SETUP_PG=true" in out and "EXPRESS_OPEN_MODELS=true" in out
        assert "whisper + Sortformer" in out

    def test_express_config_existante(self, tmp_path: Path, capsys, monkeypatch):
        from transcria.installer import cli

        (tmp_path / "config.yaml").write_text("x: 1\n", encoding="utf-8")
        monkeypatch.setattr("shutil.which", lambda name: None)
        rc = cli.main([
            "express", "--gpu-count", "0", "--total-vram-mb", "0",
            "--config", str(tmp_path / "config.yaml"), "--service-user", "bob",
        ])
        out = capsys.readouterr().out
        assert rc == 0
        assert "EXPRESS_OPEN_MODELS=false" in out and "EXPRESS_SETUP_PG=false" in out


class TestInstallShWiring:
    _CONTENT = (Path(__file__).resolve().parents[1] / "install.sh").read_text(encoding="utf-8")

    def test_flag_expert_parse(self):
        assert "--expert)" in self._CONTENT

    def test_bloc_express_garde_tty_all_in_one(self):
        assert 'EXPERT_MODE" = false && -t 0' in self._CONTENT
        assert "transcria.installer.cli express" in self._CONTENT

    def test_sortie_helper_filtree_avant_eval(self):
        # Convention recommend-llm : lignes machine isolées par grep AVANT l'eval,
        # sinon chaque ligne humaine du récap déclenche un WARN « sortie ignorée ».
        assert "grep '^EXPRESS_'" in self._CONTENT

    def test_express_bascule_en_non_interactif_apres_confirmation(self):
        bloc = self._CONTENT.split("SECTION 1-bis")[1].split("SECTION 2")[0]
        assert "NON_INTERACTIVE=true" in bloc and "ask_express" in bloc

    def test_setup_pg_respecte_un_drapeau_explicite(self):
        assert '[[ "$EXPRESS_SETUP_PG" = true && -z "$SETUP_PG" ]] && SETUP_PG=true' in self._CONTENT

    def test_bascule_open_models_seulement_si_demande(self):
        # Les backends viennent du module express (jamais en dur dans le shell).
        assert '"$EXPRESS_OPEN_MODELS" = true && -f "$CONFIG_PATH" && -n "$EXPRESS_STT_BACKEND"' \
            in self._CONTENT
        assert 'yaml_set "models.stt_backend" "$EXPRESS_STT_BACKEND"' in self._CONTENT
        assert 'yaml_set "models.diarization_backend" "$EXPRESS_DIAR_BACKEND"' in self._CONTENT
