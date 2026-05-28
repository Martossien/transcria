import os


_DISABLED_VALUES = {"-1", "none", "void", "nodevfile", "nodevfiles"}


def parse_cuda_visible_devices(value: str | None = None) -> list[str] | None:
    """Retourne la liste CUDA_VISIBLE_DEVICES, [] si CUDA est masqué, None si non contraint."""
    raw = os.environ.get("CUDA_VISIBLE_DEVICES", "") if value is None else value
    raw = raw.strip()
    if not raw:
        return None
    if raw.lower() in _DISABLED_VALUES:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def to_visible_device_index(
    reported_gpu_id: int | str,
    visible_devices: list[str] | None = None,
    *,
    allow_remapped_ordinal: bool = False,
) -> int | None:
    """Convertit un id GPU physique ou déjà remappé en ordinal CUDA visible."""
    visible = parse_cuda_visible_devices() if visible_devices is None else visible_devices
    try:
        reported = int(reported_gpu_id)
    except (TypeError, ValueError):
        reported_text = str(reported_gpu_id)
        if visible is None:
            return None
        return visible.index(reported_text) if reported_text in visible else None

    if visible is None:
        return reported
    if not visible:
        return None

    # Fallback torch.cuda: les ids sont déjà remappés en cuda:0..N-1.
    if allow_remapped_ordinal and 0 <= reported < len(visible):
        return reported

    numeric_visible = []
    for item in visible:
        try:
            numeric_visible.append(int(item))
        except ValueError:
            return None
    return numeric_visible.index(reported) if reported in numeric_visible else None


def to_nvidia_smi_gpu_index(visible_gpu_index: int, visible_devices: list[str] | None = None) -> int:
    """Retourne l'index physique à passer à nvidia-smi -i pour un ordinal CUDA visible."""
    visible = parse_cuda_visible_devices() if visible_devices is None else visible_devices
    if visible is None:
        return visible_gpu_index
    if not visible or visible_gpu_index < 0 or visible_gpu_index >= len(visible):
        return visible_gpu_index
    try:
        return int(visible[visible_gpu_index])
    except ValueError:
        return visible_gpu_index
