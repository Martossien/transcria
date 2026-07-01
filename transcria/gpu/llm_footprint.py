"""Dérivation de l'empreinte VRAM d'une LLM d'arbitrage — JAMAIS de taille en dur.

Empreinte = **poids** (taille RÉELLE du fichier téléchargé) + **KV-cache** (CALCULÉ depuis
l'archi + le contexte du palier) + marge. Cette valeur alimente ``gpu.llm_vram_mb`` (réservation
VRAM du ``VRAMManager``) et le placement — un littéral y donnerait un placement faux.

Deux temps (validé) :
  1. CALCUL : poids = ``os.path.getsize`` (GGUF) ou somme d'un dossier ; KV = formule depuis
     l'archi (métadonnées GGUF / ``config.json``) → sert à CHOISIR/réserver.
  2. VÉRIF au 1ᵉʳ load : la mesure réelle (Ollama ``/api/ps size_vram`` ; logs llama.cpp/vLLM)
     RECALE la valeur (``measured`` prime sur ``computed``).

Les fonctions de calcul sont PURES (testables sans GPU ni fichier) ; la lecture GGUF et la
taille de fichier sont des E/S, exercées à l'install / au 1ᵉʳ load.
"""
from __future__ import annotations

from pathlib import Path

_MB = 1024 * 1024


def kv_cache_mb(*, n_layers: int, n_kv_heads: int, head_dim: int, context: int, kv_dtype_bytes: int) -> int:
    """Taille du KV-cache (Mo) : 2 (K+V) × couches × têtes_kv × dim_tête × contexte × octets.

    C'est le poste qui DOMINE l'empreinte des petits modèles à grand contexte (256K)."""
    if min(n_layers, n_kv_heads, head_dim, context, kv_dtype_bytes) <= 0:
        return 0
    bytes_total = 2 * n_layers * n_kv_heads * head_dim * context * kv_dtype_bytes
    return int(bytes_total // _MB)


def weights_mb(path: str | Path) -> int:
    """Taille RÉELLE des poids (Mo) : fichier GGUF unique, ou somme d'un dossier de modèle."""
    p = Path(path)
    if p.is_file():
        return int(p.stat().st_size // _MB)
    if p.is_dir():
        total = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        return int(total // _MB)
    return 0


def arch_from_hf_config(config: dict) -> dict | None:
    """(n_layers, n_kv_heads, head_dim) depuis un config.json HF, ou None si incomplet.

    head_dim = ``head_dim`` explicite, sinon ``hidden_size`` / ``num_attention_heads``.
    n_kv_heads = ``num_key_value_heads`` (GQA), sinon ``num_attention_heads`` (MHA)."""
    if not isinstance(config, dict):
        return None
    n_layers = config.get("num_hidden_layers")
    n_heads = config.get("num_attention_heads")
    n_kv = config.get("num_key_value_heads", n_heads)
    hidden = config.get("hidden_size")
    head_dim = config.get("head_dim") or (int(hidden) // int(n_heads) if hidden and n_heads else None)
    if not (n_layers and n_kv and head_dim):
        return None
    return {"n_layers": int(n_layers), "n_kv_heads": int(n_kv), "head_dim": int(head_dim)}


def read_gguf_arch(path: str | Path) -> dict | None:
    """(n_layers, n_kv_heads, head_dim) depuis les métadonnées GGUF (lib ``gguf``), ou None.

    E/S + dépendance optionnelle : on n'échoue jamais (retourne None → l'appelant retombe
    sur la mesure au load)."""
    try:
        from gguf import GGUFReader  # type: ignore[import-not-found]
    except Exception:
        return None
    try:
        reader = GGUFReader(str(path))
        fields = reader.fields

        def _val(*keys):
            for k in keys:
                f = fields.get(k)
                if f is not None and f.data is not None and len(f.data):
                    return int(f.parts[f.data[0]][0])
            return None

        # Clés GGUF : <arch>.block_count / .attention.head_count[_kv] / .embedding_length.
        arch = None
        af = fields.get("general.architecture")
        if af is not None and af.data is not None and len(af.data):
            arch = bytes(af.parts[af.data[0]]).decode("utf-8", "replace")
        pfx = f"{arch}." if arch else ""
        n_layers = _val(f"{pfx}block_count", "block_count")
        n_heads = _val(f"{pfx}attention.head_count", "attention.head_count")
        n_kv = _val(f"{pfx}attention.head_count_kv", "attention.head_count_kv") or n_heads
        emb = _val(f"{pfx}embedding_length", "embedding_length")
        head_dim = (emb // n_heads) if (emb and n_heads) else None
        if not (n_layers and n_kv and head_dim):
            return None
        return {"n_layers": int(n_layers), "n_kv_heads": int(n_kv), "head_dim": int(head_dim)}
    except Exception:
        return None


def footprint_mb(weights: int, kv: int, *, overhead_pct: float = 0.12) -> int:
    """Empreinte VRAM totale (Mo) = (poids + KV) × (1 + marge) — activations, fragmentation."""
    return int((weights + kv) * (1.0 + overhead_pct))


def derive_footprint_mb(
    *, model_path: str | Path | None, arch: dict | None, context: int, kv_dtype_bytes: int,
    overhead_pct: float = 0.12,
) -> int | None:
    """Empreinte calculée (poids fichier + KV archi). None si les poids ou l'archi manquent
    (l'appelant retombe alors sur la mesure au 1ᵉʳ load)."""
    if not model_path or not arch:
        return None
    w = weights_mb(model_path)
    if w <= 0:
        return None
    kv = kv_cache_mb(context=context, kv_dtype_bytes=kv_dtype_bytes, **arch)
    return footprint_mb(w, kv, overhead_pct=overhead_pct)
