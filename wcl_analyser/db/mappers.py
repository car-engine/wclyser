"""Turning raw API events into rows.

One generic mapper covers almost every stream, because the fields it reads
carry the same meaning wherever they appear. The exceptions are handled
explicitly:

- ``absorb`` is never read. In ``damage_taken`` the absorbed portion of a hit
  is ``absorbed``, while in ``damage_done`` the similarly named ``absorb`` is
  the actor's own shield pool, added by ``includeResources``. Reading both into
  one column would silently mix two different quantities.
- The resource an event changed and the actor's own resource pool are
  different quantities. ``resourceChangeType`` names the resource that moved,
  while ``classResources`` reports the pool the actor was carrying and what the
  cast spent from it. For a warlock spending a soul shard the change is
  reported against shards and the pool against mana, so they get separate
  columns.
- Deaths name the killer in ``killerID`` and the killing blow in
  ``killingAbilityGameID``, with ``sourceID`` set to the environment.
- CombatantInfo is not a timeline event and does not go into ``event`` at all.
  It populates ``fight_player``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

EVENT_COLUMNS = (
    "report_code",
    "fight_id",
    "stream",
    "subject",
    "ts_ms",
    "t_rel_ms",
    "event_type",
    "source_id",
    "target_id",
    "source_key",
    "target_key",
    "source_is_pet",
    "ability_id",
    "extra_ability_id",
    "amount",
    "unmitigated",
    "absorbed",
    "mitigated",
    "overkill",
    "overheal",
    "hit_type",
    "tick",
    "stack",
    "resource_type",
    "resource_change",
    "resource_max",
    "resource_waste",
    "pool_type",
    "pool_amount",
    "pool_max",
    "pool_cost",
    "hp_current",
    "hp_max",
    "pos_x",
    "pos_y",
    "facing",
    "buffs_active",
)

EVENT_INSERT = (
    f"INSERT INTO event ({', '.join(EVENT_COLUMNS)}) "
    f"VALUES ({', '.join('?' * len(EVENT_COLUMNS))})"
)


class ActorIndex:
    """Resolves report-local actor ids to identities that survive re-import.

    A pet resolves to its owner's player key, so that pet damage aggregates
    with the player's own. The pet's own actor id stays in ``source_id``.
    """

    def __init__(self, actors: dict[int, Any]) -> None:
        self._key: dict[int, str | None] = {}
        self._is_pet: dict[int, bool] = {}
        for actor_id, actor in actors.items():
            owner = actors.get(actor.pet_owner) if actor.pet_owner else None
            if owner is not None and owner.is_player:
                self._key[actor_id] = owner.player_key
                self._is_pet[actor_id] = True
            elif actor.is_player:
                self._key[actor_id] = actor.player_key
                self._is_pet[actor_id] = False
            else:
                self._key[actor_id] = actor.name
                self._is_pet[actor_id] = False

    def key(self, actor_id: int | None) -> str | None:
        if actor_id is None:
            return None
        return self._key.get(actor_id)

    def is_pet(self, actor_id: int | None) -> bool:
        if actor_id is None:
            return False
        return self._is_pet.get(actor_id, False)


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def map_events(
    report_code: str,
    fight_id: int,
    fight_start_ms: int,
    stream: str,
    subject: str,
    events: Iterable[dict],
    actors: ActorIndex,
) -> list[tuple]:
    rows: list[tuple] = []
    is_deaths = stream == "deaths"

    for ev in events:
        source_id = _int(ev.get("sourceID"))
        ability_id = _int(ev.get("abilityGameID"))

        if is_deaths:
            killer = _int(ev.get("killerID"))
            if killer is not None:
                source_id = killer
            killing = _int(ev.get("killingAbilityGameID"))
            if killing:
                ability_id = killing

        target_id = _int(ev.get("targetID"))
        ts_ms = _int(ev.get("timestamp")) or 0

        # The resource that changed, kept apart from the pool the actor was
        # carrying. These are routinely different resources.
        resource_type = _int(ev.get("resourceChangeType"))
        resource_max = _int(ev.get("maxResourceAmount"))
        pool_type = pool_amount = pool_max = pool_cost = None
        class_resources = ev.get("classResources") or []
        if class_resources:
            pool = class_resources[0]
            if resource_type is not None:
                # Prefer the entry describing the resource that moved.
                for entry in class_resources:
                    if _int(entry.get("type")) == resource_type:
                        pool = entry
                        break
            pool_type = _int(pool.get("type"))
            pool_amount = _int(pool.get("amount"))
            pool_max = _int(pool.get("max"))
            pool_cost = _int(pool.get("cost"))

        rows.append(
            (
                report_code,
                fight_id,
                stream,
                subject,
                ts_ms,
                ts_ms - fight_start_ms,
                ev.get("type") or "unknown",
                source_id,
                target_id,
                actors.key(source_id),
                actors.key(target_id),
                1 if actors.is_pet(source_id) else 0,
                ability_id,
                _int(ev.get("extraAbilityGameID")),
                _int(ev.get("amount")),
                _int(ev.get("unmitigatedAmount")),
                _int(ev.get("absorbed")),
                _int(ev.get("mitigated")),
                _int(ev.get("overkill")),
                _int(ev.get("overheal")),
                _int(ev.get("hitType")),
                1 if ev.get("tick") else None,
                _int(ev.get("stack")),
                resource_type,
                _int(ev.get("resourceChange")),
                resource_max,
                _int(ev.get("waste")),
                pool_type,
                pool_amount,
                pool_max,
                pool_cost,
                _int(ev.get("hitPoints")),
                _int(ev.get("maxHitPoints")),
                _int(ev.get("x")),
                _int(ev.get("y")),
                _int(ev.get("facing")),
                ev.get("buffs"),
            )
        )
    return rows


def talent_fingerprint(talent_tree: list[dict] | None) -> tuple[str | None, str | None]:
    """Hash and JSON of a talent build, so a mid-night respec is visible."""
    if not talent_tree:
        return None, None
    ordered = sorted(
        ((t.get("nodeID"), t.get("id"), t.get("rank")) for t in talent_tree),
        key=lambda t: (t[0] or 0, t[1] or 0),
    )
    body = json.dumps(ordered, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16], body


def combatant_rows(
    report_code: str,
    fight_id: int,
    events: Iterable[dict],
    actors: dict[int, Any],
    details: dict[int, dict],
    subject_ids: set[int],
) -> list[dict]:
    """Build fight_player rows from CombatantInfo and that pull's player details.

    CombatantInfo supplies the numeric specialisation id, gear and talents.
    Player details supply the specialisation name, which needs no table of
    specialisation ids that a new specialisation would fall out of.
    """
    rows: dict[int, dict] = {}

    for ev in events:
        actor_id = _int(ev.get("sourceID"))
        actor = actors.get(actor_id)
        if actor is None or not actor.is_player:
            continue
        talent_hash, talent_json = talent_fingerprint(ev.get("talentTree"))
        gear = [g.get("itemLevel") for g in (ev.get("gear") or []) if g.get("itemLevel")]
        rows[actor_id] = {
            "report_code": report_code,
            "fight_id": fight_id,
            "actor_id": actor_id,
            "player_key": actor.player_key,
            "class": actor.player_class,
            "spec": None,
            "spec_id": _int(ev.get("specID")),
            "role": actor.role,
            "item_level": round(sum(gear) / len(gear)) if gear else None,
            "talent_hash": talent_hash,
            "talent_json": talent_json,
            "is_subject": 1 if actor_id in subject_ids else 0,
        }

    for actor_id, detail in details.items():
        actor = actors.get(actor_id)
        if actor is None or not actor.is_player:
            continue
        specs = detail.get("specs") or []
        spec = max(specs, key=lambda s: s.get("count", 0)).get("spec") if specs else None
        row = rows.get(actor_id)
        if row is None:
            row = {
                "report_code": report_code,
                "fight_id": fight_id,
                "actor_id": actor_id,
                "player_key": actor.player_key,
                "class": actor.player_class,
                "spec_id": None,
                "talent_hash": None,
                "talent_json": None,
                "item_level": None,
                "is_subject": 1 if actor_id in subject_ids else 0,
            }
            rows[actor_id] = row
        row["spec"] = spec
        row["role"] = detail.get("role") or row.get("role")
        if detail.get("maxItemLevel"):
            row["item_level"] = detail["maxItemLevel"]

    return list(rows.values())
