import json
import uuid

from transcria.context.central_lexicon_models import GroupLexiconEntry
from transcria.context.central_lexicon_service import (
    filter_lexicon_by_srt_presence,
    merge_lexicon_entries,
    prefilter_lexicon_entries_for_display,
)
from transcria.context.central_lexicon_store import CentralLexiconAccessError, CentralLexiconStore, CentralLexiconValidationError
from transcria.context.lexicon import LexiconManager
from transcria.context.lexicon_audit import lexicon_entries_audit_summary, looks_like_person_name


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
            from transcria.auth.models import GroupRole, Role
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
            from transcria.auth.models import GroupRole, Role
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
            from transcria.auth.models import GroupRole, Role
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
            from transcria.auth.models import GroupRole, Role
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

    def test_usage_stats_reports_top_entries_and_unused_entries(self, app):
        with app.app_context():
            from transcria.auth.groups import GroupStore
            from transcria.auth.store import UserStore

            admin = UserStore.get_by_username("admin")
            group = GroupStore.create_group(_name("stats-group"))
            lexicon = CentralLexiconStore.create_lexicon(admin, name="Lexique stats", group_id=group.id)
            used_once = CentralLexiconStore.add_or_update_entry(lexicon, admin, term="Terme utilisé")
            unused = CentralLexiconStore.add_or_update_entry(lexicon, admin, term="Terme jamais utilisé")

            CentralLexiconStore.mark_entries_used([used_once.id])
            stats = CentralLexiconStore.usage_stats(lexicon)

            assert stats["entry_count"] == 2
            assert stats["total_usage"] == 1
            assert stats["used_count"] == 1
            assert stats["never_used_count"] == 1
            assert stats["last_used_at"] is not None
            assert stats["top_entries"][0].id == used_once.id
            assert stats["never_used_entries"][0].id == unused.id

    def test_quality_issues_report_risky_entries(self, app):
        with app.app_context():
            from transcria.auth.groups import GroupStore
            from transcria.auth.store import UserStore

            admin = UserStore.get_by_username("admin")
            group = GroupStore.create_group(_name("quality-group"))
            lexicon = CentralLexiconStore.create_lexicon(admin, name="Lexique qualité", group_id=group.id)
            CentralLexiconStore.add_or_update_entry(lexicon, admin, term="SI", priority="normale")
            CentralLexiconStore.add_or_update_entry(lexicon, admin, term="École", variants=["ecole"])
            CentralLexiconStore.add_or_update_entry(lexicon, admin, term="ecole", priority="importante")

            messages = [issue["message"] for issue in CentralLexiconStore.quality_issues(lexicon)]

            assert any("Terme très court" in message for message in messages)
            assert any("Variante identique" in message for message in messages)
            assert any("Doublons proches" in message for message in messages)


class TestCentralLexiconService:
    def test_lexicon_audit_summary_flags_person_names_without_raw_terms(self):
        entries = [{
            "term": "Dr Dupont",
            "variants": ["Docteur Dupont"],
            "category": "personne",
            "priority": "critique",
            "source": "manual",
        }]

        summary = lexicon_entries_audit_summary(entries)
        encoded = json.dumps(summary, ensure_ascii=False)

        assert looks_like_person_name("Dr Dupont")
        assert summary["contains_probable_person_names"] is True
        assert summary["probable_person_name_count"] == 1
        assert summary["raw_terms_logged"] is False
        assert "Dupont" not in encoded

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

    def test_prefilter_display_keeps_present_terms_variants_and_priorities(self):
        entries = [
            {"term": "DNS", "variants": ["dénès"], "priority": "normale"},
            {"term": "API", "variants": [], "priority": "normale"},
            {"term": "Terme critique", "variants": [], "priority": "critique"},
            {"term": "Absent normal", "variants": [], "priority": "normale"},
        ]

        filtered, meta = prefilter_lexicon_entries_for_display(entries, "Le denes et API sont cités.")

        assert [entry["term"] for entry in filtered] == ["Terme critique", "API", "DNS"]
        assert meta["kept_by_variant_presence"] == 1
        assert meta["kept_by_term_presence"] == 1
        assert meta["kept_by_priority"] == 1
        assert meta["hidden"] == 1
        by_term = {entry["term"]: entry for entry in filtered}
        assert by_term["Terme critique"]["_display_reason"] == "priority"
        assert by_term["API"]["_display_reason"] == "term_presence"
        assert by_term["DNS"]["_display_reason"] == "variant_presence"

    def test_prefilter_display_limits_central_entries(self):
        entries = [
            {"term": f"Critique {i:03d}", "variants": [], "priority": "critique"}
            for i in range(6)
        ]

        filtered, meta = prefilter_lexicon_entries_for_display(entries, "", max_entries=3)

        assert len(filtered) == 3
        assert meta["limited_out"] == 3
        assert meta["hidden"] == 3

    def test_merge_preserves_display_reason_for_central_entries(self):
        merged = merge_lexicon_entries(
            central_entries=[{
                "term": "DNS",
                "source": "central",
                "central_entry_id": "entry-1",
                "central_lexicon_id": "lexicon-1",
                "central_lexicon_name": "Lexique",
                "_display_reason": "variant_presence",
            }],
            llm_suggestions=[],
        )

        assert merged[0]["_display_reason"] == "variant_presence"

    def test_session_lexicon_save_preserves_display_reason(self, app):
        with app.app_context():
            from transcria.auth.store import UserStore
            from transcria.config import get_config
            from transcria.jobs.store import JobStore

            admin = UserStore.get_by_username("admin")
            job = JobStore.create_job(owner_id=admin.id, title="Job lexique raison")
            saved = LexiconManager.save(job, get_config()["storage"]["jobs_dir"], [{
                "term": "DNS",
                "source": "central",
                "central_entry_id": "entry-1",
                "central_lexicon_id": "lexicon-1",
                "central_lexicon_name": "Lexique",
                "_display_reason": "variant_presence",
            }])

            assert saved[0]["_display_reason"] == "variant_presence"


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

    def test_lexicon_term_audit_does_not_store_raw_term(self, app, admin_client):
        with app.app_context():
            from transcria.audit.models import AuditAction, AuditLog
            from transcria.auth.groups import GroupStore

            group = GroupStore.create_group(_name("audit-lexicon-group"))
            group_id = group.id

        admin_client.post(
            "/admin/lexicons/new",
            data={"name": "Lexique audit", "group_id": group_id},
            follow_redirects=True,
        )
        with app.app_context():
            from transcria.context.central_lexicon_models import GroupLexicon

            lexicon = GroupLexicon.query.filter_by(name="Lexique audit").one()
            lexicon_id = lexicon.id

        response = admin_client.post(
            f"/admin/lexicons/{lexicon_id}/entries",
            data={"term": "Dr Dupont", "category": "personne", "priority": "critique"},
            follow_redirects=True,
        )

        assert response.status_code == 200
        with app.app_context():
            row = AuditLog.query.filter_by(action=AuditAction.LEXICON_TERM_ADD.value).order_by(AuditLog.timestamp.desc()).first()
            assert row is not None
            details = json.loads(row.details_json)
            assert details["contains_probable_person_names"] is True
            assert details["raw_terms_logged"] is False
            assert "Dupont" not in row.details_json

    def test_lexicon_export_is_audited_without_raw_terms(self, app, admin_client):
        with app.app_context():
            from transcria.audit.models import AuditAction, AuditLog
            from transcria.auth.groups import GroupStore
            from transcria.auth.store import UserStore

            admin = UserStore.get_by_username("admin")
            group = GroupStore.create_group(_name("export-lexicon-group"))
            lexicon = CentralLexiconStore.create_lexicon(admin, name="Lexique export", group_id=group.id)
            CentralLexiconStore.add_or_update_entry(lexicon, admin, term="Mme Martin", category="personne")
            lexicon_id = lexicon.id

        response = admin_client.post(f"/admin/lexicons/{lexicon_id}/export.csv")

        assert response.status_code == 200
        assert b"Mme Martin" in response.data
        with app.app_context():
            row = AuditLog.query.filter_by(action=AuditAction.LEXICON_EXPORT.value).order_by(AuditLog.timestamp.desc()).first()
            assert row is not None
            assert "Martin" not in row.details_json
            details = json.loads(row.details_json)
            assert details["format"] == "csv"
            assert details["contains_probable_person_names"] is True

    def test_lexicon_export_can_be_restricted_to_global_admins(self, app):
        with app.app_context():
            from transcria.auth.groups import GroupStore
            from transcria.auth.models import GroupRole, Role
            from transcria.auth.store import UserStore
            from transcria.config import get_config, set_config

            cfg = get_config()
            original_security = dict(cfg.get("security", {}))
            cfg["security"]["lexicon_export_admin_only"] = True
            set_config(cfg)

            user = UserStore.create_user(username=_name("export-group-admin"), password="test12345", role=Role.OPERATOR)
            group = GroupStore.create_group(_name("export-restricted-group"))
            GroupStore.add_member(group.id, user.id, GroupRole.GROUP_ADMIN)
            lexicon = CentralLexiconStore.create_lexicon(user, name="Lexique export restreint", group_id=group.id)
            lexicon_id = lexicon.id
            username = user.username

        try:
            client = app.test_client()
            client.post("/login", data={"username": username, "password": "test12345"}, follow_redirects=True)
            response = client.post(f"/admin/lexicons/{lexicon_id}/export.csv")

            assert response.status_code == 403
        finally:
            with app.app_context():
                from transcria.config import get_config, set_config

                cfg = get_config()
                cfg["security"] = original_security
                set_config(cfg)

    def test_group_admin_can_manage_own_group_lexicon(self, app):
        with app.app_context():
            from transcria.auth.groups import GroupStore
            from transcria.auth.models import GroupRole, Role
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
            from transcria.auth.models import GroupRole, Role
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

            from transcria.audit.models import AuditAction, AuditLog
            row = AuditLog.query.filter_by(action=AuditAction.JOB_LEXICON_SAVE.value).order_by(AuditLog.timestamp.desc()).first()
            assert row is not None
            details = json.loads(row.details_json)
            assert details["term_count"] == 1
            assert details["central_entry_count"] == 1
            assert details["raw_terms_logged"] is False
            assert "DNS" not in row.details_json

        list_response = admin_client.get("/admin/lexicons")
        assert list_response.status_code == 200
        assert b"Utilisations" in list_response.data
        detail_response = admin_client.get(f"/admin/lexicons/{lexicon_id}")
        assert detail_response.status_code == 200
        assert b"1 utilisation" in detail_response.data
        assert b"Utilis" in detail_response.data
        assert "Traçabilité".encode() in detail_response.data

    def test_lexicon_detail_shows_quality_issues(self, app, admin_client):
        with app.app_context():
            from transcria.auth.groups import GroupStore
            from transcria.auth.store import UserStore

            admin = UserStore.get_by_username("admin")
            group = GroupStore.create_group(_name("web-quality-group"))
            lexicon = CentralLexiconStore.create_lexicon(admin, name="Lexique qualité web", group_id=group.id)
            CentralLexiconStore.add_or_update_entry(lexicon, admin, term="SI")
            lexicon_id = lexicon.id

        response = admin_client.get(f"/admin/lexicons/{lexicon_id}")

        assert response.status_code == 200
        assert "Contrôles qualité".encode() in response.data
        assert "Terme très court".encode() in response.data

    def test_selected_lexicons_api_filters_step_prefill(self, app, admin_client):
        with app.app_context():
            from transcria.auth.store import UserStore
            from transcria.config import get_config
            from transcria.jobs.filesystem import JobFilesystem
            from transcria.jobs.models import JobState
            from transcria.jobs.store import JobStore

            admin = UserStore.get_by_username("admin")
            first = CentralLexiconStore.create_lexicon(admin, name="Lexique sélectionné", group_id=None, allow_global=True)
            second = CentralLexiconStore.create_lexicon(admin, name="Lexique ignoré", group_id=None, allow_global=True)
            CentralLexiconStore.add_or_update_entry(first, admin, term="Terme visible", priority="critique")
            CentralLexiconStore.add_or_update_entry(second, admin, term="Terme caché", priority="critique")
            job = JobStore.create_job(owner_id=admin.id, title="Job sélection")
            JobStore.update_state(job.id, JobState.PARTICIPANTS_DONE)
            jobs_dir = get_config()["storage"]["jobs_dir"]
            fs = JobFilesystem(jobs_dir, job.id)
            fs.save_text("summary/quick_transcript.txt", "Terme visible")
            job_id = job.id
            first_id = first.id

        response = admin_client.post(
            f"/api/jobs/{job_id}/selected-lexicons",
            json={"selected_lexicon_ids": [first_id, "inaccessible"]},
        )

        assert response.status_code == 200
        assert response.get_json()["selected_lexicon_ids"] == [first_id]
        page = admin_client.get(f"/jobs/{job_id}")
        assert page.status_code == 200
        assert "Lexique sélectionné".encode() in page.data
        assert "Lexique ignoré".encode() in page.data
        assert b"Terme visible" in page.data
        assert b"Terme cach" not in page.data

        with app.app_context():
            from transcria.config import get_config

            fs = JobFilesystem(get_config()["storage"]["jobs_dir"], job_id)
            stored = fs.load_json("context/selected_lexicons.json")
            assert stored["selected_lexicon_ids"] == [first_id]
