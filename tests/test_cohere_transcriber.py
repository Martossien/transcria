import sys
import types


def test_cohere_transcriber_passes_model_revision(monkeypatch):
    from transcria.stt.cohere_transcriber import CohereTranscriber

    calls = []

    class FakeProcessor:
        @staticmethod
        def from_pretrained(model_id, **kwargs):
            calls.append(("processor", model_id, kwargs))
            return object()

    class FakeModel:
        @staticmethod
        def from_pretrained(model_id, **kwargs):
            calls.append(("model", model_id, kwargs))
            return object()

    fake_transformers = types.SimpleNamespace(
        AutoProcessor=FakeProcessor,
        AutoModelForSpeechSeq2Seq=FakeModel,
    )
    fake_torch = types.SimpleNamespace(
        bfloat16=object(),
        cuda=types.SimpleNamespace(is_available=lambda: False),
    )

    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)

    transcriber = CohereTranscriber(
        model_path="CohereLabs/cohere-transcribe-03-2026",
        model_revision="  32d9e4ba  ",
        device="cpu",
    )

    assert transcriber.load() is True
    assert calls == [
        (
            "processor",
            "CohereLabs/cohere-transcribe-03-2026",
            {"revision": "32d9e4ba", "trust_remote_code": True},
        ),
        (
            "model",
            "CohereLabs/cohere-transcribe-03-2026",
            {
                "revision": "32d9e4ba",
                "torch_dtype": fake_torch.bfloat16,
                "device_map": "cpu",
                "trust_remote_code": True,
            },
        ),
    ]
