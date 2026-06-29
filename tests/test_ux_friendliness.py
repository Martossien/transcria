"""Convivialité (« user-friendly / easy ») — invariants assertables sans GPU ni œil humain.

L'oracle n'est jamais « ça a l'air clair » mais « un test vérifie une propriété de
convivialité » : pages d'erreur localisées (pas de page Werkzeug brute en anglais),
langue déclarée, titres d'onglet distincts, navigation sans cul-de-sac (aucun lien de
menu en 404), formulaires étiquetés (accessibilité/lisibilité), états vides guidés, et
absence de marqueurs de développement (TODO/lorem) dans le rendu.

Tout passe par le client de test Flask (déterministe, rapide, dans le gate de couverture).
Le volet « live » (navigateur réel, 404 cliquable, console JS) est dans scripts/ui_walkthrough.py.
"""
import re
from pathlib import Path

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "transcria" / "web" / "templates"

# Pages authentifiées « admin » couvertes par les invariants transverses.
ADMIN_PAGES = [
    "/",
    "/admin/users",
    "/admin/groups",
    "/admin/queue",
    "/admin/lexicons",
    "/admin/voices",
    "/admin/audit",
    "/admin/schedule",
    "/admin/config",
    "/system",
    "/account/password",
]

# Marqueurs trahissant une page brute Werkzeug (anglais/technique) ou du code non fini.
WERKZEUG_LEAKS = ["Not Found", "Forbidden", "Method Not Allowed", "Internal Server Error", "Werkzeug"]
DEV_MARKERS = ["TODO", "FIXME", "XXX", "lorem ipsum", "Lorem ipsum", "PLACEHOLDER", "À FAIRE"]


def _title(html: str) -> str:
    m = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    return m.group(1).strip() if m else ""


class TestFriendlyErrorPages:
    def test_404_renders_localized_page_not_raw_werkzeug(self, admin_client):
        r = admin_client.get("/cette-page-nexiste-pas")
        body = r.get_data(as_text=True)
        assert r.status_code == 404
        assert "introuvable" in body.lower()
        assert 'href="/"' in body  # chemin de sortie offert
        assert not any(leak in body for leak in WERKZEUG_LEAKS)

    def test_404_friendly_even_when_anonymous(self, client):
        # Une route inexistante 404 avant toute logique d'auth : la page reste conviviale.
        r = client.get("/route-inconnue-xyz")
        assert r.status_code == 404
        assert "introuvable" in r.get_data(as_text=True).lower()

    def test_403_renders_localized_page(self, operator_client):
        # RBAC : un opérateur sur une page admin → 403 convivial, pas « Forbidden ».
        r = operator_client.get("/admin/users")
        body = r.get_data(as_text=True)
        assert r.status_code == 403
        assert "accès refusé" in body.lower()
        assert "Forbidden" not in body

    def test_405_renders_localized_page(self, admin_client):
        # /logout est POST-only → un GET déclenche 405 ; doit rester localisé.
        r = admin_client.get("/logout")
        assert r.status_code == 405
        assert "Method Not Allowed" not in r.get_data(as_text=True)

    def test_api_errors_stay_json_not_html(self, admin_client):
        # Le front parse du JSON : une route /api/ inexistante ne doit JAMAIS renvoyer du HTML.
        r = admin_client.get("/api/route-inexistante")
        assert r.status_code == 404
        assert r.is_json
        assert r.get_json()["code"] == 404


class TestPageHygiene:
    def test_every_page_declares_french_lang(self, admin_client):
        for path in ADMIN_PAGES:
            body = admin_client.get(path).get_data(as_text=True)
            assert '<html lang="fr">' in body, f"{path} ne déclare pas lang=fr"

    def test_every_page_has_distinct_meaningful_title(self, admin_client):
        # Un onglet de navigateur lisible : chaque page a un <title> propre, pas le défaut nu.
        titles = {}
        for path in ADMIN_PAGES:
            body = admin_client.get(path).get_data(as_text=True)
            title = _title(body)
            assert title, f"{path} n'a pas de <title>"
            assert title != "TranscrIA", f"{path} garde le titre par défaut (onglet ambigu)"
            titles[path] = title
        # Titres distincts → l'utilisateur distingue les onglets ouverts.
        assert len(set(titles.values())) == len(titles), f"titres dupliqués : {titles}"

    def test_no_dev_markers_in_rendered_pages(self, admin_client):
        for path in ADMIN_PAGES:
            body = admin_client.get(path).get_data(as_text=True)
            for marker in DEV_MARKERS:
                assert marker not in body, f"marqueur de dev « {marker} » visible sur {path}"


class TestNavigationNoDeadEnds:
    def test_all_navbar_links_resolve(self, admin_client):
        # Aucun lien du MENU ne doit mener à un 404/500 (frustration = cul-de-sac).
        # On borne l'extraction au <nav>…</nav> : le corps de page contient aussi des
        # liens propres aux données (jobs accumulés) hors périmètre de cet invariant.
        body = admin_client.get("/").get_data(as_text=True)
        nav = re.search(r"<nav\b.*?</nav>", body, re.DOTALL | re.IGNORECASE)
        assert nav, "navbar absente (admin non authentifié ?)"
        hrefs = set(re.findall(r'href="(/[^"#?]*)"', nav.group(0)))
        hrefs = {h for h in hrefs if not h.startswith("/static") and not h.startswith("/logout")}
        assert hrefs, "aucun lien interne détecté dans la navbar"
        for href in sorted(hrefs):
            status = admin_client.get(href).status_code
            assert status < 400, f"lien de menu cassé : {href} → {status}"


class TestEmptyStatesGuide:
    def test_index_empty_state_invites_first_action(self, app):
        # Premier run : pas de job → message d'accueil qui guide, pas une page blanche.
        # Un utilisateur NEUF (non-admin → ne voit que ses jobs, ici aucun) garantit
        # l'état vide indépendamment des données accumulées par les autres tests.
        import uuid

        uname = f"empty_state_{uuid.uuid4().hex[:8]}"
        with app.app_context():
            from transcria.auth.models import Role
            from transcria.auth.store import UserStore
            UserStore.create_user(username=uname, password="emptypass123", role=Role.OPERATOR)

        c = app.test_client()
        c.post("/login", data={"username": uname, "password": "emptypass123"}, follow_redirects=True)
        body = c.get("/").get_data(as_text=True)
        assert "Aucun traitement" in body
        assert "premier traitement" in body.lower()


class TestFormsAreLabeled:
    """Chaque champ saisissable (hors caché/bouton/CSRF) doit avoir un <label>
    associé : lisibilité + accessibilité (lecteur d'écran, clic sur le libellé)."""

    # Pages-formulaires « simples » dont on garantit l'étiquetage complet.
    FORM_PAGES = [
        ("/login", "client"),
        ("/account/password", "admin_client"),
        ("/admin/users/new", "admin_client"),
        ("/admin/voices/new", "admin_client"),
    ]

    def _labeled_field_ids(self, html: str) -> tuple[set[str], set[str]]:
        label_for = set(re.findall(r'<label[^>]*\bfor="([^"]+)"', html))
        field_ids = set()
        for tag in re.findall(r"<(?:input|select|textarea)\b[^>]*>", html, re.IGNORECASE):
            if re.search(r'type="(hidden|submit|button)"', tag):
                continue
            m = re.search(r'\bid="([^"]+)"', tag)
            if m:
                field_ids.add(m.group(1))
        return field_ids, label_for

    def test_form_fields_have_associated_labels(self, request):
        for path, fixture_name in self.FORM_PAGES:
            client = request.getfixturevalue(fixture_name)
            body = client.get(path).get_data(as_text=True)
            field_ids, label_for = self._labeled_field_ids(body)
            unlabeled = field_ids - label_for
            assert not unlabeled, f"{path} : champs sans <label for> : {unlabeled}"


class TestFormValidationFeedback:
    """« Easy » = quand l'utilisateur se trompe, l'app le GUIDE (message français,
    re-rendu du formulaire, statut clair) au lieu de planter ou de rester muette."""

    def test_missing_required_fields_are_explained(self, admin_client):
        r = admin_client.post("/admin/users/new", data={"username": "x_no_pwd"}, follow_redirects=True)
        body = r.get_data(as_text=True)
        assert r.status_code == 200  # re-rendu du formulaire, pas une 500
        assert "obligatoires" in body.lower()

    def test_duplicate_username_is_explained(self, admin_client):
        import uuid

        uname = f"dup_{uuid.uuid4().hex[:8]}"
        data = {"username": uname, "password": "strongpass1", "password_confirm": "strongpass1", "role": "operator"}
        admin_client.post("/admin/users/new", data=data, follow_redirects=True)
        r = admin_client.post("/admin/users/new", data=data, follow_redirects=True)
        assert "existe déjà" in r.get_data(as_text=True).lower()

    def test_password_mismatch_is_explained(self, admin_client):
        import uuid

        data = {
            "username": f"mm_{uuid.uuid4().hex[:8]}",
            "password": "strongpass1",
            "password_confirm": "different99",
            "role": "operator",
        }
        r = admin_client.post("/admin/users/new", data=data, follow_redirects=True)
        assert "ne correspond pas" in r.get_data(as_text=True).lower()

    def test_too_short_password_is_explained(self, admin_client):
        import uuid

        data = {
            "username": f"sh_{uuid.uuid4().hex[:8]}",
            "password": "x",
            "password_confirm": "x",
            "role": "operator",
        }
        r = admin_client.post("/admin/users/new", data=data, follow_redirects=True)
        assert "caractères" in r.get_data(as_text=True).lower()

    def test_group_empty_name_is_explained(self, admin_client):
        r = admin_client.post("/admin/groups/new", data={"name": ""}, follow_redirects=True)
        assert "obligatoire" in r.get_data(as_text=True).lower()


class TestTemplateAccessibility:
    """Garde statique (analyse des templates, sans app) : aucun contrôle interactif
    icône-seule ne doit être privé de nom accessible (lecteur d'écran). Un nom =
    `aria-label`, `title`, du texte visible, ou une expression Jinja qui rend du texte."""

    _CONTROL = re.compile(r'<(a|button)\b([^>]*)>(.*?)</\1>', re.DOTALL | re.IGNORECASE)

    @staticmethod
    def _has_accessible_name(attrs: str, inner: str) -> bool:
        if "aria-label" in attrs or "title=" in attrs:
            return True
        # Texte humain restant une fois les balises retirées.
        if re.sub(r"<[^>]+>", "", inner).strip():
            return True
        # Une expression Jinja {{ ... }} (hors url_for/tojson) rend du texte à l'exécution.
        for expr in re.findall(r"\{\{(.*?)\}\}", inner, re.DOTALL):
            if "url_for" not in expr and "tojson" not in expr:
                return True
        return False

    def test_no_unnamed_icon_only_controls(self):
        offenders = []
        for f in sorted(TEMPLATES_DIR.glob("*.html")):
            for m in self._CONTROL.finditer(f.read_text()):
                attrs, inner = m.group(2), m.group(3)
                is_icon_only = ("bi-" in inner or 'class="bi' in inner)
                if is_icon_only and not self._has_accessible_name(attrs, inner):
                    offenders.append(f"{f.name}: {m.group(0)[:80]}")
        assert not offenders, "contrôles icône-seule sans nom accessible :\n" + "\n".join(offenders)


class TestFrenchConsistency:
    """Garde statique : pas de libellé anglais résiduel dans le TEXTE visible des
    templates (l'UI est intégralement en français). Borné à un jeu de mots courants
    d'UI pour rester déterministe et sans faux positif (classes/attributs ignorés)."""

    LEFTOVER_EN = [
        "Loading", "Submit", "Cancel", "Delete", "Please", "Error", "Success",
        "Warning", "Failed", "Settings", "Username", "Password", "Logout", "Login",
        "Confirm", "Remove", "Back", "Next", "Close", "Save",
    ]

    @staticmethod
    def _visible_segments(html: str) -> list[str]:
        html = re.sub(r"<(script|style)\b.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
        segs = []
        for raw in re.findall(r">([^<>]+)<", html):
            seg = re.sub(r"\{[%{].*?[%}]\}", "", raw, flags=re.DOTALL).strip()
            if seg:
                segs.append(seg)
        return segs

    def test_no_residual_english_in_visible_text(self):
        offenders = []
        for f in sorted(TEMPLATES_DIR.glob("*.html")):
            for seg in self._visible_segments(f.read_text()):
                for word in self.LEFTOVER_EN:
                    if re.search(rf"\b{word}\b", seg):
                        offenders.append(f"{f.name}: « {seg[:60]} » ({word})")
                        break
        assert not offenders, "anglais résiduel dans le texte visible :\n" + "\n".join(offenders)


class TestDefaultPasswordOnboarding:
    """Premier run : un compte encore sur le mot de passe par défaut doit être
    invité à le changer (sécurité + clarté), et le bandeau disparaît une fois fait."""

    def _fresh_user(self, app, password):
        import uuid

        uname = f"onboard_{uuid.uuid4().hex[:8]}"
        with app.app_context():
            from transcria.auth.models import Role
            from transcria.auth.store import UserStore
            UserStore.create_user(username=uname, password=password, role=Role.OPERATOR)
        return uname

    def test_default_password_triggers_banner(self, app):
        uname = self._fresh_user(app, "admin-change-me")  # ∈ DEFAULT_ADMIN_PASSWORDS
        c = app.test_client()
        c.post("/login", data={"username": uname, "password": "admin-change-me"})
        body = c.get("/").get_data(as_text=True)
        assert "mot de passe par défaut" in body
        assert "/account/password" in body

    def test_strong_password_no_banner(self, app):
        uname = self._fresh_user(app, "verystrongpass1")
        c = app.test_client()
        c.post("/login", data={"username": uname, "password": "verystrongpass1"})
        body = c.get("/").get_data(as_text=True)
        assert "mot de passe par défaut" not in body

    def test_banner_cleared_after_password_change(self, app):
        uname = self._fresh_user(app, "admin-change-me")
        c = app.test_client()
        c.post("/login", data={"username": uname, "password": "admin-change-me"})
        assert "mot de passe par défaut" in c.get("/").get_data(as_text=True)
        c.post(
            "/account/password",
            data={"current_password": "admin-change-me", "new_password": "newstrong99", "confirm_password": "newstrong99"},
            follow_redirects=True,
        )
        assert "mot de passe par défaut" not in c.get("/").get_data(as_text=True)
