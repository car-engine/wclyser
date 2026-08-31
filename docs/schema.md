# Database schema

Schema version 1. The authoritative definition is
`wcl_analyser/db/migrations/001_initial.sql`; this describes why it is shaped
that way.

## Times

`report.start_time_utc` is unix milliseconds. Every other timestamp is
milliseconds relative to it, which is how the API reports them. On an event,
`ts_ms` is report-relative and `t_rel_ms` is relative to the start of that
pull. Wall clock is `report.start_time_utc + ts_ms`.

## Tables

| Table | One row per |
|---|---|
| `report` | report |
| `fight` | pull |
| `actor` | actor in a report, including pets and NPCs |
| `player` | character, across every report |
| `ability` | spell, across every report |
| `fight_player` | player in a pull, with that pull's specialisation |
| `event` | event |
| `import_log` | imported blob |
| `table_snapshot` | pre-aggregated `table()` or `graph()` response |
| `metric` | one number from one analysis module |

### Why one wide `event` table

Analysis crosses event types constantly — what was cast in the seconds before
a death, whether a defensive was up when a boss ability landed. A single
timeline ordered by `t_rel_ms` makes each of those one query. The cost is a
sparse table, and SQLite stores a null in about a byte.

`t_rel_ms` and `source_key` are denormalised onto every row because they are
the axes nearly every query uses, and computing either would otherwise need a
join.

### Pets resolve to their owner

`source_key` on a pet's event holds the **owner's** player key, so pet damage
aggregates with the player's own. Across the eleven sample pulls that is
26,961,200 of 383,007,132 damage, or 7.0%, from the Infernal, the Overfiend
and the permanent pet. `source_id` still holds the pet's own actor id, and
`source_is_pet` marks the row.

### Specialisation lives on the pull

`fight_player` carries `spec`, `spec_id`, `item_level` and `talent_hash` for
each player in each pull. A player who respecs between bosses has one
specialisation for some pulls and another for the rest, so a profile keyed on
the player alone would be wrong for half a night. `talent_hash` makes a
mid-night talent change visible.

`is_subject` marks the players a pull was fetched for. Without it, analysis
cannot tell complete event coverage from the incidental appearances a
non-subject makes in the raid-wide streams.

### Pull numbering

`fight.pull_number` counts within one report. The sequence across raid nights
is `v_pull.pull_seq`, a window function computed on read, because a stored
number would go stale the moment an older night is imported late.

## Two field-name traps

Both are handled in `wcl_analyser/db/mappers.py`, and both would corrupt a
column silently.

**`absorb` against `absorbed`.** In `damage_taken`, `absorbed` is the portion
of that hit absorbed. In `damage_done`, `absorb` is the actor's own shield
pool, attached by `includeResources`. The mapper reads `absorbed` and never
`absorb`.

**The resource that changed against the resource the actor holds.**
`resourceChangeType` names the resource that moved; `classResources` reports
the pool the actor was carrying. For a warlock spending a soul shard those are
shards and mana respectively — a change of type 7 with a maximum of 50, beside
a pool of 242,263 out of 250,000. They get separate columns: `resource_type`,
`resource_change`, `resource_max`, `resource_waste` for the change, and
`pool_type`, `pool_amount`, `pool_max` for the snapshot.

## Idempotent import

The replacement unit matches the fetch unit: `(report_code, fight_id, stream,
subject)`. For one such tuple the importer deletes every row it produced and
inserts the blob's rows again, in one transaction. `import_log` records the
blob's hash, and a blob whose hash is unchanged is skipped without touching a
table.

That avoids needing a natural key over event fields, which would have to
distinguish two genuine events sharing a millisecond, a type, a source, a
target and an ability.

`CombatantInfo` is the exception. It is not a timeline event, so it populates
`fight_player` instead, replaced per pull rather than per stream.

## No raw JSON

The `event` table stores no copy of the original JSON. A night is 441,239
events; keeping each one's JSON would add roughly 130 MB per night, and the
gzipped blobs already hold exactly that at 7 MB per night. The blobs are the
archive, and a schema change is handled by rebuilding from them at no API
cost.

The one unmodelled field carried across is `buffs`, a dot-separated list of
the ability ids active on the actor, in `buffs_active`. It answers "was this
cast made under that buff" without a window join.

## Views

- `v_event` — the timeline with names resolved, for browsing by hand.
- `v_pull` — pulls in order across every imported night, with `pull_seq`.
- `v_subject_pull` — one row per pull per subject, with the specialisation
  played.

## Measured size

Eleven Mythic pulls of one boss, one subject, all 13 streams: 124,815 event
rows in a 50 MB database. Compacted it is 47.5 MB, of which the four indices
are 22.6 MB, so they roughly double the size. That is the trade for the query
speed; drop `event_source_abil` first if it ever needs shrinking.
