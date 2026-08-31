# wclyser

Personal Warcraft Logs raid performance analysis. Fetches raw event data for
chosen players, accumulates it across raid nights, and reports on how the play
changed.

Built so far: the client, fetch and normalise layers. The analysis and report
layers are described below but not written.

## Setup

Create an API client at https://www.warcraftlogs.com/api/clients/. Copy
`.env.example` to `.env` and fill in the client id and secret. `.env` is
gitignored. An exported `WCL_CLIENT_ID` and `WCL_CLIENT_SECRET` take
precedence over the file when both are present.

```bash
pip install -e .
```

Then `cp config.example.yaml config.yaml` and edit. `config.yaml` is
gitignored, and holds the players and the pulls under study.

## Commands

```bash
wcly budget                                    # hourly API point budget
wcly fights <report_code>                      # pulls, with kill/wipe and pull numbers
wcly players <report_code>                     # roster, specs played, resolved subjects
wcly streams                                   # the fetch plan
wcly fetch <report_code> --player <name>       # fetch and cache raw events
wcly import <report_code>                      # cached blobs into SQLite
wcly coverage <report_code> --stream casts     # what an analysis needs vs what is in
wcly prune <report_code>                       # drop streams the import list dropped
wcly stats <report_code>                       # row counts and imported pulls
wcly analyse <report_code> --module havoc      # run analysis modules
wcly report <report_code>                      # write a self-contained HTML page
```

`--database <path>` on any command overrides the SQLite file, for a
side-by-side rebuild.

`analyse`, `report` and `watch` arrive with the layers above.

Offline checks, needing no credentials and no network:

```bash
python tests/offline_check.py
```

## Other documents

- [CLAUDE.md](CLAUDE.md) — conventions and invariants for anyone working here.
- [LOG.md](LOG.md) — what has been built, and what is outstanding.
- [docs/wcl-api.md](docs/wcl-api.md) — measured API behaviour.
- [docs/schema.md](docs/schema.md) — the database schema and its reasoning.

## Architecture

### The fetch unit

`(report_code, fight_id, stream, subject)` is the unit of fetching, of caching
and of import replacement. One paginated call sequence produces one gzipped
blob at
`data/raw/<report>/<fight_id:04d>/events__<stream>__<subject>.json.gz`, and
import replaces exactly the rows that blob produced. Re-running any of the
three is therefore idempotent without needing a natural key over event fields.

`subject` is a player key such as `Nevaritius-Frostmourne-US`, or the sentinel
`__raid__` or `__enemy__` for the streams that are not filtered to one player.
Blobs are per subject, so adding a third warlock to the subject list fetches
only that warlock instead of invalidating the other two.

### Cache rules

A completed fight is immutable, so a cache hit never reaches the network. Two
rules keep that safe:

- The fights list is never served from cache. It is the only part of a report
  that changes, and the watcher depends on seeing new pulls.
- A blob is written only for a fight that has certainly finished: any fight but
  the newest, or the newest once the report has run on past its end by
  `finality_margin_s`. An in-progress pull is still fetched and returned, just
  not written.

Each blob records a fingerprint of the stream definition it was fetched under.
Changing a stream's dataType, hostility or filter side changes the fingerprint,
and blobs carrying the old one are refetched instead of being read back with
the wrong meaning.

Those rules make the planned `watch` command a loop over `report_meta` and
`fetch_fight` with no fetching logic of its own.

### Verified API filter semantics

The event filter fields do not mean what their names suggest. Under
`hostilityType: Friendlies`, `sourceID` selects the subject player and their
pets whichever way the event points, and the dataType carries the direction.
Under `hostilityType: Enemies` the id filters are unusable and streams have to
be fetched raid-wide.

Full measurements are in [docs/wcl-api.md](docs/wcl-api.md). Read it before
changing a stream definition.

### Specialisation is per fight

`masterData` reports only the class for a player who changed specialisation
during the report. In the sample night, Nevaritius played Demonology for pulls
1 to 12 and Destruction for pulls 13 to 23.

`playerDetails` gives every specialisation a player was seen in, which is
enough to resolve subjects before any events are fetched. The specialisation
that applies to a given pull comes from the `specID` on that fight's
`CombatantInfo` event, so the analysis layer must key the specialisation
profile on `(report, fight, player)`.

### SQLite schema

One wide `event` table rather than one per event type, dimension tables around
it, and `fight_player` carrying the specialisation each player held for each
pull. Import replaces exactly the rows one blob produced, so re-importing a
report changes nothing.

**Fetch wide, import narrow.** A blob costs API points once and then sits on
disk at a few hundred kilobytes, so fetching pulls everything. Rows in the
event table cost space in a file meant to grow across a whole tier, so
`import.streams` in `config.yaml` decides what actually lands. The same eleven
pulls import to 47.5 MB with all thirteen streams and 1.55 MB with `casts` and
`resources` alone.

An analysis that needs a stream which was not imported does not answer over
missing rows. It declares what it reads, and `wcly coverage` — or
`db.coverage.require` from inside a module — reports the gap with the command
that fixes it, distinguishing a stream that is merely unimported, where the fix
is free, from one that was never fetched, where the fix costs points.

Full description in [docs/schema.md](docs/schema.md).

### Analysis modules

A module declares the streams it reads and returns a `Result`. The runner
checks coverage before calling it, so a module never sees a pull whose data
was not imported and never has to guard against missing rows itself.

```python
@analysis("havoc", title="Havoc windows", requires={"casts"})
def run(ctx: Ctx) -> dict[str, Result]: ...
```

Presentation stays in the module, because a general table renderer would suit
none of them. What the framework takes is the `metrics` dictionary, written to
one long-format table:

```sql
metric(player_key, report_code, fight_id, analysis_key, metric_key, value)
```

Trend comparison and history read `metric` and know nothing about individual
modules, so a new module needs no schema change and no edit elsewhere.

Specialisation knowledge is data, not code. `wclyser/profiles/<class>_<spec>.yaml`
holds the spell lists, and the profile is chosen per pull, because a player who
respecs between bosses has one specialisation for some pulls and another for
the rest.

### Reports

`wcly report <code>` writes one self-contained HTML page per subject. The page
carries its own CSS, script and data, so it opens on a machine with none of
this installed and can be sent to the player it describes. The only external
request is the Google Fonts stylesheet, and the page falls back to system faces
without it.

Every window links to its own lead-in and window in Warcraft Logs, filtered to
that player's casts. Warcraft Logs URLs take report-relative milliseconds,
which is the base the event timestamps already use, so the range is
`fight.start_ms + t_rel_ms`.

Templates are per module rather than generic, in
`wclyser/report/templates/`. The Havoc page draws a raster of cast marks
against a window, which no general table renderer would produce.

```bash
wcly report <code>                        # one page per subject, into data/reports
wcly report <code> --player X --out p.html   # one named file
```

`--player`, `--module`, `--stream`, `--fight` and `--keep` all take a value
per flag or several separated by commas, and the two forms mix:

```bash
wcly report <code> --player Nevaritius,Bigdaddylock
wcly report <code> --fight 22,23 --fight 24
```

`--fight` takes report fight ids rather than pull numbers, because that is what
the API and the Warcraft Logs URLs use. `wcly fights <code>` lists them. A
fight id outside the encounter and difficulty in effect is filtered out, so
`--fight` narrows the focus rather than replacing it.

Built so far: `havoc`, which lists what was cast inside each Havoc window, what
was cleared out of the way in the seconds before it opened, and the shard level
it opened with. That last figure is extrapolated: the shard pool is only ever
reported on a cast that spends shards, so the reading at the first spend inside
the window has the generation since the opening subtracted from it, at rates
the profile supplies.

## Measured cost

One mythic raid night, 23 pulls, one subject, all 13 streams:

| | |
|---|---|
| Events | 441,239 |
| Blobs | 299 |
| Requests | 308 |
| API points | 322 of 3600 per hour |
| Wall clock | 2m 6s |
| Disk, gzipped | 7.0 MB |
| Re-run, fully cached | 3.3s, 2 requests |

At roughly 7 MB per night, a full tier of raid nights is a few hundred
megabytes of raw JSON.
