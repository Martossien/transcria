import glob
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_KNOWN_BINARIES = [
    "ffmpeg",
    "ffprobe",
    "opencode",
    "ollama",
    "llama-server",
    "python3",
    "nvcc",
]

# Emplacements connus HORS PATH, sondés quand `shutil.which` échoue. Le service tourne
# souvent en root (cf. runtime_root_vs_admin_env), dont le PATH n'a ni /usr/local/cuda/bin
# (nvcc) ni le `llama-server` compilé maison (~/llama.cpp/build/bin, hors PATH par nature).
# Les motifs peuvent contenir des jokers glob ; le premier fichier exécutable gagne.
_BINARY_FALLBACK_GLOBS: dict[str, list[str]] = {
    "nvcc": [
        "/usr/local/cuda/bin/nvcc",       # symlink « courant » d'abord
        "/usr/local/cuda-*/bin/nvcc",
        "/opt/cuda/bin/nvcc",
    ],
    "llama-server": [
        "/usr/local/bin/llama-server",
        "/opt/llama.cpp/build/bin/llama-server",
        "/root/llama.cpp/build/bin/llama-server",
        "/home/*/llama.cpp/build/bin/llama-server",  # build maison d'un compte utilisateur
    ],
}


@dataclass
class GPUInfo:
    index: int
    name: str
    memory_total_mb: int
    memory_free_mb: int
    memory_used_mb: int
    driver_version: str = ""

    @property
    def memory_total_gb(self) -> float:
        return self.memory_total_mb / 1024

    @property
    def memory_free_gb(self) -> float:
        return self.memory_free_mb / 1024

    @property
    def utilization_pct(self) -> float:
        total = self.memory_total_mb
        if total == 0:
            return 0.0
        return (self.memory_used_mb / total) * 100

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "name": self.name,
            "memory_total_mb": self.memory_total_mb,
            "memory_free_mb": self.memory_free_mb,
            "memory_used_mb": self.memory_used_mb,
            "memory_total_gb": round(self.memory_total_gb, 1),
            "memory_free_gb": round(self.memory_free_gb, 1),
            "utilization_pct": round(self.utilization_pct, 1),
            "driver_version": self.driver_version,
        }


@dataclass
class BinaryInfo:
    name: str
    path: str | None
    version: str | None = None

    @property
    def available(self) -> bool:
        return self.path is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "available": self.available,
            "version": self.version,
        }


@dataclass
class SystemInfo:
    gpus: list[GPUInfo] = field(default_factory=list)
    cuda_version: str | None = None
    binaries: list[BinaryInfo] = field(default_factory=list)
    ram_total_mb: int = 0
    ram_free_mb: int = 0
    disk_total_gb: float = 0.0
    disk_free_gb: float = 0.0
    disk_path: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def gpu_count(self) -> int:
        return len(self.gpus)

    @property
    def total_vram_mb(self) -> int:
        return sum(g.memory_total_mb for g in self.gpus)

    def to_dict(self) -> dict[str, Any]:
        return {
            "gpus": [g.to_dict() for g in self.gpus],
            "gpu_count": self.gpu_count,
            "total_vram_mb": self.total_vram_mb,
            "total_vram_gb": round(self.total_vram_mb / 1024, 1),
            "cuda_version": self.cuda_version,
            "binaries": [b.to_dict() for b in self.binaries],
            "ram_total_mb": self.ram_total_mb,
            "ram_free_mb": self.ram_free_mb,
            "ram_total_gb": round(self.ram_total_mb / 1024, 1),
            "disk_total_gb": self.disk_total_gb,
            "disk_free_gb": self.disk_free_gb,
            "disk_path": self.disk_path,
            "warnings": self.warnings,
        }


class SystemDetector:

    @staticmethod
    def detect() -> SystemInfo:
        info = SystemInfo()
        info.gpus = SystemDetector._detect_gpus()
        info.cuda_version = SystemDetector._detect_cuda_version()
        info.binaries = SystemDetector._detect_binaries()
        SystemDetector._detect_ram(info)
        SystemDetector._detect_disk(info)
        SystemDetector._generate_warnings(info)
        return info

    @staticmethod
    def _detect_gpus() -> list[GPUInfo]:
        gpus: list[GPUInfo] = []
        try:
            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=index,name,memory.total,memory.free,memory.used,driver_version",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                logger.debug("nvidia-smi returned code %d", result.returncode)
                return gpus

            for line in result.stdout.strip().split("\n"):
                if not line.strip():
                    continue
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 5:
                    try:
                        gpus.append(
                            GPUInfo(
                                index=int(parts[0]),
                                name=parts[1],
                                memory_total_mb=int(parts[2]),
                                memory_free_mb=int(parts[3]),
                                memory_used_mb=int(parts[4]),
                                driver_version=parts[5],
                            )
                        )
                    except (ValueError, IndexError):
                        logger.debug("Ligne nvidia-smi non parsable: %s", line)
        except FileNotFoundError:
            logger.debug("nvidia-smi non trouvé dans PATH")
        except Exception as exc:
            logger.debug("Erreur détection nvidia-smi: %s", exc)

        return gpus

    @staticmethod
    def _detect_cuda_version() -> str | None:
        nvcc = SystemDetector._resolve_binary("nvcc")
        try:
            if nvcc is None:
                raise FileNotFoundError("nvcc")
            result = subprocess.run(
                [nvcc, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return None
            for line in result.stdout.split("\n"):
                line = line.strip()
                if "release" in line:
                    parts = line.split("release")
                    if len(parts) >= 2:
                        version = parts[1].strip().split(",")[0].strip()
                        return version
        except (FileNotFoundError, Exception):
            pass

        try:
            import torch

            if torch.cuda.is_available():
                return torch.version.cuda
        except ImportError:
            pass

        return None

    @staticmethod
    def _resolve_binary(name: str) -> str | None:
        """Chemin d'un binaire : PATH d'abord, puis emplacements connus hors PATH.

        Le repli hors PATH évite les faux « absents » sur la page config quand le service
        tourne dans un environnement au PATH réduit (root : ni CUDA, ni build maison)."""
        path = shutil.which(name)
        if path:
            return path
        patterns = list(_BINARY_FALLBACK_GLOBS.get(name, []))
        home = os.environ.get("HOME")
        if name == "llama-server" and home:
            patterns.insert(0, os.path.join(home, "llama.cpp", "build", "bin", "llama-server"))
        for pattern in patterns:
            # Ordre décroissant : à versions multiples (cuda-13 vs cuda-12), la plus récente.
            for candidate in sorted(glob.glob(pattern), reverse=True):
                if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                    return candidate
        return None

    @staticmethod
    def _detect_binaries() -> list[BinaryInfo]:
        binaries: list[BinaryInfo] = []
        for name in _KNOWN_BINARIES:
            path = SystemDetector._resolve_binary(name)
            version = None
            if path:
                version = SystemDetector._get_binary_version(name, path)
            binaries.append(BinaryInfo(name=name, path=path, version=version))
        return binaries

    @staticmethod
    def _get_binary_version(name: str, path: str) -> str | None:
        version_flags: dict[str, list[str]] = {
            "ffmpeg": ["-version"],
            "ffprobe": ["-version"],
            "opencode": ["--version"],
            "ollama": ["--version"],
            "llama-server": ["--version"],
            "python3": ["--version"],
            "nvcc": ["--version"],
        }
        flags = version_flags.get(name, ["--version"])
        try:
            result = subprocess.run(
                [path] + flags,
                capture_output=True,
                text=True,
                timeout=5,
            )
            combined = (result.stdout + result.stderr).strip()
            first_line = combined.split("\n")[0][:120] if combined else None
            return first_line
        except Exception:
            return None

    @staticmethod
    def _detect_ram(info: SystemInfo) -> None:
        try:
            import psutil

            mem = psutil.virtual_memory()
            info.ram_total_mb = int(mem.total / (1024 * 1024))
            info.ram_free_mb = int(mem.available / (1024 * 1024))
        except ImportError:
            try:
                with open("/proc/meminfo") as f:
                    content = f.read()
                for line in content.split("\n"):
                    if line.startswith("MemTotal:"):
                        info.ram_total_mb = int(line.split()[1]) // 1024
                    elif line.startswith("MemAvailable:"):
                        info.ram_free_mb = int(line.split()[1]) // 1024
            except Exception:
                info.warnings.append("Impossible de détecter la RAM")

    @staticmethod
    def _detect_disk(info: SystemInfo) -> None:
        home = os.environ.get("HOME", str(Path.home()))
        info.disk_path = home
        try:
            import shutil as sh

            usage = sh.disk_usage(home)
            info.disk_total_gb = round(usage.total / (1024**3), 1)
            info.disk_free_gb = round(usage.free / (1024**3), 1)
        except Exception:
            info.warnings.append(f"Impossible de détecter l'espace disque sur {home}")

    @staticmethod
    def _generate_warnings(info: SystemInfo) -> None:
        if info.gpu_count == 0:
            info.warnings.append(
                "Aucun GPU NVIDIA détecté. La transcription et l'arbitrage seront lents."
            )
        else:
            total_vram = info.total_vram_mb
            if total_vram < 8000:
                info.warnings.append(
                    f"VRAM totale faible ({total_vram} Mo). "
                    "Certains modèles STT/LLM peuvent ne pas tenir en mémoire."
                )

        if info.cuda_version is None:
            info.warnings.append(
                "CUDA non détecté. Les opérations GPU ne seront pas disponibles."
            )

        missing = [
            b.name for b in info.binaries if not b.available and b.name in ("ffmpeg", "ffprobe")
        ]
        if missing:
            info.warnings.append(
                f"Binaires requis manquants: {', '.join(missing)}. "
                "Installation: apt install ffmpeg"
            )

        if info.disk_free_gb < 10:
            info.warnings.append(
                f"Espace disque faible sur {info.disk_path}: "
                f"{info.disk_free_gb} Go libres. Le stockage des jobs peut être compromis."
            )
