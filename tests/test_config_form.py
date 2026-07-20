"""Tests de l'éditeur de configuration à formulaires (config_form)."""

import pytest

from transcria.config.loader import get_default_config
from transcria.web.config_form import (
    CONFIG_FORM_SECTIONS,
    SECRET_SENTINEL,
    build_partial_config,
    coerce_value,
    display_values,
    get_dotted,
    restore_masked_secrets,
    secret_paths,
    set_dotted,
)

_ALL_FIELDS = [f for section in CONFIG_FORM_SECTIONS for f in section["fields"]]


@pytest.mark.parametrize("field", _ALL_FIELDS, ids=[f["path"] for f in _ALL_FIELDS])
def test_every_form_path_resolves_in_default_config(field):
    """Anti-dérive : chaque chemin de formulaire doit exister dans la config **par défaut**.

    On valide contre `get_default_config()` (pas `load_config()`) : déterministe partout,
    indépendant d'un `config.yaml` local — un chemin présent seulement dans une config de
    prod (mais absent des defaults) doit casser le test, comme sur une installation neuve.
    """
    cfg = get_default_config()
    sentinel = object()
    assert get_dotted(cfg, field["path"], sentinel) is not sentinel, field["path"]


def test_field_types_are_known():
    known = {"text", "int", "bool", "csv", "select", "password", "group_role_rules"}
    for field in _ALL_FIELDS:
        assert field["type"] in known
        if field["type"] == "select":
            assert field.get("options")


def test_get_dotted_and_set_dotted_roundtrip():
    d: dict = {}
    set_dotted(d, "a.b.c", 3)
    assert d == {"a": {"b": {"c": 3}}}
    assert get_dotted(d, "a.b.c") == 3
    assert get_dotted(d, "a.b.x", "def") == "def"
    assert get_dotted(d, "a.missing.deep", None) is None


def test_coerce_value_bool_int_csv():
    assert coerce_value({"type": "bool"}, "on") is True
    assert coerce_value({"type": "bool"}, None) is False
    assert coerce_value({"type": "int"}, "1024") == 1024
    assert coerce_value({"type": "int"}, "") is None
    assert coerce_value({"type": "csv"}, "mp3, wav , ,m4a") == ["mp3", "wav", "m4a"]
    assert coerce_value({"type": "text"}, "  hello ") == "hello"


def test_build_partial_config_nests_only_managed_fields():
    sections = [{
        "title": "T",
        "fields": [
            {"path": "workflow.execution.max_concurrent_jobs", "type": "int"},
            {"path": "workflow.queue.enabled", "type": "bool"},
            {"path": "security.allowed_upload_extensions", "type": "csv"},
        ],
    }]
    form = {
        "workflow.execution.max_concurrent_jobs": "3",
        # case décochée → absente du form → False
        "security.allowed_upload_extensions": "mp3,wav",
    }
    partial = build_partial_config(form, sections)
    assert partial["workflow"]["execution"]["max_concurrent_jobs"] == 3
    assert partial["workflow"]["queue"]["enabled"] is False
    assert partial["security"]["allowed_upload_extensions"] == ["mp3", "wav"]


def test_secret_paths_includes_admin_password():
    assert "auth.first_admin_password" in secret_paths(CONFIG_FORM_SECTIONS)


def test_display_values_masks_secrets():
    cfg = {"auth": {"first_admin_password": "s3cret"}, "server": {"host": "0.0.0.0"}}
    values = display_values(cfg, CONFIG_FORM_SECTIONS)
    assert values["auth.first_admin_password"] == SECRET_SENTINEL
    assert values["server.host"] == "0.0.0.0"


def test_restore_masked_secrets_keeps_current_when_sentinel():
    current = {"auth": {"first_admin_password": "real"}}
    submitted = {"auth": {"first_admin_password": SECRET_SENTINEL}}
    restored = restore_masked_secrets(submitted, current, CONFIG_FORM_SECTIONS)
    assert restored["auth"]["first_admin_password"] == "real"


def test_restore_masked_secrets_accepts_new_value():
    current = {"auth": {"first_admin_password": "real"}}
    submitted = {"auth": {"first_admin_password": "nouveau"}}
    restored = restore_masked_secrets(submitted, current, CONFIG_FORM_SECTIONS)
    assert restored["auth"]["first_admin_password"] == "nouveau"


def test_form_merge_preserves_unmanaged_keys():
    """Le dict partiel du formulaire fusionne sans écraser les clés non gérées."""
    from transcria.config import _deep_merge

    cfg = {
        "workflow": {"execution": {"max_concurrent_jobs": 1}, "transcription_cleanup": {"enabled": True}},
        "models": {"stt_backend": "cohere"},
    }
    partial = build_partial_config({"workflow.execution.max_concurrent_jobs": "4"}, [
        {"title": "T", "fields": [{"path": "workflow.execution.max_concurrent_jobs", "type": "int"}]},
    ])
    merged = _deep_merge(cfg, partial)
    assert merged["workflow"]["execution"]["max_concurrent_jobs"] == 4
    # clés non gérées intactes
    assert merged["workflow"]["transcription_cleanup"]["enabled"] is True
    assert merged["models"]["stt_backend"] == "cohere"


def test_admin_config_page_renders_form_fields(admin_client):
    resp = admin_client.get("/admin/config")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # sections et onglets présents
    assert "Notifications email" in body
    assert 'name="_mode"' in body
    assert 'name="models.stt_backend"' in body
    assert 'name="services.arbitrage_llm_port"' in body
    # le mot de passe admin est masqué (sentinelle), jamais en clair
    assert SECRET_SENTINEL in body


class TestSectionIdentiteSSO:
    """Chantier identité lot 1 : la section SSO du formulaire — dont le type
    `group_role_rules` (une règle « groupe = rôle » par ligne)."""

    _FIELD = {"type": "group_role_rules"}

    def test_coerce_regles_simples_et_dn_active_directory(self):
        # Découpage sur le DERNIER « = » : un DN AD contient des « = ».
        raw = ("transcria-admins = admin\n"
               "CN=Transcria Users,OU=Apps,DC=corp = operator\n"
               "\n"
               "# commentaire ignoré\n")
        assert coerce_value(self._FIELD, raw) == [
            {"group": "transcria-admins", "role": "admin"},
            {"group": "CN=Transcria Users,OU=Apps,DC=corp", "role": "operator"},
        ]

    def test_ligne_sans_egal_conservee_pour_la_validation(self):
        # Jamais de perte muette : la règle invalide part à la validation du
        # mapping (rôle vide → erreur affichée à la sauvegarde).
        assert coerce_value(self._FIELD, "groupe-sans-role") == [
            {"group": "groupe-sans-role", "role": ""}]

    def test_affichage_liste_vers_lignes(self):
        cfg = {"auth": {"role_mapping": {"rules": [
            {"group": "transcria-admins", "role": "admin"},
            {"group": "CN=A,OU=B", "role": "viewer"},
        ]}}}
        values = display_values(cfg, CONFIG_FORM_SECTIONS)
        assert values["auth.role_mapping.rules"] == (
            "transcria-admins = admin\nCN=A,OU=B = viewer")

    def test_aller_retour_affichage_coercition(self):
        rules = [{"group": "CN=Transcria Users,OU=Apps,DC=corp", "role": "operator"}]
        cfg = {"auth": {"role_mapping": {"rules": rules}}}
        shown = display_values(cfg, CONFIG_FORM_SECTIONS)["auth.role_mapping.rules"]
        assert coerce_value(self._FIELD, shown) == rules

    def test_secret_client_oidc_masque(self):
        assert "auth.oidc.client_secret" in secret_paths(CONFIG_FORM_SECTIONS)
        cfg = {"auth": {"oidc": {"client_secret": "s3cret"}}}
        assert display_values(cfg, CONFIG_FORM_SECTIONS)["auth.oidc.client_secret"] == SECRET_SENTINEL

    def test_page_admin_rend_la_section_sso(self, admin_client):
        body = admin_client.get("/admin/config").get_data(as_text=True)
        assert 'name="auth.backend"' in body
        assert 'name="auth.oidc.issuer"' in body
        assert 'name="auth.role_mapping.rules"' in body   # textarea des règles
        # Champs LDAP (lot 2) présents dans la même section, secret de service masqué.
        assert 'name="auth.ldap.servers"' in body
        assert 'name="auth.ldap.bind_mode"' in body
        assert 'name="auth.ldap.service_password"' in body

    def test_secret_service_ldap_masque(self):
        assert "auth.ldap.service_password" in secret_paths(CONFIG_FORM_SECTIONS)
        cfg = {"auth": {"ldap": {"service_password": "topsecret"}}}
        assert display_values(cfg, CONFIG_FORM_SECTIONS)["auth.ldap.service_password"] == SECRET_SENTINEL


class TestChampNullable:
    """Champ `nullable` (lot 2, summary_stt_backend) : l'option vide du select
    signifie EXPLICITEMENT null — écrite au save (retour au « comme le pipeline »),
    contrairement aux int vides qui signifient « ne pas toucher »."""

    def _field(self):
        from transcria.web.config_form import CONFIG_FORM_SECTIONS, iter_fields
        return next(f for f in iter_fields(CONFIG_FORM_SECTIONS)
                    if f["path"] == "models.summary_stt_backend")

    def test_le_champ_est_declare_nullable_avec_option_vide(self):
        field = self._field()
        assert field.get("nullable") is True
        assert "" in field["options"]
        assert "kroko" in field["options"] and "qwen3asr" in field["options"]

    def test_valeur_vide_coerce_en_none(self):
        from transcria.web.config_form import coerce_value
        assert coerce_value(self._field(), "") is None
        assert coerce_value(self._field(), "qwen3asr") == "qwen3asr"

    def test_le_save_ecrit_null_pour_revenir_au_defaut(self):
        from transcria.web.config_form import CONFIG_FORM_SECTIONS, build_partial_config
        partial = build_partial_config({"models.summary_stt_backend": ""}, CONFIG_FORM_SECTIONS)
        assert partial["models"]["summary_stt_backend"] is None

    def test_les_int_vides_restent_ignores(self):
        from transcria.web.config_form import CONFIG_FORM_SECTIONS, build_partial_config
        partial = build_partial_config({"server.port": ""}, CONFIG_FORM_SECTIONS)
        assert "server" not in partial or "port" not in partial.get("server", {})
