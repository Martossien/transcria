"""Fakes de tests (vague C4) — les doublures officielles, importables partout.

Officialise les doublures recopiées de test en test (console d'installeur,
filesystem, GPU, LLM, store) : ``from fakes import FakeConsole, FakeJobStore``.
Patron d'import identique à ``net_helpers`` (le répertoire ``tests/`` est sur
``sys.path`` pendant la collecte).
"""
from fakes.console import FakeConsole  # noqa: F401
from fakes.filesystem import InMemoryJobFilesystem  # noqa: F401
from fakes.gpu import FakeArbitrageVram, FakeLlmLockAllocator, fake_gpu_info  # noqa: F401
from fakes.llm import FakeLlmExecutor  # noqa: F401
from fakes.store import FakeJobStore  # noqa: F401
from fakes.workflow import FakeWorkflowRunner  # noqa: F401
