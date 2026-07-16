"""Console d'installeur enregistreuse — remplace les doublons locaux des tests installer."""


class FakeConsole:
    """Enregistre chaque message avec son niveau : les assertions lisent
    ``messages`` (liste de tuples ``(niveau, message)``) ou ``texts(niveau)``."""

    def __init__(self):
        self.messages: list[tuple[str, str]] = []

    def info(self, m: str) -> None:
        self.messages.append(("info", m))

    def ok(self, m: str) -> None:
        self.messages.append(("ok", m))

    def warn(self, m: str) -> None:
        self.messages.append(("warn", m))

    def error(self, m: str) -> None:
        self.messages.append(("error", m))

    def texts(self, level: str | None = None) -> list[str]:
        return [m for lvl, m in self.messages if level is None or lvl == level]
