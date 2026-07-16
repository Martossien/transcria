"""Fake LLM — réponses scriptées, sans sous-processus ni GPU."""


class FakeLlmExecutor:
    """Rejoue des réponses scriptées dans l'ordre (``replies``), enregistre les
    instructions reçues (``calls``). Épuisement = AssertionError : un test qui
    consomme plus de réponses que prévu est un test faux."""

    def __init__(self, replies: list[str] | None = None):
        self.replies = list(replies or [])
        self.calls: list[dict] = []

    def run(self, instruction: str, **kwargs) -> str:
        self.calls.append({"instruction": instruction, **kwargs})
        assert self.replies, "FakeLlmExecutor : plus de réponse scriptée disponible"
        return self.replies.pop(0)
