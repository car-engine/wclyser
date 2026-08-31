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
    _, _, fetcher = _context(args)
    meta = fetcher.report_meta(args.code)
    print(
        f"{meta.code}  {meta.title or '(untitled)'}  {meta.zone_name or ''}  "
        f"night {meta.raid_night}  guild {meta.guild or '-'}"
    )
    for fight in meta.fights:
        final = "" if fetcher.is_final(meta, fight) else "  [in progress]"
        print(f"  {fight.describe()}{final}")
    print(f"{len(meta.fights)} fights, {len(meta.players())} players")
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

    print(f"{meta.code}  {meta.title or '(untitled)'}  night {meta.raid_night}")
    print(f"subjects: {', '.join(a.player_key for a in subjects) or '(raid-wide only)'}")
    print(f"streams:  {', '.join(s.key for s in streams)}")
    print(f"budget:   {client.rate_limit(refresh=True)}")

    summary = fetcher.fetch_report(
        meta,
        subjects,
        streams,
        fight_ids=args.fight,
        force=args.force,
        finished_only=args.finished_only,
    )

    for fight in meta.fights:
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
