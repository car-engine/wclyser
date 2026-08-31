"""Command line entry point.

Only the client and fetch layers exist so far, so the commands here are the
ones that read the API: ``fights``, ``players``, ``budget`` and ``fetch``.
``import``, ``analyse``, ``report`` and ``watch`` arrive with the layers above.
"""

from __future__ import annotations

import argparse
import logging
import sys

from .client import GraphQLClient
from .config import load_config
from .errors import WCLError
from .fetch import Fetcher, resolve_streams
from .fetch.meta import select_fights
from .fetch.scopes import BY_KEY, Scope

log = logging.getLogger("wcla")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wcla", description=__doc__)
    parser.add_argument("--config", help="path to config.yaml")
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
    config = load_config(args.config)
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
