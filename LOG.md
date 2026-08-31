# Log

What has been built, and what is still open. Newest entries first.

## Open items

- **Debuffs on enemies cannot be narrowed to one applier.** Under
  `hostilityType: Enemies` the actor id filters resolve against enemy indices
  the report does not expose, and every filtered form returned zero rows.
  `filterExpression` returned zero as well. The stream is fetched raid-wide as
  a result, at roughly 7,650 events per mythic pull, and has to be narrowed at
  import. Worth revisiting: try `filterExpression` with a different syntax,
  check whether the enemy indices can be recovered from `enemyNPCs` on the
  fight, or use `table(dataType: Debuffs)` where only uptime is needed.
- **`filterExpression` returned nothing in every form tried**, including
  `source.id = 5` and `target.id = 5`, on streams where the equivalent id
  filter worked. Either the syntax is different from what was tried or the
  feature needs something this client does not have. Unresolved.
- **The difficulty id map is mostly unverified.** Only 5 = Mythic is confirmed
  against a real report. `LFR`, `Normal`, `Heroic` and `Mythic+` in
  `wcl_analyser/fetch/meta.py` are assumed and should be checked against a
  report at each difficulty.
- **The `Healing` stream is filtered from the target side**, giving healing
  received. A healer subject would want it filtered from the source side. The
  fetch plan has no per-subject stream overrides yet.
- **Soul Shard generation is not logged anywhere directly.** The level is
  sampled at each spend, from `classResources` on the cast, so generation has
  to be inferred from the level between consecutive spends. See
  `docs/wcl-api.md`. Whether a fragment-level source exists is unchecked.
- **Analysis modules await design input.** The intent is specific patterns the
  user wants examined, not generic throughput statistics.

## 2026-08-31, later still

Import narrowed, and the feedback path for missing data built.

`import.streams` in `config.yaml` decides what reaches the database, now set to
`casts` and `resources`. The same eleven pulls import to 47.5 MB with all
thirteen streams and **1.55 MB** with those two, a factor of thirty. Fetching
stays wide: a blob costs points once and then sits on disk, while a row costs
space in a file meant to grow across a tier.

Added:

- `wcla coverage`, and `db.coverage.require` for modules to call. It compares
  the streams an analysis declares against `import_log` and reports the gap
  with the command that fixes it, distinguishing a stream that is cached and
  merely unimported, where the fix is free, from one never fetched, where the
  fix costs points.
- `wcla prune`, to drop streams the import list no longer wants without a full
  rebuild. `combatant_info` is never a pruning candidate, because it feeds
  `fight_player` rather than `event`.
- `--database <path>` on any command, for a side-by-side rebuild.
- `import_log.schema_version`. A migration that adds a column leaves existing
  rows null in it, so an unchanged blob hash is no longer enough on its own to
  skip the work. Found the hard way: migration 002 added `pool_cost` and every
  existing cast row stayed null in it.

**Soul Shards are on the cast events, not in the `Resources` stream.** The
`Resources` dataType gave 207 shard events all with `resourceChange: 0`, and
`graph(dataType: Resources)` returned an empty series. The real data is
`classResources` on a cast that spends shards: type 7, amount and cost in
tenths, maximum 50. Chaos Bolt 342 casts, Shadowburn 65, Soulburn 24. The
`cost` half was being dropped, and migration 002 adds `pool_cost` to carry it.

That means the shard level is sampled at every spend rather than tracked
continuously, and the level sitting at its cap of 5.0 is directly visible.

## 2026-08-31, later

Normalise layer built. SQLite schema, a forward-only migration runner, the
event mappers and `wcla import`. Run against the focus, Nevaritius playing
Destruction on Entombed Sentinels Mythic: 11 pulls, 124,815 event rows and 220
`fight_player` rows in a 50 MB database. A second import changed nothing, with
all 143 blobs recognised as unchanged.

Import reads the blob cache and never the API, so it needs no credentials and
cannot spend points. An expected blob that is absent is reported.

`CombatantInfo.talentTree` holds the talents, settling the open question left
by the empty `talents` array. It carries ability ids, node ids
and ranks, hashed into `fight_player.talent_hash` so a mid-night respec is
visible.

Found while checking the imported data: **the resource an event changed is not
the resource the actor holds.** A warlock spending a soul shard reports
`resourceChangeType: 7` with a maximum of 50, while `classResources` reports a
mana pool of 242,263 out of 250,000. The first mapper put both in one set of
columns, which made every resource row nonsense. They now have separate
columns, `resource_*` and `pool_*`. The schema had not been committed, so this
was corrected in `001_initial.sql` and the database rebuilt from cache rather
than carried as a migration.

Also decided: no `raw` JSON column on `event`. It would add roughly 130 MB per
night where the gzipped blobs already hold the same content at 7 MB.

## 2026-08-31

Client and fetch layers built and verified against report `dYGnBvDgfjcTNqKX`,
a mythic night from 2026-08-30.

Ported from the throwaway prototype `wcl_casts_export.py`, which stays
untracked because it carries a client secret as a hardcoded default. Those
credentials were used for the verification runs and **should be rotated**.

Built:

- OAuth client-credentials flow with an on-disk token cache bound to a hash of
  the client id.
- GraphQL transport with exponential backoff on 429 and 5xx, `Retry-After`
  honoured, one transparent re-authentication on 401, and hourly point budget
  tracking.
- Cache-first fetch layer over 13 streams, keyed by
  `(report, fight, stream, subject)`.
- Encounter, difficulty and specialisation filters, driven by a `focus` block
  in `config.yaml`.
- Credentials read from environment then a gitignored `.env`.
- Offline checks with a fake client, needing no credentials.

Found, in the course of verifying:

- **The event filter fields do not mean what their names suggest.** With
  `hostilityType: Friendlies`, `sourceID` selects the subject player and their
  pets whichever way the event points; the dataType carries the direction.
  `DamageTaken` filtered by `sourceID` gave 862 events with real enemy
  attackers, where filtering by `targetID` gave 509 events of nothing but
  self-damage. Four stream definitions were wrong and were corrected. Recorded
  in `docs/wcl-api.md`.
- **Specialisation is a property of the pull, not the player.** Nevaritius
  played Demonology on pulls 1 to 12 and Destruction on pulls 13 to 23,
  confirmed by `specID` 266 then 267 on each fight's CombatantInfo. The
  schema therefore needs a `fight_player` table, and the analysis layer must
  key the specialisation profile on `(report, fight, player)`.
- **The prototype's CSV was malformed** because it set `useAbilityIDs: false`
  and `useActorIDs: false`. That embeds whole actor and ability objects in
  each event and removes the `sourceID` and `abilityGameID` fields the script
  then looked up, so the class and ability id columns came out empty and the
  source column held a Python dict. The package uses the id form.

Measured, for one mythic night of 23 pulls with one subject and all 13
streams: 441,239 events in 299 blobs, 308 requests, 322 of 3600 hourly points,
2m 6s, 7.0 MB gzipped. Re-running took 3.3 seconds and 2 requests with every
blob served from disk.

Decided:

- Analysis focus for the first trial is Nevaritius playing Destruction Warlock
  on Entombed Sentinels, Mythic. Eleven pulls, 125,035 events, already cached.
- Raw blobs are kept indefinitely. They are the only way to rebuild the
  database after a schema change without re-spending API points.

## Next

The analysis layer, once the patterns to examine have been designed. The
plumbing is in place: modules read only from SQLite and write to `metric`.
