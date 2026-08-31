"""Analysis modules.

Importing this package registers every module. Each reads only from SQLite and
declares the streams it needs, so the runner can check coverage before calling
it rather than letting it answer over missing rows.
"""

from . import havoc  # noqa: F401  (registers the module)
from .base import Ctx, Module, REGISTRY, Result, analysis, resolve, store_metrics

__all__ = [
    "Ctx",
    "Module",
    "REGISTRY",
    "Result",
    "analysis",
    "resolve",
    "store_metrics",
]
