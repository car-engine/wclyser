"""Command line entry point.

``budget``, ``fights``, ``players``, ``fetch`` and ``sync`` reach the API.
``import``, ``coverage``, ``prune``, ``stats`` and ``analyse`` read only the
cache and the database, and need no credentials.

``analyse`` imports a blob that is cached but not yet in the database, because
that costs nothing. It never fetches, so no command that analyses can spend
API points. ``report`` and ``watch`` arrive later.
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
from .analysis import REGISTRY as MODULES, Ctx, resolve as resolve_modules, store_metrics
from .db.coverage import ensure as ensure_coverage
from . import report as report_html

log = logging.getLogger("wcly")


class CommaList(argparse.Action):
    """A repeatable flag that also accepts comma-separated values.

    ``--player A --player B`` and ``--player A,B`` both give ``["A", "B"]``,
    and the two forms can be mixed. Conversion happens here rather than in
    argparse's ``type``, which would run against the whole comma-separated
    string and reject it.
    """

    def __init__(self, option_strings, dest, item_type=str, **kwargs):
        self._item_type = item_type
        super().__init__(option_strings, dest, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        collected = list(getattr(namespace, self.dest, None) or [])
        for part in str(values).split(","):
            part = part.strip()
            if not part:
                continue
            try:
                collected.append(self._item_type(part))
            except ValueError:
                parser.error(
                    f"{option_string}: {part!r} is not a valid "
                    f"{self._item_type.__name__}"
                )
        setattr(namespace, self.dest, collected)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wcly", description=__doc__)
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
    players.add_argument(
        "--player", action=CommaList,
        help="subject; repeat the flag or comma-separate",
    )
    players.set_defaults(func=cmd_players)

    fetch = sub.add_parser("fetch", help="fetch and cache raw events for a report")
    fetch.add_argument("code")
    fetch.add_argument(
        "--player",
        action=CommaList,
        help="subject to fetch; repeat or comma-separate. Overrides configuration",
    )
    fetch.add_argument(
        "--stream",
        action=CommaList,
        help=f"stream to fetch; repeat or comma-separate. Known: {', '.join(sorted(BY_KEY))}",
    )
    fetch.add_argument(
        "--fight", action=CommaList, item_type=int,
        help="fight id; repeat or comma-separate",
    )
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
    imp.add_argument(
        "--player", action=CommaList, help="subject; repeat or comma-separate"
    )
    imp.add_argument(
        "--stream",
        action=CommaList,
        help="stream to import; repeat or comma-separate. Overrides the import block",
    )
    imp.add_argument(
        "--fight", action=CommaList, item_type=int,
        help="fight id; repeat or comma-separate",
    )
    imp.add_argument("--force", action="store_true", help="re-import unchanged blobs")
    _add_focus_flags(imp)
    imp.set_defaults(func=cmd_import)

    cov = sub.add_parser(
        "coverage", help="what an analysis needs against what has been imported"
    )
    cov.add_argument("code")
    cov.add_argument(
        "--player", action=CommaList, help="subject; repeat or comma-separate"
    )
    cov.add_argument(
        "--stream", action=CommaList,
        help="stream the analysis needs; repeat or comma-separate"
    )
    _add_focus_flags(cov)
    cov.set_defaults(func=cmd_coverage)

    prune = sub.add_parser(
        "prune", help="drop imported streams outside the configured import list"
    )
    prune.add_argument("code", nargs="?")
    prune.add_argument(
        "--keep", action=CommaList,
        help="stream to keep; repeat or comma-separate. Overrides config"
    )
    prune.add_argument("--dry-run", action="store_true", help="report without deleting")
    prune.set_defaults(func=cmd_prune)

    stats = sub.add_parser("stats", help="row counts and the imported pulls")
    stats.add_argument("code", nargs="?")
    stats.set_defaults(func=cmd_stats)

    ana = sub.add_parser("analyse", help="run analysis modules over the database")
    ana.add_argument("code")
    ana.add_argument(
        "--player", action=CommaList, help="subject; repeat or comma-separate"
    )
    ana.add_argument(
        "--module", action=CommaList, required=True,
        help=f"analysis module to run; repeat or comma-separate. "
             f"Known: {', '.join(sorted(MODULES))}",
    )
    ana.add_argument(
        "--fight", action=CommaList, item_type=int,
        help="fight id; repeat or comma-separate",
    )
    ana.add_argument(
        "--detail", action="store_true", help="list every cast with its timing"
    )
    ana.add_argument(
        "--no-store", action="store_true", help="print without writing metrics"
    )
    ana.add_argument(
        "--no-import",
        action="store_true",
        help="fail on missing rows instead of importing blobs already cached",
    )
    ana.add_argument(
        "--html",
        nargs="?",
        const="",
        metavar="PATH",
        help="also write a self-contained HTML page. A path ending in .html "
             "names one file, anything else is a directory. Bare --html uses "
             "<data_dir>/reports",
    )
    _add_focus_flags(ana)
    ana.set_defaults(func=cmd_analyse)

    syn = sub.add_parser(
        "sync", help="fetch a report and import it, in one step"
    )
    syn.add_argument("code")
    syn.add_argument(
        "--player", action=CommaList, help="subject; repeat or comma-separate"
    )
    syn.add_argument(
        "--fight", action=CommaList, item_type=int,
        help="fight id; repeat or comma-separate",
    )
    syn.add_argument("--force", action="store_true", help="ignore cached blobs")
    syn.add_argument(
        "--finished-only", action="store_true",
        help="skip a pull that may still be in progress",
    )
    _add_focus_flags(syn)
    syn.set_defaults(func=cmd_sync)

    rep = sub.add_parser(
        "report",
        help="reserved for the cross-night trend document; not built yet",
    )
    rep.add_argument("code", nargs="?")
    rep.set_defaults(func=cmd_report)

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


def _describe_focus(encounter, difficulty, spec) -> str:
    """The focus actually in effect, which the flags may have overridden."""
    parts = [
        f"{label} {value}"
        for label, value in (
            ("encounter", encounter), ("difficulty", difficulty), ("spec", spec)
        )
        if value is not None
    ]
    return ", ".join(parts) or "everything"


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
        f"{len(chosen)} in focus ({_describe_focus(encounter, difficulty, spec)})"
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
    print(f"focus:    {_describe_focus(encounter, difficulty, spec)}")
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
    print(f"focus:    {_describe_focus(encounter, difficulty, spec)}")
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
        print(f"Run 'wcly fetch {meta.code}' and import again.")
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


def cmd_sync(args: argparse.Namespace) -> int:
    """Fetch, then import, so the usual sequence is one command.

    Fetching stays wide and importing stays narrow, exactly as the two
    commands do on their own. This spends API points, which is why it is its
    own verb rather than something analyse does behind your back.
    """
    config, client, fetcher = _context(args)
    meta = fetcher.report_meta(args.code)
    subjects = meta.resolve_subjects(config, args.player)
    streams = resolve_streams(None)

    if not subjects and any(s.scope is Scope.SUBJECT for s in streams):
        raise WCLError(
            "no subjects resolved. Pass --player, or set subjects in config.yaml."
        )

    encounter, difficulty, spec = _focus(args, config)
    fights = select_fights(meta.fights, encounter, difficulty, args.fight)
    if spec:
        fights = fetcher.filter_fights_by_spec(meta, fights, subjects, spec)
    if not fights:
        print("the focus matched no pulls in this report")
        return 0

    print(f"{meta.code}  {meta.title or '(untitled)'}  night {meta.raid_night}")
    print(f"subjects: {', '.join(a.player_key for a in subjects)}")
    print(f"focus:    {_describe_focus(encounter, difficulty, spec)}")
    print(f"pulls:    {len(fights)} of {len(meta.fights)}")
    print(f"budget:   {client.rate_limit(refresh=True)}")

    print()
    print("fetching every stream")
    summary = fetcher.fetch_report(
        meta, subjects, streams, fights=fights,
        force=args.force, finished_only=args.finished_only,
    )
    print(f"  {summary.events} events across {len(summary.results)} blobs "
          f"({summary.cached} cached, {summary.fetched} fetched)")
    for fight_id, reason in summary.skipped_fights:
        print(f"  fight {fight_id} skipped: {reason}")

    import_streams = resolve_streams(config.import_.streams)
    print()
    print(f"importing {', '.join(s.key for s in import_streams)}")
    conn = open_database(config.db_path)
    imported = Importer(conn, config, meta).run(fights, subjects, import_streams)
    print(f"  {imported.rows} event rows, {imported.imported} blobs imported, "
          f"{imported.skipped} unchanged")
    if imported.missing:
        print(f"  {len(imported.missing)} expected blobs are still absent")
    conn.close()

    print()
    print(f"budget:   {client.rate_limit(refresh=True)}")
    return 0


def cmd_analyse(args: argparse.Namespace) -> int:
    config = _load(args)
    meta = load_offline_meta(config.raw_dir, args.code)
    subjects, fights = _analysis_scope(args, config, meta)
    modules = resolve_modules(args.module)
    conn = open_database(config.db_path)

    pages_to = None
    if args.html is not None:
        pages_to = Path(args.html) if args.html else config.data_dir / "reports"
    single_file = pages_to is not None and pages_to.suffix.lower() == ".html"
    if single_file and (len(subjects) > 1 or len(modules) > 1):
        raise WCLError(
            "--html names one file but several pages would be written. "
            "Give a directory instead."
        )

    print(f"{meta.code}  night {meta.raid_night}  {len(fights)} pulls")
    print(f"subjects: {', '.join(a.player_key for a in subjects)}")
    print(f"modules:  {', '.join(m.key for m in modules)}")

    ctx = Ctx(
        conn=conn,
        config=config,
        meta=meta,
        fights=fights,
        subjects=subjects,
        options={"detail": args.detail},
    )

    stored = 0
    written: list[Path] = []
    for module in modules:
        # A gap whose blob is cached is closed here, which is free. A gap
        # needing a fetch raises with the command, so this never reaches the
        # network.
        _, imported = ensure_coverage(
            conn, config, meta, fights, subjects, sorted(module.requires),
            allow_import=not args.no_import,
        )
        if imported is not None and imported.rows:
            print(f"  imported {imported.rows} rows from "
                  f"{imported.imported} cached blobs for {module.key}")
        results = module.run(ctx)
        for player_key, result in results.items():
            print()
            print(f"== {result.title} ==")
            for line in result.lines:
                print(line)
            for warning in result.warnings:
                print(f"  note: {warning}")
            if not args.no_store:
                stored += store_metrics(conn, meta.code, player_key, result)

            if pages_to is None:
                continue
            if not report_html.available(module.key):
                print(f"  no page template for {module.key}; terminal output only")
                continue
            if not result.payload:
                print(f"  no data to render for {player_key}")
                continue
            path = pages_to if single_file else pages_to / report_html.filename(
                meta.code, player_key, module.key
            )
            report_html.write(
                path,
                report_html.render(
                    module.key, result.payload,
                    footer_note=f"wclyser · {meta.code}",
                ),
            )
            written.append(path)
            print(f"  page: {path}  ({path.stat().st_size // 1024} KB)")

    print()
    if stored:
        print(f"{stored} metrics written")
    if written:
        print(f"{len(written)} page(s) written; each opens on its own.")
    conn.close()
    return 0


def _analysis_scope(args, config, meta):
    """Subjects and pulls for a run, shared by analyse and report."""
    subjects = meta.resolve_subjects(config, args.player)
    if not subjects:
        raise WCLError(
            "no subjects resolved. Pass --player, or set subjects in config.yaml."
        )
    encounter, difficulty, spec = _focus(args, config)
    fights = select_fights(meta.fights, encounter, difficulty, args.fight)
    if spec:
        fights = _filter_cached_spec(config, meta, fights, subjects, spec)
    if not fights:
        raise WCLError("the focus matched no pulls in this report")
    return subjects, fights


def cmd_report(args: argparse.Namespace) -> int:
    """Reserved for the document that compares a night against stored history.

    Page output for a single night belongs to ``analyse --html``, which
    computes the result once and renders it both ways. What is left for this
    command is the thing it was always meant to be: a summary read from the
    metric table across nights, which does depend on analyse having run.
    """
    print("wcly report is reserved for the cross-night trend document and is")
    print("not built yet. It will read the metric table across raid nights, so")
    print("it will need 'wcly analyse' to have run for the nights it compares.")
    print()
    print("For a page covering one night, use:")
    print("  wcly analyse <code> --module havoc --html")
    return 1


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
