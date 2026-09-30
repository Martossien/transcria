"""Réconciliation des opencode orphelins : le fichier .opencode.pid vit dans le SCRATCH d'agent.

Depuis l'isolation des agents (AgentWorkspace), le runner écrit `.opencode.pid` sous
`<agent_work_root>/<job>/<phase>/`, hors du dossier du job : une recherche limitée à
`jobs/<id>/` ne trouvait plus jamais rien (défaut latent trouvé en 0.4.6). Le signal vise
le groupe de process (wrapper npm, serveur privé de la v2).
"""
from __future__ import annotations

import signal

import transcria.services.job_executor as je


class _Log:
    def __init__(self):
        self.lines: list[str] = []

    def warning(self, msg, *args, **kw):
        self.lines.append(msg % args if args else msg)


def _fake_kills(monkeypatch, alive: set[int]):
    sent: list[tuple[int, int]] = []

    def killpg(pgid, sig):
        sent.append((pgid, sig))
        if sig == signal.SIGKILL:
            alive.discard(pgid)

    def kill(pid, sig):
        if pid not in alive:
            raise ProcessLookupError
        if sig == signal.SIGKILL:
            alive.discard(pid)

    monkeypatch.setattr(je.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(je.os, "killpg", killpg)
    monkeypatch.setattr(je.os, "kill", kill)
    monkeypatch.setattr(je.time, "sleep", lambda s: None)
    return sent


def test_pid_file_in_the_agent_scratch_is_found_and_the_group_is_signalled(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs"
    (jobs / "job1").mkdir(parents=True)
    scratch = tmp_path / "agent-work" / "job1" / "correction"
    scratch.mkdir(parents=True)
    (scratch / ".opencode.pid").write_text("4242\n")
    sent = _fake_kills(monkeypatch, alive={4242})
    log = _Log()

    je._kill_orphaned_opencode("job1", str(jobs), log, str(tmp_path / "agent-work"))

    assert (4242, signal.SIGTERM) in sent                       # SIGTERM au groupe
    assert (4242, signal.SIGKILL) in sent                       # toujours vivant → SIGKILL au groupe
    assert not (scratch / ".opencode.pid").exists()
    assert any("SIGTERM" in line for line in log.lines)


def test_without_agent_root_only_the_job_dir_is_scanned(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs"
    (jobs / "job1" / "work").mkdir(parents=True)
    (jobs / "job1" / "work" / ".opencode.pid").write_text("77")
    sent = _fake_kills(monkeypatch, alive=set())               # déjà mort
    je._kill_orphaned_opencode("job1", str(jobs), _Log())
    assert sent and sent[0][1] == signal.SIGTERM
    assert not (jobs / "job1" / "work" / ".opencode.pid").exists()


def test_group_signal_falls_back_to_the_pid_when_the_group_is_gone(monkeypatch):
    calls: list[tuple[str, int]] = []

    def killpg(pgid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(je.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(je.os, "killpg", killpg)
    monkeypatch.setattr(je.os, "kill", lambda pid, sig: calls.append(("kill", pid)))
    je._signal_opencode_group(99, signal.SIGTERM)
    assert calls == [("kill", 99)]

