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
  `wclyser/fetch/meta.py` are assumed and should be checked against a
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

## 2026-09-01, import follows the module

Which streams reach the database is now decided by `--stream`, then a named
config's `import.streams`, then what the selected analysis modules declare in
their `requires`, then everything. `wcly import <code> --module havoc` stores
casts alone. `import` and `sync` both take `--module` for this, and both print
what decided the list.

Also corrected: a relative `data_dir` was resolved against the config file's
directory. Moving the saved run into `nights/` therefore moved the data store
to `nights/data` and every report read as unfetched. It resolves against the
working directory now, because config files sit together in a directory of
saved runs while the data store is one shared place.

## 2026-09-01, configuration is explicit

A config file is now read only when a command names it with `--config`. The
search of `$WCLYSER_CONFIG`, the working directory and `~/.config/wclyser` is
gone.

This came from a real confusion the same day: the configured focus said
`difficulty: Heroic`, every command silently applied it, and a Mythic report
returned nothing with no indication why. A setting that decides which pulls and
which players a command answers over belongs in the command that ran.

A config now also names the analysis modules, under `analysis.modules`, so one
file is a whole saved run: subjects, focus, modules and import streams.
`--module` is no longer required at the parser; it is required unless a named
config supplies one, and the flag wins when both do.

Defaults without a config: everything in focus, no subjects, no modules, data
in `./data`, and every stream imported. The first two fail with a message
naming both ways to supply them.

## 2026-09-01, sync and free auto-import

`analyse` now closes a coverage gap itself when every missing blob is already
cached, which costs nothing and is idempotent. `db.coverage.ensure` does the
work: it checks, imports what is free, and re-checks. A gap needing a fetch
still raises with the command, so analysis cannot reach the network however it
is called. `--no-import` restores the strict behaviour.

Auto-fetching was considered and rejected. `analyse` is the command run
repeatedly while iterating on a profile, a widened focus would turn it into 322
points and two minutes of network, and the fetch layer being the only thing
that touches the network is what keeps the offline checks credential-free.

`wcly sync` is the explicit one-liner instead: fetch wide, then import narrow,
with the point budget printed either side.

Also corrected: `fights`, `fetch`, `import` and `sync` printed the focus from
configuration rather than the focus actually in effect, so a run overridden with
`--difficulty Mythic` reported the configured Heroic.

## 2026-09-01, page output folded into analyse

`wcly analyse --module havoc --html` prints the result, writes the metrics and
renders the page from one run, so the three cannot disagree. The separate
`report` command was the same computation with a different renderer, and it did
not write metrics, so a night reported but never analysed left the trend table
empty.

`wcly report` is kept as a placeholder for the cross-night document it was
always specified to be: a summary read from the `metric` table across nights.
That one does depend on analyse having run, which is a real dependency rather
than a nominal one. It prints what it is reserved for and exits non-zero.

## 2026-09-01, --module required

`analyse` and `report` both demand `--module`. Omitting it used to run every
registered module, which was invisible with one module and would have been a
surprise with two.

Open question recorded rather than acted on: `report` runs the module itself
and does not need `analyse` first, so the two commands are the same computation
with different renderers. `analyse` writes metrics and `report` does not, which
means a night reported but never analysed leaves the trend table empty.

## 2026-09-01, comma-separated flags

Every repeatable flag now also accepts comma-separated values, so
`--player A,B` and `--player A --player B` are the same, and the two forms mix.
Conversion happens in the argparse action rather than through `type`, which
would run against the whole comma-separated string and reject it.

`--fight` keeps taking report fight ids. Pull numbers were considered and left
alone: fight ids are what the API and the Warcraft Logs URLs use, and the page
prints both.

## 2026-09-01, report layer

`wcly report` writes the Havoc page from the database. One self-contained HTML
file per subject, carrying its own CSS, script and data, so it opens anywhere
and can be sent to the player it describes.

`Result` gained a `payload` dictionary, which is the structured form of a
module's output. The terminal keeps rendering `lines`; the page renders the
payload. Templates live in `wclyser/report/templates/` and are per module, not
generic: the Havoc page draws a raster of cast marks against a window, and a
general table renderer would not produce it.

Every window links to its own lead-in and window in Warcraft Logs. The URL
takes report-relative milliseconds, the same base the event timestamps use, so
the range is `fight.start_ms + t_rel_ms` with `source` set to the subject's
report-local actor id. Confirmed working against the live site.

Checked on the 2026-08-31 night, which has two warlocks: two pages, 155 and 160
windows, each carrying its own actor id in its links. One pull was left out
because it was never fetched, and the specialisation filter reported it rather
than analysing a gap.

## 2026-09-01, later still

The shard level at a window opening is now extrapolated rather than read.

The reading at the first shard-spending cast inside the window is an upper
bound, because only generation happens between the Havoc cast and it. The
generation in that gap is now subtracted, so the reported figure is the level
at the opening.

Rates live in the profile, in shards per cast: Conflagrate 0.5, Soul Fire 1.0,
Incinerate 0.2, and a passive Immolate or Wither tick of 0.1 every 1.5s.
Everything inside the window counts twice, because Havoc duplicates it.

Worked example, pull 1 window 2. Havoc at 36.45s, first spend a Chaos Bolt
4.19s later reading 3.5 shards, one Conflagrate in between. Generation is
0.5 x 2 for the Conflagrate plus 4.19/1.5 x 0.1 x 2 for the passive ticks,
1.56 in total, so the opening level was 1.94. The previous spend left him at
0.4, and the extrapolation sits above that, as it must.

Two approximations, both under-counting generation, so the figure reads
slightly high. Critical strikes generate extra shards and are ignored. The
passive tick is assumed to be running for the whole gap, which cannot be
checked without a debuff stream the import does not keep.

The check on the model is the lower bound: an extrapolation landing below the
level seen at the previous spend means generation was over-counted. One of 54
windows does, and it is reported.

Over the eleven Mythic pulls the entry level is a mean 4.0 of 5, median 4.6,
with 35 of 54 windows opening at 4.0 or more and 4 below 2.5. Before
subtracting generation the same readings averaged 4.7.

## 2026-09-01, later

Two additions to the Havoc module.

The lead-in: the five seconds before a window opens, from `lead_in_s` in the
profile. A spell from the bad list cast there was cleared out of the way rather
than spending a duplicated global cooldown, which the output flags as
`cleared first`. The lead-in is clamped so it never reaches back into the
previous window.

The shard level on entry. This needed a detour. Havoc costs no shards, so its
own cast event reports the mana pool, and across all three reports every one of
the 1,838 shard `resourcechange` events carries a change of zero. No stream
reports a continuous shard track.

The first attempt bracketed the level between the spend either side of the
opening, which is rigorous but useless: the median bracket ran three shards
wide out of five, and none was exact. What the output reports instead is the
level at the first shard-spending cast inside the window, read a mean 2.9
seconds in, which is a directly observed figure and an upper bound on the level
at entry. The lower bound is kept as `shards_floor_mean` in the metrics.

Over the eleven Mythic pulls: mean 4.7 of 5, median 5.0, and 39 of 54 windows
read at the shard cap.

The lead-in comparison separates habit from cadence. Malevolence was cleared
first before 24 windows and spoiled 5; Summon Infernal 17 against 3. Wither is
the other way round, 13 against 48.

## 2026-09-01

First analysis module: `havoc`, listing what was cast inside each Havoc window
for a Destruction Warlock. With it, the analysis framework: a registry, a
`Result` carrying formatted lines and a metrics dictionary, per-pull
specialisation profiles as YAML, and `wcly analyse`.

The runner calls `db.coverage.require` before a module runs, so a module that
reads a stream nobody imported fails with the command to fix it. The module
reads `casts` alone, which is one of the two streams the import keeps.

Result over the eleven Entombed Sentinels pulls: 54 windows, 877 casts, 16.2
per window. 50 of 54 windows contain a spell from the bad list, but 48 of those
50 are Wither, and windows cover 52% of the analysed time.

Two things checked before trusting the output:

- 22 casts in a 20-second window looked impossible, but the database matches
  the blob exactly and the `begincast` events explain it. A hard cast occupies
  the global cooldown from its start, so a queued instant fires in the same
  millisecond the hard cast completes. Haste-reduced Chaos Bolts of 0.8 to 1.2
  seconds with Conflagrate woven between them give that rate.
- No pet casts are counted. `source_key` resolves a pet to its owner, which is
  right for damage and would have been wrong here, but the subject's own casts
  are the only ones in the stream for this pull.

## 2026-08-31, renamed

`wcl-analyser` became `wclyser`, and the command `wcla` became `wcly`. The
Python package directory moved with it, `git mv wcl_analyser wclyser`.

Renamed with it, since they carry the project's identity rather than Warcraft
Logs':

- `WCL_ANALYSER_CONFIG` to `WCLYSER_CONFIG`.
- The fallback config location, `~/.config/wcl-analyser/` to `~/.config/wclyser/`.
- The database file, `data/wcl.sqlite` to `data/wclyser.sqlite`, rebuilt from
  cache in half a second.

Left alone, because there `WCL` means Warcraft Logs: `WCL_CLIENT_ID`,
`WCL_CLIENT_SECRET`, the `WCLError` hierarchy, and `docs/wcl-api.md`.

61 replacements across 14 files.

The repository directory was renamed afterwards, which needed the session using
it to be closed first: Windows refuses to rename a directory that is a running
process's working directory.

**A folder rename breaks the virtual environment.** `pyvenv.cfg` survives,
because its `home` points at the base Python install rather than at the
project, so `.venv/Scripts/python.exe` still runs. Every `.exe` in
`.venv/Scripts` does not: each embeds the absolute path of the interpreter that
created it, so `pip.exe` fails with "Unable to create process". The editable
install's finder also still maps the package to the old path.

`python -m pip install -e .` would repair the project's own launcher, leaving
the five others stale. The venv was deleted and recreated instead, which is a
minute's work and leaves nothing pointing at the old path.

The `data` directory needed nothing. `data_dir` is the relative path `data`,
resolved against the configuration file's own location.

## 2026-08-31, later still

Import narrowed, and the feedback path for missing data built.

`import.streams` in `config.yaml` decides what reaches the database, now set to
`casts` and `resources`. The same eleven pulls import to 47.5 MB with all
thirteen streams and **1.55 MB** with those two, a factor of thirty. Fetching
stays wide: a blob costs points once and then sits on disk, while a row costs
space in a file meant to grow across a tier.

Added:

- `wcly coverage`, and `db.coverage.require` for modules to call. It compares
  the streams an analysis declares against `import_log` and reports the gap
  with the command that fixes it, distinguishing a stream that is cached and
  merely unimported, where the fix is free, from one never fetched, where the
  fix costs points.
- `wcly prune`, to drop streams the import list no longer wants without a full
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
event mappers and `wcly import`. Run against the focus, Nevaritius playing
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

Judge the Havoc output and adjust `wclyser/profiles/warlock_destruction.yaml`,
then decide the second module.
