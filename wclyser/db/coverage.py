"""What the database holds, against what an analysis needs.

Import is narrowed to a few streams, so an analysis module can ask for
something that was never imported. Rather than returning a quietly wrong
answer over missing rows, a module declares the streams it reads and calls
:func:`require`, which either passes or raises with the exact command to run.

A gap has two causes and they need different commands. If the blob is on disk
the fix is a re-import, which is free. If it is not, the fix is a fetch first,
which costs API points.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config
from ..errors import WCLError
from ..fetch.cache import BlobCache
from ..fetch.meta import Actor, Fight, ReportMeta
from ..fetch.scopes import BY_KEY, Scope, subject_slug


class MissingData(WCLError):
    """An analysis needs data the database does not hold."""


@dataclass
class Gap:
    fight_id: int
    stream: str
    subject: str
    cached: bool

    @property
    def remedy(self) -> str:
        return "import" if self.cached else "fetch"


@dataclass
class Coverage:
    report_code: str
    required: list[str]
    gaps: list[Gap] = field(default_factory=list)
    checked: int = 0

    @property
    def complete(self) -> bool:
        return not self.gaps

    @property
    def needs_fetch(self) -> list[Gap]:
        return [g for g in self.gaps if not g.cached]

    @property
    def needs_import(self) -> list[Gap]:
        return [g for g in self.gaps if g.cached]

    def message(self) -> str:
        """A description a person can act on."""
        streams = sorted({g.stream for g in self.gaps})
        fights = sorted({g.fight_id for g in self.gaps})
        lines = [
            f"{len(self.gaps)} of {self.checked} required blobs are not in the "
            f"database for report {self.report_code}.",
            f"Missing streams: {', '.join(streams)}.",
            f"Affected pulls: {', '.join(str(f) for f in fights)}.",
        ]
        stream_flags = " ".join(f"--stream {s}" for s in streams)
        if self.needs_import:
            lines.append(
                f"{len(self.needs_import)} are already cached; re-import is free:"
            )
            lines.append(f"  wcly import {self.report_code} {stream_flags}")
        if self.needs_fetch:
            lines.append(
                f"{len(self.needs_fetch)} are not cached and must be fetched first, "
                f"which spends API points:"
            )
            lines.append(f"  wcly fetch {self.report_code} {stream_flags}")
            lines.append(f"  wcly import {self.report_code} {stream_flags}")
        return "\n".join(lines)


def imported_blobs(conn: sqlite3.Connection, report_code: str) -> set[tuple[int, str, str]]:
    return {
        (row["fight_id"], row["stream"], row["subject"])
        for row in conn.execute(
            "SELECT fight_id, stream, subject FROM import_log WHERE report_code = ?",
            (report_code,),
        )
    }


def check(
    conn: sqlite3.Connection,
    config: Config,
    meta: ReportMeta,
    fights: list[Fight],
    subjects: list[Actor],
    required: list[str],
) -> Coverage:
    """Compare what an analysis needs against what has been imported."""
    unknown = [s for s in required if s not in BY_KEY]
    if unknown:
        raise WCLError(
            f"unknown stream(s) {unknown}; known keys are: {', '.join(sorted(BY_KEY))}"
        )

    have = imported_blobs(conn, meta.code)
    cache = BlobCache(Path(config.raw_dir))
    result = Coverage(report_code=meta.code, required=list(required))

    for fight in fights:
        present = meta.subjects_in_fight(fight, subjects)
        for key in required:
            stream = BY_KEY[key]
            if stream.scope is Scope.SUBJECT:
                slugs = [subject_slug(stream, a.player_key) for a in present]
            else:
                slugs = [subject_slug(stream, None)]
            for slug in slugs:
                result.checked += 1
                if (fight.id, key, slug) in have:
                    continue
                blob_path = cache.path(meta.code, fight.id, key, slug)
                result.gaps.append(
                    Gap(fight.id, key, slug, cached=blob_path.is_file())
                )

    return result


def require(
    conn: sqlite3.Connection,
    config: Config,
    meta: ReportMeta,
    fights: list[Fight],
    subjects: list[Actor],
    required: list[str],
) -> Coverage:
    """Pass when the data is present, raise with the remedy when it is not."""
    coverage = check(conn, config, meta, fights, subjects, required)
    if not coverage.complete:
        raise MissingData(coverage.message())
    return coverage


def ensure(
    conn: sqlite3.Connection,
    config: Config,
    meta: ReportMeta,
    fights: list[Fight],
    subjects: list[Actor],
    required: list[str],
    allow_import: bool = True,
) -> tuple[Coverage, object | None]:
    """Make the required data present where doing so is free.

    A gap whose blob is already cached is closed by importing it, which costs
    no API points and is idempotent. A gap with no blob needs a fetch, which
    spends points, so it raises instead and names the command. That keeps
    analysis unable to reach the network however it is called.

    Returns the coverage after any import, and the import summary when one ran.
    """
    coverage = check(conn, config, meta, fights, subjects, required)
    if coverage.complete:
        return coverage, None

    summary = None
    if allow_import and not coverage.needs_fetch:
        from ..fetch.scopes import resolve_streams
        from .importer import Importer

        streams = resolve_streams(sorted({g.stream for g in coverage.gaps}))
        affected_ids = {g.fight_id for g in coverage.gaps}
        affected = [f for f in fights if f.id in affected_ids]
        summary = Importer(conn, config, meta).run(affected, subjects, streams)
        coverage = check(conn, config, meta, fights, subjects, required)

    if not coverage.complete:
        raise MissingData(coverage.message())
    return coverage, summary
