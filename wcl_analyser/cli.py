"""Command line entry point.

``budget``, ``fights``, ``players`` and ``fetch`` read the API. ``import``,
``coverage`` and ``stats`` read only the cache and the database, and need no
credentials. ``analyse``, ``report`` and ``watch`` arrive with the layers
above.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .client import GraphQLClient
from .config import load_config
from .errors import WCLError
from .fetch import Fetcher, resolve_streams
from .fetch.meta import load_offline_meta, select_fights
from .db import (
    Importer,
    check as check_coverage,
    database_stats,
    open_database,
    prune_streams,
    schema_version,
)
from .fetch.scopes import BY_KEY, Scope

log = logging.getLogger("wcla")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wcla", description=__doc__)
    parser.add_argument("--config", help="path to config.yaml")
    parser.add_argument(
        "--database", help="path to the SQLite file; overrides the one in data_dir"
    )
    parser.add_argument("-v", "--verbose", action="count", default=0)
    sub = parser.add_subparsers(dest="command", required=True)

    budget = sub.add_parser("budget", help="show the hourly API point budget")
    budget.set_defaults(func=cmd_budget)

    fights = sub.add_parser("fights", help="list the fights in a report")
    fights.add_argument("code")
    _add_focus_flags(fights)
    fights.set_defaults(func=cmd_fights)

    players = sub.add_parser("players", help="list the players in a report")
    players.add_argument("code")
    players.add_argument("--player", action="append", help="resolve these subjects")
    players.set_defaults(func=cmd_players)

    fetch = sub.add_parser("fetch", help="fetch and cache raw events for a report")
    fetch.add_argument("code")
    fetch.add_argument(
        "--player",
        action="append",
        help="subject to fetch, repeatable; overrides configuration",
    )
    fetch.add_argument(
        "--stream",
        action="append",
        help=f"stream to fetch, repeatable. Known: {', '.join(sorted(BY_KEY))}",
    )
    fetch.add_argument("--fight", action="append", type=int, help="fight id, repeatable")
    _add_focus_flags(fetch)
    fetch.add_argument("--force", action="store_true", help="ignore cached blobs")
    fetch.add_argument(
        "--finished-only",
        action="store_true",
        help="skip a pull that may still be in progress",
    )
    fetch.set_defaults(func=cmd_fetch)

    imp = sub.add_parser(
        "import", help="import cached blobs into SQLite; never touches the API"
    )
    imp.add_argument("code")
    imp.add_argument("--player", action="append", help="subject, repeatable")
    imp.add_argument(
        "--stream",
        action="append",
        help="stream to import, repeatable; overrides the import block",
    )
    imp.add_argument("--fight", action="append", type=int, help="fight id, repeatable")
    imp.add_argument("--force", action="store_true", help="re-import unchanged blobs")
    _add_focus_flags(imp)
    imp.set_defaults(func=cmd_import)

    cov = sub.add_parser(
        "coverage", help="what an analysis needs against what has been imported"
    )
    cov.add_argument("code")
    cov.add_argument("--player", action="append", help="subject, repeatable")
    cov.add_argument(
        "--stream", action="append", help="stream the analysis needs, repeatable"
    )
    _add_focus_flags(cov)
    cov.set_defaults(func=cmd_coverage)

    prune = sub.add_parser(
        "prune", help="drop imported streams outside the configured import list"
    )
    prune.add_argument("code", nargs="?")
    prune.add_argument(
        "--keep", action="append", help="stream to keep, repeatable; overrides config"
    )
    prune.add_argument("--dry-run", action="store_true", help="report without deleting")
    prune.set_defaults(func=cmd_prune)

    stats = sub.add_parser("stats", help="row counts and the imported pulls")
    stats.add_argument("code", nargs="?")
    stats.set_defaults(func=cmd_stats)

    streams = sub.add_parser("streams", help="show the fetch plan")
    streams.set_defaults(func=cmd_streams)

    return parser


def _add_focus_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--encounter", help="boss name substring or encounter id; narrows the pulls"
    )
    parser.add_argument(
        "--difficulty", help="LFR, Normal, Heroic, Mythic, Mythic+, or the raw id"
    )
    parser.add_argument(
        "--spec",
        help="keep only pulls where a subject played this specialisation",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="ignore the focus block in configuration",
    )


def _load(args: argparse.Namespace):
    config = load_config(args.config)
    if getattr(args, "database", None):
        config.database = Path(args.database)
    return config


def _focus(args: argparse.Namespace, config) -> tuple:
    """Command line focus flags, falling back to the configured focus."""
    if getattr(args, "all", False):
        return None, None, None
    focus = config.focus
    return (
        getattr(args, "encounter", None) or focus.encounter,
        getattr(args, "difficulty", None) or focus.difficulty,
        getattr(args, "spec", None) or focus.spec,
    )


def _context(args: argparse.Namespace) -> tuple:
    config = _load(args)
    client = GraphQLClient(config)
    return config, client, Fetcher(client, config)


def cmd_budget(args: argparse.Namespace) -> int:
    _, client, _ = _context(args)
    print(client.rate_limit(refresh=True))
    return 0


def cmd_streams(args: argparse.Namespace) -> int:
    for stream in resolve_streams(None):
        side = stream.filter_side.value
        print(
            f"{stream.key:<18} {stream.data_type:<14} {stream.scope.value:<8} "
            f"{stream.hostility:<11} filter by {side}"
        )
    return 0


def cmd_fights(args: argparse.Namespace) -> int:
    config, _, fetcher = _context(args)
    meta = fetcher.report_meta(args.code)
    encounter, difficulty, spec = _focus(args, config)
    chosen = {f.id for f in select_fights(meta.fights, encounter, difficulty)}
    print(
        f"{meta.code}  {meta.title or '(untitled)'}  {meta.zone_name or ''}  "
        f"night {meta.raid_night}  guild {meta.guild or '-'}"
    )
    for fight in meta.fights:
        mark = "*" if fight.id in chosen else " "
        final = "" if fetcher.is_final(meta, fight) else "  [in progress]"
        print(f" {mark} {fight.describe()}{final}")
    print(
        f"{len(meta.fights)} fights, {len(meta.players())} players. "
        f"{len(chosen)} in focus ({config.focus.describe() if not args.all else 'everything'})"
    )
    return 0


def cmd_players(args: argparse.Namespace) -> int:
    config, _, fetcher = _context(args)
    meta = fetcher.report_meta(args.code)
    subjects = {a.id for a in meta.resolve_subjects(config, args.player)}
    for actor in sorted(meta.players(), key=lambda a: (a.sub_type or "", a.name)):
        mark = "*" if actor.id in subjects else " "
        specs = "/".join(actor.specs_played) or actor.spec or "?"
        swap = "  (spec swapped during the report)" if actor.swapped_spec else ""
        print(
            f" {mark} {actor.id:>3}  {actor.player_key:<34} "
            f"{actor.player_class or '?':<12} {specs:<26}{actor.role or '':<7}{swap}"
        )
    print(f"{len(subjects)} subject(s) marked with *")
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    config, client, fetcher = _context(args)
    meta = fetcher.report_meta(args.code)
    streams = resolve_streams(args.stream)
    subjects = meta.resolve_subjects(config, args.player)

    if not subjects and any(s.scope is Scope.SUBJECT for s in streams):
        raise WCLError(
            "no subjects resolved. Pass --player, or set subjects in config.yaml. "
            f"Players in this report: "
            f"{', '.join(sorted(a.player_key for a in meta.players()))}"
        )

    encounter, difficulty, spec = _focus(args, config)
    fights = select_fights(meta.fights, encounter, difficulty, args.fight)
    if spec:
        fights = fetcher.filter_fights_by_spec(meta, fights, subjects, spec)

    print(f"{meta.code}  {meta.title or '(untitled)'}  night {meta.raid_night}")
    print(f"subjects: {', '.join(a.player_key for a in subjects) or '(raid-wide only)'}")
    print(f"streams:  {', '.join(s.key for s in streams)}")
    print(f"focus:    {config.focus.describe() if not args.all else 'everything'}")
    print(f"pulls:    {len(fights)} of {len(meta.fights)}")
    print(f"budget:   {client.rate_limit(refresh=True)}")

    if not fights:
        print()
        print("nothing to fetch: the focus matched no pulls in this report")
        return 0

    summary = fetcher.fetch_report(
        meta,
        subjects,
        streams,
        fights=fights,
        force=args.force,
        finished_only=args.finished_only,
    )

    for fight in fights:
        rows = [r for r in summary.results if r.fight_id == fight.id]
        if not rows:
            continue
        events = sum(r.event_count for r in rows)
        cached = sum(1 for r in rows if r.cached)
        print(
            f"  {fight.describe()}  {events:>7} events  "
            f"{cached}/{len(rows)} blobs from cache"
        )

    for fight_id, reason in summary.skipped_fights:
        print(f"  fight {fight_id} skipped: {reason}")

    print(
        f"\n{summary.events} events across {len(summary.results)} blobs "
        f"({summary.cached} cached, {summary.fetched} fetched) "
        f"in {client.requests_made} requests"
    )
    print(f"budget:   {client.rate_limit(refresh=True)}")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    config = _load(args)
    meta = load_offline_meta(config.raw_dir, args.code)
    streams = resolve_streams(args.stream or config.import_.streams)
    subjects = meta.resolve_subjects(config, args.player)

    encounter, difficulty, spec = _focus(args, config)
    fights = select_fights(meta.fights, encounter, difficulty, args.fight)
    if spec:
        # Resolved from each pull's cached player details, so import stays
        # offline. A pull whose details were never fetched cannot be judged and
        # is left out.
        fights = _filter_cached_spec(config, meta, fights, subjects, spec)

    conn = open_database(config.db_path)
    print(f"{meta.code}  {meta.title or '(untitled)'}  night {meta.raid_night}")
    print(f"database: {config.db_path}  (schema version {schema_version(conn)})")
    print(f"subjects: {', '.join(a.player_key for a in subjects) or '(raid-wide only)'}")
    print(f"streams:  {', '.join(s.key for s in streams)}")
    print(f"focus:    {config.focus.describe() if not args.all else 'everything'}")
    print(f"pulls:    {len(fights)} of {len(meta.fights)}")

    importer = Importer(conn, config, meta)
    summary = importer.run(fights, subjects, streams, force=args.force)

    print()
    for stream, rows in sorted(summary.per_stream.items(), key=lambda kv: -kv[1]):
        print(f"  {stream:<20} {rows:>8} rows")
    print()
    print(
        f"{summary.fights} pulls, {summary.rows} event rows, "
        f"{summary.imported} blobs imported, {summary.skipped} unchanged"
    )
    if summary.missing:
        print()
        print(f"{len(summary.missing)} expected blobs are not in the cache:")
        for fight_id, stream, subject in summary.missing[:20]:
            print(f"  fight {fight_id:>3}  {stream:<20} {subject}")
        if len(summary.missing) > 20:
            print(f"  ... and {len(summary.missing) - 20} more")
        print(f"Run 'wcla fetch {meta.code}' and import again.")
    conn.close()
    return 0


def _filter_cached_spec(config, meta, fights, subjects, spec):
    """Specialisation filter that reads only the cache."""
    from .fetch.cache import BlobCache
    from .fetch.meta import iter_player_details
    from .fetch.scopes import RAID_SUBJECT

    cache = BlobCache(config.raw_dir)
    wanted = spec.strip().lower()
    kept = []
    for fight in fights:
        blob = cache.read(
            cache.path(meta.code, fight.id, "player_details", RAID_SUBJECT, kind="meta")
        )
        if blob is None:
            log.warning("fight %d has no cached player details; skipped", fight.id)
            continue
        details = {e["id"]: e for e in iter_player_details(blob.payload.get("data"))}
        for actor in meta.subjects_in_fight(fight, subjects):
            entry = details.get(actor.id) or {}
            specs = entry.get("specs") or []
            best = max(specs, key=lambda x: x.get("count", 0)).get("spec") if specs else None
            if (best or "").lower() == wanted:
                kept.append(fight)
                break
    return kept


def cmd_coverage(args: argparse.Namespace) -> int:
    config = _load(args)
    meta = load_offline_meta(config.raw_dir, args.code)
    subjects = meta.resolve_subjects(config, args.player)
    encounter, difficulty, spec = _focus(args, config)
    fights = select_fights(meta.fights, encounter, difficulty, None)
    if spec:
        fights = _filter_cached_spec(config, meta, fights, subjects, spec)

    required = args.stream or [s.key for s in resolve_streams(config.import_.streams)]
    conn = open_database(config.db_path, read_only=True)
    coverage = check_coverage(conn, config, meta, fights, subjects, required)
    conn.close()

    print(f"{meta.code}  {len(fights)} pulls  streams: {', '.join(required)}")
    if coverage.complete:
        print(f"all {coverage.checked} required blobs are imported")
        return 0
    print(coverage.message())
    return 1


def cmd_prune(args: argparse.Namespace) -> int:
    config = _load(args)
    keep = args.keep or config.import_.streams
    if not keep:
        raise WCLError(
            "nothing to prune against: set import.streams in config.yaml, or pass --keep"
        )
    keep = [s.key for s in resolve_streams(keep)]
    # CombatantInfo feeds fight_player rather than the event table, and is
    # always wanted, so it is never a pruning candidate.
    keep = sorted(set(keep) | {"combatant_info"})

    conn = open_database(config.db_path)
    summary = prune_streams(conn, args.code, keep, dry_run=args.dry_run)
    verb = "would remove" if args.dry_run else "removed"
    print(f"keeping: {', '.join(keep)}")
    if not summary.removed_rows and not summary.removed_blobs:
        print("nothing to prune")
    else:
        for stream, rows in sorted(summary.per_stream.items(), key=lambda kv: -kv[1]):
            print(f"  {stream:<20} {rows:>8} rows")
        print(
            f"{verb} {summary.removed_rows} event rows across "
            f"{summary.removed_blobs} imported blobs"
        )
    conn.close()
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    config = _load(args)
    conn = open_database(config.db_path, read_only=True)
    print(f"database: {config.db_path}  (schema version {schema_version(conn)})")
    for table, count in database_stats(conn, args.code).items():
        print(f"  {table:<14} {count:>9}")

    rows = conn.execute(
        """
        SELECT raid_night, boss_name, difficulty_name, pull_number, outcome,
               duration_s, player_key, spec
        FROM v_subject_pull ORDER BY raid_night, pull_number
        """
    ).fetchall()
    if rows:
        print()
        print("subject pulls:")
        for r in rows:
            print(
                f"  {r['raid_night']}  {r['boss_name'][:24]:<25} "
                f"{r['difficulty_name']:<8} pull {r['pull_number']:>2}  "
                f"{r['outcome']:<4} {r['duration_s']:>6.0f}s  "
                f"{r['player_key']:<28} {r['spec'] or '?'}"
            )
    conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    level = logging.WARNING - min(args.verbose, 2) * 10
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")
    try:
        return args.func(args)
    except WCLError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
