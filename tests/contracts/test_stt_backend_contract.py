"""Suite de contrat commune des backends STT (vague C1) — paramétrée sur le registre.

Chaque backend natif prouve ici ce qui est vérifiable sans modèle ni GPU :
descripteur complet et cohérent, construction sans chargement, VRAM entière
positive, dispatch de la factory. Les contrats comportementaux sur l'audio
réel (WAV 16 k accepté, segments triés, timestamps monotones) vivent dans les
suites GPU/E2E — un modèle est nécessaire pour les exercer.
"""
import inspect

import pytest

from transcria.stt import registry
from transcria.stt.base_transcriber import BaseTranscriber
from transcria.stt.registry import ModelCatalogEntry, SttBackendDescriptor
from transcria.stt.transcriber_factory import create_transcriber, get_backend_vram_mb, list_available_backends, local_builders

# L'ordre historique de _STT_BACKENDS est un contrat d'affichage (wizard, doc).
EXPECTED_ORDER = ["cohere", "cohere_tf5", "whisper", "granite", "parakeet", "voxtral", "kroko", "moss"]


def _descriptors() -> list[SttBackendDescriptor]:
    return list(registry.backends().values())


class TestRegistryShape:
    def test_names_and_historical_order(self):
        assert list(registry.backends()) == EXPECTED_ORDER
        assert list_available_backends() == EXPECTED_ORDER

    def test_descriptor_lives_in_its_backend_module(self):
        # DoD C1 : ajouter un backend = 1 module + 1 enregistrement.
        for descriptor in _descriptors():
            module = inspect.getmodule(descriptor.build)
            assert module.__name__ == f"transcria.stt.{descriptor.name}_transcriber"
            assert module.DESCRIPTOR is descriptor


@pytest.mark.parametrize("name", EXPECTED_ORDER)
class TestBackendContract:
    def test_descriptor_coherent(self, name):
        descriptor = registry.get(name)
        assert descriptor.name == name
        assert callable(descriptor.build)
        assert callable(descriptor.vram_mb)

    def test_vram_is_a_positive_int_from_empty_config(self, name):
        vram = registry.get(name).vram_mb({})
        assert isinstance(vram, int)
        assert vram >= 0

    def test_catalog_entry_complete_when_present(self, name):
        entry = registry.get(name).catalog
        if entry is None:
            # Seul cohere_tf5 réutilise le modèle d'un autre backend (cohere).
            assert name == "cohere_tf5"
            return
        assert isinstance(entry, ModelCatalogEntry)
        assert entry.repo and "/" in entry.repo
        assert entry.license
        assert entry.license_url.startswith("https://")
        assert entry.est_gb > 0

    def test_build_returns_transcriber_without_loading(self, name):
        transcriber = registry.get(name).build({}, None)
        assert isinstance(transcriber, BaseTranscriber)
        # `available` fait partie du contrat : sondable sans modèle, sans lever.
        assert isinstance(transcriber.available, bool)

    def test_factory_dispatches_to_this_backend(self, name):
        transcriber = create_transcriber({}, backend=name)
        assert type(transcriber).__module__ == f"transcria.stt.{name}_transcriber"

    def test_local_builder_exposed(self, name):
        assert local_builders()[name] is registry.get(name).build


class TestCatalogReadsRegistry:
    def test_models_catalog_sources_mirror_registry_entries(self):
        from transcria.models_catalog import _stt_sources

        sources = _stt_sources()
        with_catalog = {d.name: d.catalog for d in _descriptors() if d.catalog is not None}
        assert sorted(sources) == sorted(with_catalog)
        for name, entry in with_catalog.items():
            assert sources[name] == {
                "repo": entry.repo, "gated": entry.gated, "license": entry.license,
                "license_url": entry.license_url, "est_gb": entry.est_gb,
            }


class TestFakeBackendDemo:
    """DoD C1 : la démonstration qu'ajouter un backend = 1 module + 1 enregistrement.

    Le « module » est simulé ici ; l'enregistrement suffit pour que factory,
    VRAM et builders locaux le servent sans autre modification.
    """

    @pytest.fixture()
    def fake_registry(self, monkeypatch):
        class FakeTranscriber(BaseTranscriber):
            model_name = "fake"

            @property
            def available(self) -> bool:
                return True

            def load(self) -> bool:
                return True

            def transcribe(self, audio_path, language="fr", chunk_length_s=30,
                           progress_callback=None, audio_array=None, sample_rate=16000):
                return []

            def offload(self) -> None:
                return None

        descriptor = SttBackendDescriptor(
            name="fake",
            build=lambda config, device=None: FakeTranscriber(),
            vram_mb=lambda config: 1234,
            catalog=None,
        )
        table = dict(registry.backends())
        table["fake"] = descriptor
        monkeypatch.setattr(registry, "backends", lambda: table)
        return descriptor

    def test_factory_serves_the_fake_backend(self, fake_registry):
        transcriber = create_transcriber({}, backend="fake")
        assert type(transcriber).__name__ == "FakeTranscriber"
        assert get_backend_vram_mb("fake", {}) == 1234
        assert "fake" in list_available_backends()
        assert local_builders()["fake"] is fake_registry.build


class TestUnknownBackendFallback:
    def test_unknown_backend_falls_back_to_cohere_with_warning(self, caplog):
        import logging as _logging

        with caplog.at_level(_logging.WARNING):
            transcriber = create_transcriber({}, backend="inexistant")
        assert type(transcriber).__name__ == "CohereTranscriber"
        assert any("inexistant" in record.message for record in caplog.records)

    def test_unknown_backend_vram_falls_back_to_cohere_footprint(self):
        assert get_backend_vram_mb("inexistant", {"gpu": {"cohere_vram_mb": 4321}}) == 4321
