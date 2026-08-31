"""Importing cached blobs into SQLite.

Import reads the blob cache and never the API, so it needs no credentials and
cannot spend API points. A blob that is expected and absent is reported rather
than fetched; run ``wcly fetch`` and import again.

Idempotency comes from the replacement unit matching the fetch unit. For one
``(report, fight, stream, subject)`` the importer deletes every row that tuple
produced and inserts the blob's rows again, inside one transaction. A blob
whose hash matches the recorded one is skipped without touching the tables.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..config import Config
from ..fetch.cache import BlobCache
from ..fetch.meta import Actor, Fight, ReportMeta
from ..fetch.scopes import RAID_SUBJECT, Scope, Stream, subject_slug
from .mappers import EVENT_INSERT, ActorIndex, combatant_rows, map_events
from .schema import schema_version

log = logging.getLogger(__name__)

# CombatantInfo is not a timeline event; it populates fight_player instead.
COMBATANT_STREAM = "combatant_info"


@dataclass
class ImportSummary:
    report_code: str
    fights: int = 0
    imported: int = 0
    skipped: int = 0
    rows: int = 0
    missing: list[tuple[int, str, str]] = field(default_factory=list)
    per_stream: dict[str, int] = field(default_factory=dict)


class Importer:
    def __init__(self, conn: sqlite3.Connection, config: Config, meta: ReportMeta) -> None:
        self.conn = conn
        self.config = config
        self.meta = meta
        self.cache = BlobCache(config.raw_dir)
        self.actors = ActorIndex(meta.actors)
        self.now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.schema_version = schema_version(conn)

    # -- dimensions ------------------------------------------------------

    def import_dimensions(self) -> None:
        meta = self.meta
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO report (code, title, owner, guild, region, zone_id,
                                    zone_name, start_time_utc, end_time_utc,
                                    raid_night, imported_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    title = excluded.title,
                    owner = excluded.owner,
                    guild = excluded.guild,
                    region = excluded.region,
                    zone_id = excluded.zone_id,
                    zone_name = excluded.zone_name,
                    end_time_utc = excluded.end_time_utc,
                    imported_at = excluded.imported_at
                """,
                (
                    meta.code,
                    meta.title,
                    meta.owner,
                    meta.guild,
                    meta.region,
                    meta.zone_id,
                    meta.zone_name,
                    meta.start_time_utc,
                    meta.end_time_utc,
                    meta.raid_night,
                    self.now,
                ),
            )

            self.conn.executemany(
                """
                INSERT INTO actor (report_code, actor_id, guid, name, server,
                                   region, type, sub_type, player_key, pet_owner)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(report_code, actor_id) DO UPDATE SET
                    name = excluded.name,
                    sub_type = excluded.sub_type,
                    player_key = excluded.player_key,
                    pet_owner = excluded.pet_owner
                """,
                [
                    (
                        meta.code,
                        a.id,
                        a.game_id,
                        a.name,
                        a.server,
                        a.region,
                        a.type,
                        a.sub_type,
                        a.player_key if a.is_player else None,
                        a.pet_owner,
                    )
                    for a in meta.actors.values()
                ],
            )

            self.conn.executemany(
                """
                INSERT INTO player (player_key, name, server, region, class,
                                    guid, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(player_key) DO UPDATE SET
                    class = excluded.class,
                    guid = COALESCE(excluded.guid, player.guid),
                    first_seen = MIN(player.first_seen, excluded.first_seen),
                    last_seen  = MAX(player.last_seen,  excluded.last_seen)
                """,
                [
                    (
                        a.player_key,
                        a.name,
                        a.server,
                        a.region,
                        a.player_class,
                        a.game_id,
                        meta.raid_night,
                        meta.raid_night,
                    )
                    for a in meta.players()
                ],
            )

            self.conn.executemany(
                """
                INSERT INTO ability (ability_id, name, icon, school)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(ability_id) DO UPDATE SET
                    name = excluded.name,
                    icon = excluded.icon
                """,
                [
                    (
                        ability_id,
                        raw.get("name"),
                        raw.get("icon"),
                        raw.get("type"),
                    )
                    for ability_id, raw in meta.abilities.items()
                ],
            )

    def import_fight(self, fight: Fight) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO fight (report_code, fight_id, encounter_id, boss_name,
                                   difficulty, difficulty_name, raid_size, kill,
                                   end_percent, boss_percent, last_phase,
                                   start_ms, end_ms, duration_ms, pull_number)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(report_code, fight_id) DO UPDATE SET
                    kill = excluded.kill,
                    end_percent = excluded.end_percent,
                    boss_percent = excluded.boss_percent,
                    last_phase = excluded.last_phase,
                    end_ms = excluded.end_ms,
                    duration_ms = excluded.duration_ms,
                    pull_number = excluded.pull_number
                """,
                (
                    self.meta.code,
                    fight.id,
                    fight.encounter_id,
                    fight.name,
                    fight.difficulty,
                    fight.difficulty_name,
                    fight.size,
                    1 if fight.kill else 0,
                    fight.fight_percentage,
                    fight.boss_percentage,
                    fight.last_phase,
                    fight.start_ms,
                    fight.end_ms,
                    fight.duration_ms,
                    fight.pull_number,
                ),
            )

    # -- blobs -----------------------------------------------------------

    def is_current(self, fight_id: int, stream: str, subject: str, blob_hash: str) -> bool:
        """Whether this blob is already in, under the schema now in force."""
        row = self.conn.execute(
            """
            SELECT blob_sha256, schema_version FROM import_log
            WHERE report_code = ? AND fight_id = ? AND stream = ? AND subject = ?
            """,
            (self.meta.code, fight_id, stream, subject),
        ).fetchone()
        if row is None:
            return False
        return (
            row["blob_sha256"] == blob_hash
            and row["schema_version"] == self.schema_version
        )

    def _record(self, fight_id: int, stream: str, subject: str, blob_hash: str,
                rows: int) -> None:
        self.conn.execute(
            """
            INSERT INTO import_log (report_code, fight_id, stream, subject,
                                    blob_sha256, row_count, imported_at,
                                    schema_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(report_code, fight_id, stream, subject) DO UPDATE SET
                blob_sha256 = excluded.blob_sha256,
                row_count = excluded.row_count,
                imported_at = excluded.imported_at,
                schema_version = excluded.schema_version
            """,
            (self.meta.code, fight_id, stream, subject, blob_hash, rows, self.now,
             self.schema_version),
        )

    def import_event_blob(
        self,
        fight: Fight,
        stream: Stream,
        subject: str,
        summary: ImportSummary,
        force: bool,
    ) -> None:
        path = self.cache.path(self.meta.code, fight.id, stream.key, subject)
        blob = self.cache.read(path)
        if blob is None:
            summary.missing.append((fight.id, stream.key, subject))
            return

        if not force and self.is_current(fight.id, stream.key, subject, blob.content_hash):
            summary.skipped += 1
            return

        rows = map_events(
            self.meta.code,
            fight.id,
            fight.start_ms,
            stream.key,
            subject,
            blob.events,
            self.actors,
        )

        with self.conn:
            self.conn.execute(
                """
                DELETE FROM event
                WHERE report_code = ? AND fight_id = ? AND stream = ? AND subject = ?
                """,
                (self.meta.code, fight.id, stream.key, subject),
            )
            self.conn.executemany(EVENT_INSERT, rows)
            self._record(fight.id, stream.key, subject, blob.content_hash, len(rows))

        summary.imported += 1
        summary.rows += len(rows)
        summary.per_stream[stream.key] = summary.per_stream.get(stream.key, 0) + len(rows)

    def import_fight_players(
        self,
        fight: Fight,
        subjects: list[Actor],
        summary: ImportSummary,
        force: bool,
    ) -> None:
        """Populate fight_player from CombatantInfo and that pull's player details."""
        combatant_path = self.cache.path(
            self.meta.code, fight.id, COMBATANT_STREAM, RAID_SUBJECT
        )
        details_path = self.cache.path(
            self.meta.code, fight.id, "player_details", RAID_SUBJECT, kind="meta"
        )
        combatant = self.cache.read(combatant_path)
        details_blob = self.cache.read(details_path)

        if combatant is None and details_blob is None:
            summary.missing.append((fight.id, COMBATANT_STREAM, RAID_SUBJECT))
            return

        combined_hash = "+".join(
            b.content_hash if b else "-" for b in (combatant, details_blob)
        )
        if not force and self.is_current(fight.id, COMBATANT_STREAM, RAID_SUBJECT, combined_hash):
            summary.skipped += 1
            return

        from ..fetch.meta import iter_player_details

        details = {}
        if details_blob is not None:
            details = {
                entry["id"]: entry
                for entry in iter_player_details(details_blob.payload.get("data"))
                if entry.get("id") is not None
            }

        present = self.meta.subjects_in_fight(fight, subjects)
        rows = combatant_rows(
            self.meta.code,
            fight.id,
            combatant.events if combatant else [],
            self.meta.actors,
            details,
            {a.id for a in present},
        )

        with self.conn:
            self.conn.execute(
                "DELETE FROM fight_player WHERE report_code = ? AND fight_id = ?",
                (self.meta.code, fight.id),
            )
            self.conn.executemany(
                """
                INSERT INTO fight_player (report_code, fight_id, actor_id, player_key,
                                          class, spec, spec_id, role, item_level,
                                          talent_hash, talent_json, is_subject)
                VALUES (:report_code, :fight_id, :actor_id, :player_key, :class,
                        :spec, :spec_id, :role, :item_level, :talent_hash,
                        :talent_json, :is_subject)
                """,
                [{**{"spec": None, "role": None}, **row} for row in rows],
            )
            self._record(fight.id, COMBATANT_STREAM, RAID_SUBJECT, combined_hash, len(rows))

        summary.imported += 1
        summary.per_stream[COMBATANT_STREAM] = (
            summary.per_stream.get(COMBATANT_STREAM, 0) + len(rows)
        )

    # -- entry point -----------------------------------------------------

    def run(
        self,
        fights: list[Fight],
        subjects: list[Actor],
        streams: list[Stream],
        force: bool = False,
    ) -> ImportSummary:
        summary = ImportSummary(report_code=self.meta.code)
        self.import_dimensions()

        for fight in fights:
            self.import_fight(fight)
            summary.fights += 1
            present = self.meta.subjects_in_fight(fight, subjects)

            for stream in streams:
                if stream.key == COMBATANT_STREAM:
                    continue
                if stream.scope is Scope.SUBJECT:
                    for actor in present:
                        self.import_event_blob(
                            fight, stream, subject_slug(stream, actor.player_key),
                            summary, force,
                        )
                else:
                    self.import_event_blob(
                        fight, stream, subject_slug(stream, None), summary, force
                    )

            self.import_fight_players(fight, subjects, summary, force)

        return summary


@dataclass
class PruneSummary:
    removed_rows: int = 0
    removed_blobs: int = 0
    per_stream: dict[str, int] = field(default_factory=dict)

    @property
    def streams(self) -> list[str]:
        return sorted(self.per_stream)


def prune_streams(
    conn: sqlite3.Connection,
    report_code: str | None,
    keep: list[str],
    dry_run: bool = False,
) -> PruneSummary:
    """Remove imported streams outside ``keep``.

    Narrowing the import list leaves behind whatever a previous, wider import
    put in. Rebuilding from cache would also work and costs no API points, but
    this is the surgical form for a database holding many nights.
    """
    summary = PruneSummary()
    where = "stream NOT IN ({})".format(",".join("?" * len(keep)))
    params: list = list(keep)
    if report_code:
        where += " AND report_code = ?"
        params.append(report_code)

    for row in conn.execute(
        f"SELECT stream, COUNT(*) AS n FROM event WHERE {where} GROUP BY stream",
        params,
    ):
        summary.per_stream[row["stream"]] = row["n"]
        summary.removed_rows += row["n"]

    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM import_log WHERE {where}", params
    ).fetchone()
    summary.removed_blobs = row["n"]

    if dry_run or not summary.removed_rows and not summary.removed_blobs:
        return summary

    with conn:
        conn.execute(f"DELETE FROM event WHERE {where}", params)
        conn.execute(f"DELETE FROM import_log WHERE {where}", params)
    conn.execute("VACUUM")
    return summary


def database_stats(conn: sqlite3.Connection, report_code: str | None = None) -> dict:
    """Row counts for the tables a person would want to browse."""
    stats: dict[str, int] = {}
    for table in ("report", "fight", "actor", "player", "ability", "fight_player",
                  "event", "import_log"):
        if report_code and table in ("event", "fight", "fight_player", "import_log", "actor"):
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE report_code = ?",
                (report_code,),
            ).fetchone()
        else:
            row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        stats[table] = row["n"]
    return stats
