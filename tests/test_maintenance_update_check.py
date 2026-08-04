"""Détection de nouvelle version (transcria.maintenance.update_check).

Logique pure : comparaison de versions, normalisation de la réponse GitHub,
cache fichier + TTL, vue UI. AUCUN réseau : le fetcher est injecté partout.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from transcria.maintenance import update_check as uc


class TestParseVersion:
    def test_formes_usuelles(self):
        assert uc.parse_version("v0.4.0") == ((0, 4, 0), True)
        assert uc.parse_version("0.3.9.1") == ((0, 3, 9, 1), True)
        assert uc.parse_version("v0.1.0-beta.8") == ((0, 1, 0), False)

    def test_inanalysable(self):
        assert uc.parse_version("latest") is None
        assert uc.parse_version("") is None
        assert uc.parse_version("v0.4.x") is None


class TestIsNewer:
    @pytest.mark.parametrize(("candidate", "reference", "expected"), [
        ("v0.4.1", "0.4.0", True),
        ("v0.5.0", "0.4.9.9", True),
        ("v0.4.0", "0.4.0", False),          # égalité stricte → pas « plus récente »
        ("v0.3.9.1", "0.4.0", False),
        ("v0.4.0.1", "0.4.0", True),         # 4 composants contre 3 (padding)
        ("v0.4.0", "0.1.0-beta.8", True),
        ("v0.4.0-rc.1", "0.4.0", False),     # la finale bat sa pré-version
        ("v0.4.0", "0.4.0-rc.1", True),
    ])
    def test_comparaisons(self, candidate, reference, expected):
        assert uc.is_newer(candidate, reference) is expected

    def test_inanalysable_ne_signale_jamais(self):
        assert uc.is_newer("latest", "0.4.0") is False
        assert uc.is_newer("v0.5.0", "n/a") is False


class TestFetchLatestRelease:
    def test_normalise_la_reponse(self):
        payload = {"tag_name": "v0.5.0", "html_url": "https://example.test/rel",
                   "published_at": "2026-08-10T00:00:00Z",
                   "body": "## Quoi de neuf\n" + "ligne\n" * 100}
        release = uc.fetch_latest_release(lambda url: payload)
        assert release["tag"] == "v0.5.0"
        assert release["url"] == "https://example.test/rel"
        assert len(release["notes"].splitlines()) <= 30

    def test_tag_inattendu_leve(self):
        with pytest.raises(uc.UpdateCheckError, match="inattendue"):
            uc.fetch_latest_release(lambda url: {"tag_name": "latest"})

    def test_echec_reseau_devient_message_actionnable(self):
        def fetch(url):
            raise OSError("connexion coupée")
        with pytest.raises(uc.UpdateCheckError, match="joindre l'API GitHub"):
            uc.fetch_latest_release(fetch)


class TestCache:
    def _cfg(self, tmp_path):
        return {"maintenance": {"backup_dir": str(tmp_path / "backups")}}

    def test_check_ecrit_le_cache_et_read_le_relit(self, tmp_path):
        cfg = self._cfg(tmp_path)
        release = uc.check_for_update(
            cfg, fetch=lambda url: {"tag_name": "v0.5.0", "html_url": "u", "body": ""},
            now_fn=lambda: 1_000_000.0)
        assert release["tag"] == "v0.5.0"
        cached = uc.read_cache(uc.cache_path(cfg))
        assert cached is not None and cached["tag"] == "v0.5.0"
        assert cached["checked_at"] == datetime.fromtimestamp(1_000_000.0, tz=UTC).isoformat()

    def test_cache_corrompu_ou_absent_rend_none(self, tmp_path):
        path = tmp_path / "update-check.json"
        assert uc.read_cache(path) is None
        path.write_text("{pas du json", encoding="utf-8")
        assert uc.read_cache(path) is None
        path.write_text(json.dumps({"tag": "latest"}), encoding="utf-8")
        assert uc.read_cache(path) is None

    def test_is_stale_selon_ttl(self):
        now = 2_000_000.0
        fresh = {"tag": "v0.5.0", "checked_at": datetime.fromtimestamp(now - 60, tz=UTC).isoformat()}
        old = {"tag": "v0.5.0",
               "checked_at": datetime.fromtimestamp(now - uc.CACHE_TTL_S - 1, tz=UTC).isoformat()}
        assert uc.is_stale(fresh, now_fn=lambda: now) is False
        assert uc.is_stale(old, now_fn=lambda: now) is True
        assert uc.is_stale(None, now_fn=lambda: now) is True
        assert uc.is_stale({"tag": "v0.5.0"}, now_fn=lambda: now) is True

    def test_cache_inecrivable_ne_masque_pas_le_resultat(self, tmp_path):
        blocking = tmp_path / "fichier"
        blocking.write_text("", encoding="utf-8")
        cfg = {"maintenance": {"backup_dir": str(blocking / "impossible")}}
        release = uc.check_for_update(
            cfg, fetch=lambda url: {"tag_name": "v0.5.0", "html_url": "u", "body": ""})
        assert release["tag"] == "v0.5.0"


class TestSummarize:
    def test_sans_cache(self):
        view = uc.summarize(None, "0.4.0")
        assert view["current"] == "0.4.0" and view["newer"] is False and view["tag"] is None

    def test_newer_recalcule_a_la_lecture(self):
        cached = {"tag": "v0.4.0", "url": "u", "checked_at": "2026-08-04T00:00:00+00:00"}
        # Cache écrit AVANT une mise à niveau : la version courante a rattrapé le tag.
        assert uc.summarize(cached, "0.3.9")["newer"] is True
        assert uc.summarize(cached, "0.4.0")["newer"] is False
