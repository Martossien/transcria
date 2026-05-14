class WorkflowSteps:
    @staticmethod
    def step_requires_upload(step_id: str) -> bool:
        return step_id in {"file", "analyze", "summary", "participants", "processing", "quality", "export"}

    @staticmethod
    def step_requires_speakers(step_id: str) -> bool:
        return step_id in {"processing", "quality"}

    @staticmethod
    def get_step_index(step_id: str) -> int:
        for i, s in enumerate(_STEPS):
            if s["id"] == step_id:
                return i
        return -1

    @staticmethod
    def get_next_step_id(step_id: str) -> str | None:
        idx = WorkflowSteps.get_step_index(step_id)
        if idx < 0 or idx >= len(_STEPS) - 1:
            return None
        return _STEPS[idx + 1]["id"]


_STEPS = [
    {"id": "file", "label": "Fichier"},
    {"id": "analyze", "label": "Analyse"},
    {"id": "summary", "label": "Résumé"},
    {"id": "context", "label": "Contexte"},
    {"id": "participants", "label": "Participants & Locuteurs"},
    {"id": "lexicon", "label": "Lexique"},
    {"id": "processing", "label": "Traitement"},
    {"id": "quality", "label": "Qualité"},
    {"id": "export", "label": "Export"},
]
