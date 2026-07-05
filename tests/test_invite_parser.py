"""Tests du parseur d'invitation (transcria/context/invite_parser.py).

Données strictement génériques (example.org, noms fictifs) : aucune donnée de
réunion réelle ne doit figurer dans le code ni les tests.
"""
from transcria.context.invite_parser import (
    MAX_BRIEF_CHARS,
    render_invite_markdown,
    sanitize_invite,
)


class TestSanitizeInvite:
    def test_empty_input_returns_empty(self):
        assert sanitize_invite("") == {"brief": "", "names": []}
        assert sanitize_invite("   \n\t ") == {"brief": "", "names": []}

    def test_derives_names_from_email_local_parts(self):
        raw = (
            "DUPONT JEAN (SITE A) <jean.dupont@example.org>; "
            "MARTIN MARIE (SITE A) <marie.martin@example.org>"
        )
        result = sanitize_invite(raw)
        assert result["names"] == ["Jean Dupont", "Marie Martin"]

    def test_filters_resource_mailbox(self):
        # Une boîte de ressource (chiffres/tirets dans la partie locale) n'est pas
        # une personne et ne doit pas entrer dans la liste des noms.
        raw = (
            "DUPONT JEAN <jean.dupont@example.org>; "
            "SR-204 10 Places <sr-204.salle@example.org>"
        )
        result = sanitize_invite(raw)
        assert result["names"] == ["Jean Dupont"]

    def test_strips_emails_from_brief(self):
        raw = "Objet : revue. Contact jean.dupont@example.org pour les détails."
        result = sanitize_invite(raw)
        assert "@" not in result["brief"]
        assert "Objet : revue." in result["brief"]
        assert result["names"] == ["Jean Dupont"]

    def test_deduplicates_names(self):
        raw = "<jean.dupont@example.org>; encore <jean.dupont@example.org>"
        assert sanitize_invite("a" + raw)["names"] == ["Jean Dupont"]

    def test_handles_compound_local_part(self):
        raw = "<marie.claire.bernard@example.org>"
        assert sanitize_invite(raw)["names"] == ["Marie Claire Bernard"]

    def test_normalizes_whitespace_and_nbsp(self):
        raw = "Ligne une\t\tencore.\n\n\n\nLigne deux."
        brief = sanitize_invite(raw)["brief"]
        assert " " not in brief
        assert "\t" not in brief
        assert "\n\n\n" not in brief

    def test_caps_brief_length(self):
        raw = "mot " * 5000  # ~20000 chars
        assert len(sanitize_invite(raw)["brief"]) <= MAX_BRIEF_CHARS

    def test_plain_address_without_dot_is_not_a_person(self):
        # « contact@ » (pas de prenom.nom) ne doit pas produire de nom.
        assert sanitize_invite("<contact@example.org>")["names"] == []


class TestRenderInviteMarkdown:
    def test_empty_parsed_renders_empty_string(self):
        assert render_invite_markdown({"brief": "", "names": []}) == ""
        assert render_invite_markdown({}) == ""

    def test_renders_names_and_brief(self):
        md = render_invite_markdown({
            "brief": "Ordre du jour : point 1, point 2.",
            "names": ["Jean Dupont", "Marie Martin"],
        })
        assert "## Noms probables (orthographe à privilégier)" in md
        assert "- Jean Dupont" in md
        assert "## Contexte (objet, corps, ordre du jour)" in md
        assert "Ordre du jour" in md
        assert md.endswith("\n")

    def test_renders_brief_only_when_no_names(self):
        md = render_invite_markdown({"brief": "Contexte seul.", "names": []})
        assert "Noms probables" not in md
        assert "Contexte seul." in md

    def test_ignores_blank_names(self):
        md = render_invite_markdown({"brief": "", "names": ["  ", ""]})
        assert md == ""

    def test_renders_documents_section(self):
        md = render_invite_markdown({
            "brief": "",
            "names": [],
            "documents": [
                {"name": "deck.pptx", "text": "Slide 1 : objectifs trimestriels"},
            ],
        })
        assert "Documents présentés" in md
        assert "deck.pptx" in md
        assert "objectifs trimestriels" in md

    def test_documents_only_still_renders(self):
        # Un document joint suffit à produire le brief (ni noms ni texte collé).
        md = render_invite_markdown({"documents": [{"name": "n.txt", "text": "contenu"}]})
        assert md != ""
        assert "contenu" in md

    def test_documents_without_text_are_ignored(self):
        md = render_invite_markdown({"documents": [{"name": "vide.pdf", "text": ""}]})
        assert md == ""


class TestMaterializeMeetingInvite:
    """Le runner écrit meeting_invite.md depuis extra_data, ou rien si absent."""

    class _FakeJob:
        def __init__(self, extra):
            self._extra = extra

        def get_extra_data(self):
            return self._extra

    def test_writes_file_from_extra_data(self, tmp_path):
        from transcria.jobs.filesystem import JobFilesystem
        from transcria.workflow.runner import WorkflowRunner

        fs = JobFilesystem(str(tmp_path), "job-invite")
        (fs.job_dir / "summary").mkdir(parents=True, exist_ok=True)
        job = self._FakeJob({"meeting_invite": {"brief": "Ordre du jour : A, B.", "names": ["Jean Dupont"]}})

        path = WorkflowRunner._materialize_meeting_invite(fs, job)

        assert path is not None
        content = open(path, encoding="utf-8").read()
        assert "Jean Dupont" in content
        assert "Ordre du jour" in content

    def test_returns_none_without_invite(self, tmp_path):
        from transcria.jobs.filesystem import JobFilesystem
        from transcria.workflow.runner import WorkflowRunner

        fs = JobFilesystem(str(tmp_path), "job-noinvite")
        job = self._FakeJob({})
        assert WorkflowRunner._materialize_meeting_invite(fs, job) is None

    def test_returns_none_when_invite_empty(self, tmp_path):
        from transcria.jobs.filesystem import JobFilesystem
        from transcria.workflow.runner import WorkflowRunner

        fs = JobFilesystem(str(tmp_path), "job-empty")
        job = self._FakeJob({"meeting_invite": {"brief": "", "names": []}})
        assert WorkflowRunner._materialize_meeting_invite(fs, job) is None

    def test_writes_documents_into_materialized_file(self, tmp_path):
        # Bout-en-bout (sans GPU) : un document joint se retrouve dans meeting_invite.md,
        # le fichier passé à la LLM de résumé.
        from transcria.jobs.filesystem import JobFilesystem
        from transcria.workflow.runner import WorkflowRunner

        fs = JobFilesystem(str(tmp_path), "job-doc")
        (fs.job_dir / "summary").mkdir(parents=True, exist_ok=True)
        job = self._FakeJob({"meeting_invite": {
            "brief": "", "names": [],
            "documents": [{"name": "deck.pptx", "text": "Objectifs du trimestre : croissance"}],
        }})

        path = WorkflowRunner._materialize_meeting_invite(fs, job)

        assert path is not None
        content = open(path, encoding="utf-8").read()
        assert "Documents présentés" in content
        assert "deck.pptx" in content
        assert "Objectifs du trimestre" in content
