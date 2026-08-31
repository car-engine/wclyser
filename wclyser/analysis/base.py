"""The analysis registry.

A module declares the streams it reads and returns a :class:`Result`. The
runner checks coverage before calling it, so a module never sees a pull whose
data was not imported, and never has to guard against missing rows itself.

Presentation lives in the module. A module knows what its own output should
look like, and a general table renderer would fit none of them well. What the
framework takes from a module is its ``metrics``, which go to the ``metric``
table in long format, so trend and history queries need no knowledge of any
individual module.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from ..config import Config
from ..errors import WCLError
from ..fetch.meta import Actor, Fight, ReportMeta

# fight_id used for a metric that describes a whole report rather than a pull.
REPORT_SCOPE = -1


@dataclass
class Ctx:
    """What a module is given."""

    conn: sqlite3.Connection
    config: Config
    meta: ReportMeta
    fights: list[Fight]
    subjects: list[Actor]
    options: dict[str, Any] = field(default_factory=dict)

    def option(self, name: str, default: Any = None) -> Any:
        return self.options.get(name, default)

    def spec_of(self, fight_id: int, player_key: str) -> tuple[str | None, str | None]:
        """The class and specialisation a subject held for one pull."""
        row = self.conn.execute(
            """
            SELECT class, spec FROM fight_player
            WHERE report_code = ? AND fight_id = ? AND player_key = ?
            """,
            (self.meta.code, fight_id, player_key),
        ).fetchone()
        if row is None:
            return None, None
        return row["class"], row["spec"]


@dataclass
class Result:
    key: str
    title: str
    # Formatted output, printed as given.
    lines: list[str] = field(default_factory=list)
    # Metrics describing the whole report.
    metrics: dict[str, float] = field(default_factory=dict)
    # Metrics per pull, keyed by fight id.
    fight_metrics: dict[int, dict[str, float]] = field(default_factory=dict)
    # Anything the module wants to flag: an unresolved spell name, a pull it
    # could not judge.
    warnings: list[str] = field(default_factory=list)
    # Structured data for the report layer to render and for CSV export. A
    # module that has no page template can leave this empty.
    payload: dict = field(default_factory=dict)


@dataclass
class Module:
    key: str
    title: str
    requires: frozenset[str]
    run: Callable[[Ctx], dict[str, Result]]


REGISTRY: dict[str, Module] = {}


def analysis(key: str, title: str, requires: set[str]):
    """Register an analysis module.

    ``requires`` names the streams the module reads. The runner passes them to
    the coverage check, so a module that reads a stream nobody imported fails
    with the command to fix it instead of a quietly short answer.
    """

    def decorate(func: Callable[[Ctx], dict[str, Result]]) -> Callable:
        if key in REGISTRY:
            raise WCLError(f"analysis module {key!r} is registered twice")
        REGISTRY[key] = Module(
            key=key, title=title, requires=frozenset(requires), run=func
        )
        return func

    return decorate


def resolve(keys: list[str] | None) -> list[Module]:
    if not keys:
        return list(REGISTRY.values())
    unknown = [k for k in keys if k not in REGISTRY]
    if unknown:
        known = ", ".join(sorted(REGISTRY)) or "none registered"
        raise WCLError(f"unknown analysis module(s) {unknown}; known: {known}")
    return [REGISTRY[k] for k in keys]


def store_metrics(
    conn: sqlite3.Connection, report_code: str, player_key: str, result: Result
) -> int:
    """Write a module's metrics to the long-format metric table."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = [
        (player_key, report_code, REPORT_SCOPE, result.key, name, value, now)
        for name, value in result.metrics.items()
    ]
    for fight_id, metrics in result.fight_metrics.items():
        rows.extend(
            (player_key, report_code, fight_id, result.key, name, value, now)
            for name, value in metrics.items()
        )
    with conn:
        conn.executemany(
            """
            INSERT INTO metric (player_key, report_code, fight_id, analysis_key,
                                metric_key, value, computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(player_key, report_code, fight_id, analysis_key, metric_key)
            DO UPDATE SET value = excluded.value, computed_at = excluded.computed_at
            """,
            rows,
        )
    return len(rows)
