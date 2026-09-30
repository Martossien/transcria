"""Ligne de commande opencode selon sa version majeure (v1 et v2 coexistent).

Les sorties de ``--version`` et les drapeaux sont ceux des binaires RÉELS relevés le
2026-09-30 (v1.18.30 : ``1.18.30`` ; v2.0.19 : ``opencode v2.0.19`` ; ``run --help`` v2
sans ``--dir``, avec ``--standalone``).
"""
from __future__ import annotations

from transcria.llm_tools.opencode_cli import (
    LATEST_KNOWN_MAJOR,
    OpencodeVersion,
    build_run_command,
    build_run_env,
    parse_opencode_version,
)


class TestParseVersion:
    def test_v1_prints_the_bare_version(self):
        v = parse_opencode_version("1.18.30\n")
        assert (v.major, v.minor, v.patch) == (1, 18, 30)
        assert v.line == 1 and v.known and str(v) == "1.18.30"

    def test_v2_prefixes_the_version(self):
        v = parse_opencode_version("opencode v2.0.19\n")
        assert (v.major, v.minor, v.patch) == (2, 0, 19)
        assert v.line == 2

    def test_first_non_empty_line_wins(self):
        assert parse_opencode_version("\n\n  1.18.22  \nbruit 9.9.9\n").major == 1

    def test_unreadable_output_is_unknown_and_runs_as_v1(self):
        for out in (None, "", "   \n", "command not found", "version inconnue"):
            v = parse_opencode_version(out)
            assert not v.known and v.line == 1

    def test_a_newer_major_runs_as_the_latest_known_line(self):
        v = parse_opencode_version("opencode v3.1.0")
        assert v.major == 3 and v.line == LATEST_KNOWN_MAJOR


def _cmd(version: OpencodeVersion) -> list[str]:
    return build_run_command(
        "/opt/oc/opencode", version=version, work_dir="/scratch/job/correction",
        model_ref="local/arbitrage", instruction="Fais le travail.", prompt_file="/scratch/prompt.txt",
    )


class TestRunCommand:
    def test_v1_anchors_the_project_with_dir(self):
        cmd = _cmd(OpencodeVersion(1, 18, 30))
        assert cmd == [
            "/opt/oc/opencode", "run", "--format", "json",
            "--dir", "/scratch/job/correction",
            "--model", "local/arbitrage",
            "Fais le travail.",
            "-f", "/scratch/prompt.txt",
        ]

    def test_v2_is_standalone_and_has_no_dir_flag(self):
        cmd = _cmd(OpencodeVersion(2, 0, 19))
        assert "--dir" not in cmd            # retiré en v2 : un drapeau inconnu fait échouer le run
        assert cmd[:3] == ["/opt/oc/opencode", "run", "--standalone"]   # jamais le serveur partagé
        assert cmd[3:] == [
            "--format", "json", "--model", "local/arbitrage",
            "Fais le travail.", "-f", "/scratch/prompt.txt",
        ]

    def test_unknown_version_keeps_the_historical_v1_command(self):
        assert _cmd(OpencodeVersion(0)) == _cmd(OpencodeVersion(1, 0, 0))


class TestRunEnv:
    def test_pwd_tmpdir_and_data_home_point_at_the_scratch(self):
        env = build_run_env({"PATH": "/bin", "PWD": "/ailleurs"}, work_dir="/scratch/w", data_home="/scratch/w/.d")
        # PWD : la v2 y lit sa racine de projet AVANT le cwd réel — il doit être réécrit.
        assert env == {"PATH": "/bin", "PWD": "/scratch/w", "TMPDIR": "/scratch/w", "XDG_DATA_HOME": "/scratch/w/.d"}

    def test_does_not_mutate_the_base_environment(self):
        base = {"PWD": "/ailleurs"}
        build_run_env(base, work_dir="/w", data_home="/d")
        assert base == {"PWD": "/ailleurs"}
