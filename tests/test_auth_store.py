
import logging
from pathlib import Path

import pytest

from transcria.auth.groups import GroupStore
from transcria.auth.models import GroupRole, Role, User
from transcria.auth.store import DEFAULT_ADMIN_PASSWORDS, UserStore
from transcria.database import db


def _empty_all_tables():
    """Vide toutes les tables dans l'ordre FK-safe (enfants → parents).

    Sous PostgreSQL les clés étrangères sont appliquées : on ne peut pas vider
    ``users`` sans purger d'abord ses tables filles (jobs, audit, voix…). Ces
    tests simulent une base vierge pour la création du premier admin.
    """
    for table in reversed(db.metadata.sorted_tables):
        db.session.execute(table.delete())
    db.session.commit()


_USER_FIELDS = ("id", "username", "display_name", "email", "password_hash", "role",
                "is_active", "created_at", "last_login")


def _snapshot_users():
    """Capture les users en valeurs simples (les objets ORM expirent après le wipe)."""
    return [{f: getattr(u, f) for f in _USER_FIELDS} for u in db.session.query(User).all()]


def _restore_users(snapshot):
    """Remet la base à l'état d'avant le test : table vide puis users restaurés."""
    _empty_all_tables()
    for data in snapshot:
        db.session.add(User(**data))
    db.session.commit()


class TestUserStore:
    def test_create_user(self, app):
        with app.app_context():
            user = UserStore.create_user(
                username="storetest", password="pass123", display_name="Test", role=Role.OPERATOR
            )
            assert user.username == "storetest"
            assert user.role_enum == Role.OPERATOR
            assert user.is_active is True

    def test_get_by_username(self, app):
        with app.app_context():
            user = UserStore.create_user(username="findme", password="pw")
            found = UserStore.get_by_username("findme")
            assert found is not None
            assert found.id == user.id

    def test_get_by_username_nonexistent(self, app):
        with app.app_context():
            assert UserStore.get_by_username("noone") is None

    def test_get_by_id(self, app):
        with app.app_context():
            user = UserStore.create_user(username="byid", password="pw")
            found = UserStore.get_by_id(user.id)
            assert found is not None
            assert found.username == "byid"

    def test_list_users(self, app):
        with app.app_context():
            users = UserStore.list_users()
            assert len(users) >= 1

    def test_update_user(self, app):
        with app.app_context():
            user = UserStore.create_user(username="updateme", password="pw")
            updated = UserStore.update_user(user.id, display_name="New Name", email="new@test.com")
            assert updated is not None
            assert updated.display_name == "New Name"
            assert updated.email == "new@test.com"

    def test_change_password(self, app):
        with app.app_context():
            user = UserStore.create_user(username="pwtest", password="old")
            assert user.check_password("old")
            success = UserStore.change_password(user.id, "newpass")
            assert success
            same_user = UserStore.get_by_id(user.id)
            assert same_user.check_password("newpass")
            assert not same_user.check_password("old")

    def test_deactivate_user(self, app):
        with app.app_context():
            user = UserStore.create_user(username="deact", password="pw")
            assert user.is_active
            success = UserStore.deactivate_user(user.id)
            assert success
            found = UserStore.get_by_id(user.id)
            assert not found.is_active

    def test_count_users(self, app):
        with app.app_context():
            c1 = UserStore.count_users()
            UserStore.create_user(username=f"count{c1}", password="pw")
            c2 = UserStore.count_users()
            assert c2 == c1 + 1

    def test_ensure_admin_creates_first_admin(self, app):
        with app.app_context():
            snapshot = _snapshot_users()
            _empty_all_tables()

            try:
                assert UserStore.count_users() == 0
                UserStore.ensure_admin({"auth": {"first_admin_username": "root", "first_admin_password": "rootpass"}})
                admin = UserStore.get_by_username("root")
                assert admin is not None
                assert admin.role_enum == Role.ADMIN
                assert admin.check_password("rootpass")
            finally:
                _restore_users(snapshot)

    def test_ensure_admin_noop_if_users_exist(self, app):
        with app.app_context():
            count = UserStore.count_users()
            assert count > 0
            UserStore.ensure_admin({"auth": {"first_admin_username": "x", "first_admin_password": "x"}})
            assert UserStore.get_by_username("x") is None

    def test_ensure_admin_sentinelle_ne_cree_rien_et_annonce_setup(self, app, caplog):
        with app.app_context():
            snapshot = _snapshot_users()
            _empty_all_tables()

            try:
                caplog.set_level("INFO", logger="transcria.auth.store")
                UserStore.ensure_admin(
                    {"auth": {"first_admin_username": "admin", "first_admin_password": "admin-change-me"}}
                )

                # Issue #11 v2 : sentinelle → AUCUN compte créé (plus de secret généré
                # à retrouver dans un journal) — le portail imposera /setup à la
                # première visite, et le log de démarrage l'annonce.
                assert UserStore.get_by_username("admin") is None
                assert "/setup" in caplog.text
            finally:
                _restore_users(snapshot)

    def test_ensure_admin_idempotent_sous_course_de_premier_boot(self, app, monkeypatch):
        """Course de PREMIER démarrage : plusieurs workers gunicorn passent tous le
        `count_users() == 0` puis tentent l'insertion en parallèle. Le perdant reçoit
        une violation d'unicité — ensure_admin doit l'ABSORBER (rollback + skip), sinon
        gunicorn (WORKER_BOOT_ERROR) refuse de démarrer le service (bug vécu au 1er boot
        du bundled)."""
        from transcria.auth.models import User

        with app.app_context():
            snapshot = _snapshot_users()
            _empty_all_tables()
            try:
                # Le worker « gagnant » a déjà créé l'admin.
                UserStore.ensure_admin({"auth": {"first_admin_username": "root", "first_admin_password": "rootpass"}})
                assert User.query.filter_by(username="root").count() == 1

                # Le worker « perdant » voit encore 0 (fenêtre de course) et retente
                # l'insertion → UniqueViolation, qui NE DOIT PAS remonter.
                monkeypatch.setattr(UserStore, "count_users", staticmethod(lambda: 0))
                UserStore.ensure_admin({"auth": {"first_admin_username": "root", "first_admin_password": "rootpass"}})
                monkeypatch.undo()

                # Aucune exception, toujours UN seul admin, session utilisable.
                assert User.query.filter_by(username="root").count() == 1
                assert UserStore.get_by_username("root") is not None
            finally:
                _restore_users(snapshot)


class TestGroupStore:
    def test_create_group_and_membership(self, app):
        with app.app_context():
            suffix = __import__("uuid").uuid4().hex[:8]
            user = UserStore.create_user(username=f"group_user_{suffix}", password="pw")
            group = GroupStore.create_group(f"Groupe {suffix}", "Description")

            membership = GroupStore.add_member(group.id, user.id, GroupRole.GROUP_ADMIN)

            assert membership is not None
            assert membership.role == GroupRole.GROUP_ADMIN.value
            assert group.id in GroupStore.user_group_ids(user.id)
            assert GroupStore.can_manage_group(user, group.id) is True

    def test_users_share_group(self, app):
        with app.app_context():
            suffix = __import__("uuid").uuid4().hex[:8]
            user_a = UserStore.create_user(username=f"group_a_{suffix}", password="pw")
            user_b = UserStore.create_user(username=f"group_b_{suffix}", password="pw")
            user_c = UserStore.create_user(username=f"group_c_{suffix}", password="pw")
            group = GroupStore.create_group(f"Partage {suffix}")

            GroupStore.add_member(group.id, user_a.id)
            GroupStore.add_member(group.id, user_b.id)

            assert GroupStore.users_share_group(user_a.id, user_b.id) is True
            assert GroupStore.users_share_group(user_a.id, user_c.id) is False


# --- Passe sécurité S1.4 + issue #11 v2 : plus de secret d'amorçage, ni publié ni généré -
#
# S1.4 : `config.example.yaml` publiait `first_admin_password: "CHANGE-ME"` et le compte
# était RÉELLEMENT créé avec — secret public sous identifiant connu. Premier correctif :
# générer un secret aléatoire journalisé une fois.
#
# Issue #11 v2 : le secret généré était indécouvrable en pratique (le 1er testeur externe
# a terminé son installation sans pouvoir se connecter — personne ne lit le journal au bon
# moment). Choix final : sur sentinelle, ensure_admin ne crée RIEN ; le portail impose la
# page /setup à la première visite et l'utilisateur choisit son mot de passe là où il ne
# peut pas le rater. Un mot de passe CONFIGURÉ reste respecté (chemin automatisation).

class TestAmorçageSansSecretPublie:
    @pytest.fixture()
    def base_vierge(self, app):
        """`ensure_admin` est un no-op dès qu'un compte existe : ces tests ont besoin
        d'une base réellement vide, puis la rendent telle qu'ils l'ont trouvée."""
        with app.app_context():
            snapshot = _snapshot_users()
            _empty_all_tables()
            try:
                yield
            finally:
                _restore_users(snapshot)

    def _admin(self, mdp):
        UserStore.ensure_admin({"auth": {"first_admin_username": "root",
                                         "first_admin_password": mdp}})
        return UserStore.get_by_username("root")

    @pytest.mark.parametrize("sentinelle", ["CHANGE-ME", "admin-change-me", ""])
    def test_une_sentinelle_ne_cree_AUCUN_compte(self, base_vierge, sentinelle):
        # v2 : encore plus fort que « jamais le mot de passe » — pas de compte du tout.
        assert self._admin(sentinelle) is None
        assert UserStore.count_users() == 0

    def test_aucun_secret_ne_fuit_dans_le_journal(self, base_vierge, caplog):
        with caplog.at_level(logging.INFO, logger="transcria.auth.store"):
            self._admin("CHANGE-ME")
            self._admin("un-vrai-secret-choisi")
        assert "mot de passe généré" not in caplog.text
        assert "un-vrai-secret-choisi" not in caplog.text

    def test_un_mot_de_passe_choisi_par_lexploitant_est_RESPECTE(self, base_vierge):
        """Contre-épreuve : qui configure vraiment un secret garde la main."""
        assert self._admin("un-vrai-secret-choisi").check_password("un-vrai-secret-choisi")


def test_lexemple_public_ne_contient_plus_de_mot_de_passe_utilisable():
    """Le dépôt est PUBLIC : `config.example.yaml` ne doit plus livrer de secret initial."""
    import yaml
    exemple = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
    assert not (exemple.get("auth", {}) or {}).get("first_admin_password")
