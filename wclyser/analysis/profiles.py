"""Specialisation profiles.

Spell priorities and cooldown lists are data, not code. A profile lives at
``wclyser/profiles/<class>_<spec>.yaml`` and is chosen per pull, because a
player who respecs between bosses has one specialisation for some pulls and
another for the rest.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from ..errors import WCLError

PROFILE_DIR = Path(__file__).parent.parent / "profiles"


def profile_name(player_class: str | None, spec: str | None) -> str | None:
    if not player_class or not spec:
        return None
    return f"{player_class}_{spec}".lower()


@lru_cache(maxsize=None)
def load_profile(name: str) -> dict[str, Any] | None:
    """Read a profile, or return None when there is none for that spec."""
    path = PROFILE_DIR / f"{name}.yaml"
    if not path.is_file():
        return None
    body = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(body, dict):
        raise WCLError(f"{path} must contain a YAML mapping at the top level")
    return body


def available() -> list[str]:
    return sorted(p.stem for p in PROFILE_DIR.glob("*.yaml"))
