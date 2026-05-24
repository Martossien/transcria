import uuid

from transcria.context.central_lexicon_models import GroupLexiconEntry
from transcria.context.central_lexicon_service import filter_lexicon_by_srt_presence
from transcria.context.central_lexicon_service import merge_lexicon_entries
from transcria.context.central_lexicon_store import CentralLexiconAccessError
from transcria.context.central_lexicon_store import CentralLexiconStore
from transcria.context.central_lexicon_store import CentralLexiconValidationError


def _name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class TestCentralLexiconStore:
    def test_admin_can_create_global_lexicon(self, app):
        with app.app_context():
            from transcria.auth.store import UserStore

            admin = UserStore.get_by_username("admin")
            lexicon = CentralLexiconStore.create_lexicon(
                admin,
                name=_name("global-lexicon"),
                group_id=None,
                allow_global=True,
            )

            assert lexicon.id
            assert lexicon.group_id is None

    def test_group_admin_cannot_create_global_lexicon(self, app):
        with app.app_context():
            from transcria.auth.groups import GroupStore
            from transcria.auth.models import GroupRole
            from transcria.auth.models import Role
            from transcria.auth.store import UserStore

            user = UserStore.create_user(username=_name("group-admin"), password="test12345", role=Role.OPERATOR)
            group = GroupStore.create_group(_name("group"))
            GroupStore.add_member(group.id, user.id, GroupRole.GROUP_ADMIN)

            try:
                CentralLexiconStore.create_lexicon(user, name="Global interdit", group_id=None, allow_global=True)
            except CentralLexiconValidationError as exc:
                assert "groupe" in str(exc)
            else:
                raise AssertionError("CentralLexiconValidationError attendu")

    def test_group_admin_can_create_group_lexicon(self, app):
        with app.app_context():
            from transcria.auth.groups import GroupStore
            from transcria.auth.models import GroupRole
            from transcria.auth.models import Role
            from transcria.auth.store import UserStore

            user = UserStore.create_user(username=_name("group-admin"), password="test12345", role=Role.OPERATOR)
            group = GroupStore.create_group(_name("group"))
            GroupStore.add_member(group.id, user.id, GroupRole.GROUP_ADMIN)

            lexicon = CentralLexiconStore.create_lexicon(user, name="Lexique groupe", group_id=group.id)

            assert lexicon.group_id == group.id
            assert CentralLexiconStore.can_manage_lexicon(user, lexicon)

    def test_group_member_cannot_manage_group_lexicon(self, app):
        with app.app_context():
            from transcria.auth.groups import GroupStore
            from transcria.auth.models import GroupRole
            from transcria.auth.models import Role
            from transcria.auth.store import UserStore

            admin = UserStore.get_by_username("admin")
            member = UserStore.create_user(username=_name("member"), password="test12345", role=Role.OPERATOR)
            group = GroupStore.create_group(_name("group"))
            GroupStore.add_member(group.id, member.id, GroupRole.MEMBER)
            lexicon = CentralLexiconStore.create_lexicon(admin, name="Lexique groupe", group_id=group.id)

            assert not CentralLexiconStore.can_manage_lexicon(member, lexicon)
            try:
                CentralLexiconStore.add_or_update_entry(lexicon, member, term="DNS")
            except CentralLexiconAccessError:
                pass
            else:
                raise AssertionError("CentralLexiconAccessError attendu")

    def test_rejects_duplicate_term_case_insensitive(self, app):
        with app.app_context():
            from transcria.auth.groups import GroupStore
            from transcria.auth.store import UserStore

            admin = UserStore.get_by_username("admin")
            group = GroupStore.create_group(_name("group"))
            lexicon = CentralLexiconStore.create_lexicon(admin, name="Lexique", group_id=group.id)
            CentralLexiconStore.add_or_update_entry(lexicon, admin, term="DNS")

            try:
                CentralLexiconStore.add_or_update_entry(lexicon, admin, term="dns")
            except CentralLexiconValidationError as exc:
                assert "existe déjà" in str(exc)
            else:
                raise AssertionError("CentralLexiconValidationError attendu")

    def test_import_entries_uses_mot_suspect_fallback(self, app):
        with app.app_context():
            from transcria.auth.groups import GroupStore
            from transcria.auth.store import UserStore

            admin = UserStore.get_by_username("admin")
            group = GroupStore.create_group(_name("group"))
            lexicon = CentralLexiconStore.create_lexicon(admin, name="Lexique", group_id=group.id)

            result = CentralLexiconStore.import_entries(lexicon, admin, "Terme seul\nAPI,sigle,critique")

            assert result == {"imported": 2, "rejected": 0}
            entries = GroupLexiconEntry.query.filter_by(lexicon_id=lexicon.id).order_by(GroupLexiconEntry.term).all()
            assert [entry.term for entry in entries] == ["API", "Terme seul"]
            assert entries[1].category == "mot suspect"

    def test_accessible_lexicons_for_job_use_job_owner_groups(self, app):
        with app.app_context():
            from transcria.auth.groups import GroupStore
            from transcria.auth.models import GroupRole
            from transcria.auth.models import Role
            from transcria.auth.store import UserStore
            from transcria.jobs.store import JobStore

            admin = UserStore.get_by_username("admin")
            owner = UserStore.create_user(username=_name("owner"), password="test12345", role=Role.OPERATOR)
            viewer = UserStore.create_user(username=_name("viewer"), password="test12345", role=Role.OPERATOR)
            owner_group = GroupStore.create_group(_name("owner-group"))
            viewer_group = GroupStore.create_group(_name("viewer-group"))
            GroupStore.add_member(owner_group.id, owner.id, GroupRole.MEMBER)
            GroupStore.add_member(viewer_group.id, viewer.id, GroupRole.MEMBER)
            owner_lexicon = CentralLexiconStore.create_lexicon(admin, name="Owner lexicon", group_id=owner_group.id)
            CentralLexiconStore.create_lexicon(admin, name="Viewer lexicon", group_id=viewer_group.id)
            global_lexicon = CentralLexiconStore.create_lexicon(admin, name="Global lexicon", group_id=None, allow_global=True)
            job = JobStore.create_job(owner_id=owner.id, title="job")

            lexicons = CentralLexiconStore.list_accessible_lexicons_for_job(job)
            ids = {item.id for item in lexicons}

            assert owner_lexicon.id in ids
            assert global_lexicon.id in ids
            assert not any(item.name == "Viewer lexicon" for item in lexicons)


class TestCentralLexiconService:
    def test_merge_keeps_session_as_authority(self):
        merged = merge_lexicon_entries(
            central_entries=[{"term": "API", "variants": ["à pieds"], "priority": "normale", "comment": "central"}],
            llm_suggestions=[{"term": "API", "variants": ["appy"], "priority": "critique", "comment": "llm"}],
            session_entries=[{"term": "API", "variants": ["a pi"], "priority": "importante", "comment": "session"}],
        )

        assert len(merged) == 1
        assert merged[0]["comment"] == "session"
        assert merged[0]["priority"] == "critique"
        assert merged[0]["variants"] == ["a pi", "à pieds", "appy"]
        assert merged[0]["source"] == "session"

    def test_merge_preserves_central_metadata_without_assigning_it_to_llm(self):
        merged = merge_lexicon_entries(
            central_entries=[{
                "id": "entry-1",
                "lexicon_id": "lex-1",
                "central_lexicon_name": "Lexique groupe",
                "term": "DNS",
            }],
            llm_suggestions=[{"id": "llm-1", "term": "API"}],
        )

        by_term = {entry["term"]: entry for entry in merged}
        assert by_term["DNS"]["central_entry_id"] == "entry-1"
        assert by_term["DNS"]["central_lexicon_id"] == "lex-1"
        assert by_term["DNS"]["central_lexicon_name"] == "Lexique groupe"
        assert by_term["API"]["central_entry_id"] == ""

    def test_filter_keeps_terms_and_variants_present_in_srt(self):
        lexicon = [
            {"term": "DNS", "variants": ["dénès"], "priority": "normale"},
            {"term": "API", "variants": ["à pieds"], "priority": "normale"},
            {"term": "SI", "variants": [], "priority": "critique"},
            {"term": "Absent", "variants": [], "priority": "normale"},
        ]

        filtered, meta = filter_lexicon_by_srt_presence(lexicon, "Le denes et API sont cités.")

        assert [item["term"] for item in filtered] == ["DNS", "API", "SI"]
        assert filtered[2]["_preservation_only"] is True
        assert meta["kept_by_variant_presence"] == 1
        assert meta["kept_by_term_presence"] == 1
        assert meta["kept_by_priority"] == 1
        assert meta["filtered_out"] == 1

    def test_filter_empty_lexicon(self):
        filtered, meta = filter_lexicon_by_srt_presence([], "texte")

        assert filtered == []
        assert meta["total"] == 0


class TestCentralLexiconWeb:
    def test_operator_cannot_access_lexicon_admin(self, operator_client):
        assert operator_client.get("/admin/lexicons").status_code == 403

    def test_admin_can_create_lexicon_and_entry_from_web(self, app, admin_client):
        with app.app_context():
            from transcria.auth.groups import GroupStore

            group = GroupStore.create_group(_name("web-lexicon-group"))
            group_id = group.id

        response = admin_client.post(
            "/admin/lexicons/new",
            data={
                "name": "Lexique web",
                "description": "Termes UI",
                "group_id": group_id,
            },
            follow_redirects=True,
        )

        assert response.status_code == 200
        assert b"Lexique web" in response.data
        with app.app_context():
            from transcria.context.central_lexicon_models import GroupLexicon

            lexicon = GroupLexicon.query.filter_by(name="Lexique web").one()
            lexicon_id = lexicon.id

        response = admin_client.post(
            f"/admin/lexicons/{lexicon_id}/entries",
            data={
                "term": "DNS",
                "variants": "dénès; D.N.S.",
                "category": "sigle",
                "priority": "critique",
                "comment": "Domain Name System",
            },
            follow_redirects=True,
        )

        assert response.status_code == 200
        assert b"DNS" in response.data
        with app.app_context():
            entry = GroupLexiconEntry.query.filter_by(lexicon_id=lexicon_id, term="DNS").one()
            assert entry.variants == ["dénès", "D.N.S."]
            assert entry.priority == "critique"

    def test_group_admin_can_manage_own_group_lexicon(self, app):
        with app.app_context():
            from transcria.auth.groups import GroupStore
            from transcria.auth.models import GroupRole
            from transcria.auth.models import Role
            from transcria.auth.store import UserStore

            user = UserStore.create_user(username=_name("web-group-admin"), password="test12345", role=Role.OPERATOR)
            group = GroupStore.create_group(_name("web-group"))
            GroupStore.add_member(group.id, user.id, GroupRole.GROUP_ADMIN)
            username = user.username
            group_id = group.id

        client = app.test_client()
        client.post("/login", data={"username": username, "password": "test12345"}, follow_redirects=True)
        response = client.post(
            "/admin/lexicons/new",
            data={"name": "Lexique admin groupe", "group_id": group_id},
            follow_redirects=True,
        )

        assert response.status_code == 200
        assert b"Lexique admin groupe" in response.data

    def test_available_lexicons_api_uses_job_owner_scope(self, app, admin_client):
        with app.app_context():
            from transcria.auth.groups import GroupStore
            from transcria.auth.models import GroupRole
            from transcria.auth.models import Role
            from transcria.auth.store import UserStore
            from transcria.jobs.store import JobStore

            admin = UserStore.get_by_username("admin")
            owner = UserStore.create_user(username=_name("api-owner"), password="test12345", role=Role.OPERATOR)
            owner_group = GroupStore.create_group(_name("api-owner-group"))
            other_group = GroupStore.create_group(_name("api-other-group"))
            GroupStore.add_member(owner_group.id, owner.id, GroupRole.MEMBER)

            owner_lexicon = CentralLexiconStore.create_lexicon(admin, name="Lexique owner API", group_id=owner_group.id)
            CentralLexiconStore.add_or_update_entry(owner_lexicon, admin, term="DNS", variants=["dénès"])
            CentralLexiconStore.create_lexicon(admin, name="Lexique autre API", group_id=other_group.id)
            job = JobStore.create_job(owner_id=owner.id, title="Job lexique")
            job_id = job.id

        response = admin_client.get(f"/api/jobs/{job_id}/available-lexicons")

        assert response.status_code == 200
        payload = response.get_json()
        names = [item["name"] for item in payload["lexicons"]]
        assert "Lexique owner API" in names
        assert "Lexique autre API" not in names
        owner_payload = next(item for item in payload["lexicons"] if item["name"] == "Lexique owner API")
        assert owner_payload["entries"][0]["term"] == "DNS"

    def test_save_session_lexicon_marks_central_entry_used(self, app, admin_client):
        with app.app_context():
            from transcria.auth.groups import GroupStore
            from transcria.auth.store import UserStore
            from transcria.jobs.store import JobStore

            admin = UserStore.get_by_username("admin")
            group = GroupStore.create_group(_name("usage-group"))
            lexicon = CentralLexiconStore.create_lexicon(admin, name="Lexique usage", group_id=group.id)
            entry = CentralLexiconStore.add_or_update_entry(lexicon, admin, term="DNS")
            entry_id = entry.id
            lexicon_id = lexicon.id
            job = JobStore.create_job(owner_id=admin.id, title="Job usage")
            job_id = job.id

        response = admin_client.post(
            f"/api/jobs/{job_id}/lexicon",
            json=[{
                "term": "DNS",
                "category": "sigle",
                "priority": "normale",
                "source": "central",
                "central_entry_id": entry_id,
                "central_lexicon_id": lexicon_id,
                "central_lexicon_name": "Lexique usage",
            }],
        )

        assert response.status_code == 200
        with app.app_context():
            from transcria.database import db

            updated = db.session.get(GroupLexiconEntry, entry_id)
            assert updated.usage_count == 1
