# wclyser

Personal Warcraft Logs raid performance analysis. It fetches raw event data for
chosen players and accumulates it across raid nights. One night can then be
compared against the last.

Built: the client, fetch, normalise, analysis and page layers. The cross-night
`report` document and the live `watch` loop are not written.

## Setup

Create an API client at https://www.warcraftlogs.com/api/clients/. Copy
`.env.example` to `.env` and fill in the client id and secret. `.env` is
gitignored. An exported `WCL_CLIENT_ID` and `WCL_CLIENT_SECRET` take precedence
over that file.

```bash
pip install -e .
```

Offline checks need no credentials and no network:

```bash
python tests/offline_check.py
```

## How to use it

A raid night takes two commands. The first spends API points and the second
cannot.

```bash
wcly sync    dYGnBvDgfjcTNqKX --config configs/entombed-mythic.yaml
wcly analyse dYGnBvDgfjcTNqKX --config configs/entombed-mythic.yaml --html
```

`sync` fetches every stream for the pulls in focus and imports the ones the
analysis reads. `analyse` prints the result, writes its metrics to the
database, and with `--html` renders one page per subject.

The same night with no config file, everything on the command line:

```bash
wcly sync    dYGnBvDgfjcTNqKX --player Nevaritius \
             --encounter "Entombed Sentinels" --difficulty Mythic
wcly analyse dYGnBvDgfjcTNqKX --player Nevaritius --module havoc \
             --encounter "Entombed Sentinels" --difficulty Mythic --html
```

Both forms are complete. A flag overrides whatever a config sets.

Two commands help settle the focus before any points are spent.
`wcly fights <code>` lists a report's pulls with their fight ids, and
`wcly players <code>` lists its roster with the specialisations each player was
seen in. Each costs one request.

Re-running `analyse` is free. It reads the database, and where a pull's blob is
already cached but not yet imported it imports it first. Only a gap with no
blob stops it, and the message then names the fetch to run.

### Where things go

| Path | Holds |
|---|---|
| `data/raw/` | gzipped API responses, one blob per report, fight, stream and subject |
| `data/wclyser.sqlite` | the database, accumulating across nights |
| `configs/` | saved runs, one file per question |
| `reports/` | generated pages, as `<night>_<report>/<player>_<module>.html` |

All four are gitignored. The database and the pages rebuild from `data/raw` at
no API cost, which is why the blobs are kept.

## Commands

Reaching the API:

```bash
wcly budget                          # points left in the current hour
wcly fights   <code>                 # pulls, with fight ids, kill or wipe, pull numbers
wcly players  <code>                 # roster, specialisations played, resolved subjects
wcly fetch    <code>                 # fetch and cache raw events
wcly sync     <code>                 # fetch, then import, in one step
```

Reading only the cache and the database, so no credentials are needed:

```bash
wcly import   <code>                 # cached blobs into SQLite
wcly analyse  <code> --module havoc  # run a module, print it, store its metrics
wcly analyse  <code> --module havoc --html   # and render a page per subject
wcly coverage <code>                 # what an analysis needs against what is imported
wcly prune    [code]                 # drop imported streams outside the keep set
wcly stats    [code]                 # row counts and the imported pulls
wcly streams                         # the fetch plan, with each stream's scope
wcly report   [code]                 # reserved for the cross-night document
```

`prune` and `stats` cover every report when the code is left off. `report`
prints what it is reserved for and exits non-zero.

### Flags

`--config`, `--database` and `-v` work on either side of the subcommand.
`--database` points at a different SQLite file, for a side-by-side rebuild.

`--player`, `--module`, `--stream`, `--fight` and `--keep` take one value per
flag or several separated by commas, and the two forms mix:

```bash
wcly analyse <code> --player Nevaritius,Bigdaddylock --module havoc
wcly analyse <code> --fight 22,23 --fight 24 --module havoc
```

`--encounter`, `--difficulty` and `--spec` narrow which pulls a command covers.
`--all` ignores a config's focus block. `--encounter` matches a substring of the
boss name or an encounter id, and `--difficulty` takes a name or the raw id.

`--fight` takes report fight ids. Pull numbers are not accepted, because the
API and the Warcraft Logs URLs both use fight ids, and `wcly fights` lists
them. A fight id outside the encounter and difficulty in effect is filtered
out, so `--fight` narrows the focus. It does not replace it.

`--module` is required on `analyse` unless a named config sets
`analysis.modules`. Nothing defaults to running every module, because a module
answers one specific question.

## Configuration

A config file is optional and is read **only when a command names it** with
`--config`. Nothing is discovered, so a file sitting in the working directory
has no effect unless it is asked for. A setting that narrows the pulls or picks
the players changes what a command answers, and that belongs in the command
that ran.

One file is one saved run: the players, the pulls under study, the analysis
modules, and optionally the streams to import. Copy `config.example.yaml` to
start one. Saved runs live in `configs/`, named for the question they ask.

```yaml
subjects:
  default: ["Nevaritius-Frostmourne-US"]

focus:
  encounter: Entombed Sentinels
  difficulty: Mythic
  spec: Destruction

analysis:
  modules: [havoc]
```

A relative `data_dir` or `reports_dir` resolves against the working directory.
Config files sit together in `configs/` while the data store is one shared
place, so resolving against the file would give each config its own empty
store.

## Other documents

- [CLAUDE.md](CLAUDE.md) — conventions and invariants for anyone working here.
- [LOG.md](LOG.md) — what has been built, and what is outstanding.
- [docs/wcl-api.md](docs/wcl-api.md) — measured API behaviour.
- [docs/schema.md](docs/schema.md) — the database schema and its reasoning.
- [docs/adding-a-module.md](docs/adding-a-module.md) — the walkthrough for a
  new analysis module, from getting the data in to rendering its page.

## Architecture

### The fetch unit

`(report_code, fight_id, stream, subject)` is the unit of fetching, of caching
and of import replacement. One paginated call sequence produces one gzipped
blob at
`data/raw/<report>/<fight_id:04d>/events__<stream>__<subject>.json.gz`, and
import replaces exactly the rows that blob produced. Re-running any of the
three is therefore idempotent, with no natural key over event fields.

`subject` is a player key such as `Nevaritius-Frostmourne-US`, or the sentinel
`__raid__` or `__enemy__` for a stream that is not filtered to one player.
Blobs are per subject, so adding a third warlock to the subject list fetches
that warlock alone and leaves the other two cached.

### Cache rules

A completed fight is immutable, so a cache hit never reaches the network. Two
rules keep that safe:

- A report's fights list is never served from cache. It is the only part of a
  report that changes, and the planned watcher depends on seeing new pulls.
- A blob is written only for a fight that has certainly finished: any fight but
  the newest, or the newest once the report has run on past its end by
  `finality_margin_s`. An in-progress pull is fetched and returned, and not
  written.

Each blob records a fingerprint of the stream definition it was fetched under.
Changing a stream's dataType, hostility or filter side changes the fingerprint,
and a blob carrying the old one is refetched instead of being read back with
the wrong meaning.

Those two rules and the fingerprint make the planned `watch` command a loop
over `report_meta` and `fetch_fight`, with no fetching logic of its own.

### Verified API filter semantics

Event filter fields do not mean what their names suggest. Under
`hostilityType: Friendlies`, `sourceID` selects the subject player and their
pets whichever way the event points, and the dataType carries the direction.
Under `hostilityType: Enemies` the id filters return nothing at all, so those
streams are fetched raid-wide.

Full measurements are in [docs/wcl-api.md](docs/wcl-api.md), which is the
reference for any change to a stream definition.

### Specialisation is per fight

`masterData` reports only the class for a player who changed specialisation
during a report. In the first sample night, Nevaritius played Demonology for
pulls 1 to 12 and Destruction for pulls 13 to 23.

`playerDetails` gives every specialisation a player was seen in, which is
enough to resolve subjects before any events are fetched. The specialisation
that applies to one pull comes from the `specID` on that fight's
`CombatantInfo` event, so a specialisation profile is keyed on
`(report, fight, player)`.

### SQLite schema

One wide `event` table rather than one per event type, dimension tables around
it, and `fight_player` carrying the specialisation each player held for each
pull. Import replaces exactly the rows one blob produced, so re-importing a
report changes nothing.

**Fetch wide, import narrow.** A blob costs API points once and then sits on
disk at a few hundred kilobytes, so fetching pulls every stream. Rows in the
event table cost space in a file meant to grow across a whole tier, so the
import list decides what lands. The same eleven pulls import to 47.5 MB with
all thirteen streams and 1.55 MB with `casts` and `resources` alone.

Which streams reach the database is decided in this order: the `--stream` flag,
an `import.streams` list in a named config, then whatever the selected analysis
modules declare they read, and only then everything. So
`wcly import <code> --module havoc` stores casts alone, because casts are all
that module reads.

`analyse` imports a blob that is cached but not yet in the database, because
that costs nothing and is idempotent. It never fetches, so no command that
analyses can spend API points however it is called. A gap with no blob raises
with the command to run, and `wcly sync` is the explicit one-liner for fetch
then import. `--no-import` restores the strict behaviour.

An analysis never answers over missing rows. It declares the streams it reads,
and `db.coverage.ensure` closes what it can and raises otherwise, naming the
command that fixes the gap. The message separates a stream that is merely
unimported, where the fix is free, from one that was never fetched, where the
fix costs points. `wcly coverage` runs the same check by hand.

Full description in [docs/schema.md](docs/schema.md).

### Analysis modules

A module declares the streams it reads and returns a `Result`. The runner
checks coverage before calling it, so a module never sees a pull whose data was
not imported, and never has to guard against missing rows itself.

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

Specialisation knowledge lives in a data file. `wclyser/profiles/<class>_<spec>.yaml`
holds the spell lists and rates, and the profile is chosen per pull, because a
player who respecs between bosses has one specialisation for some pulls and
another for the rest.

Built so far: `havoc`. It lists what was cast inside each Havoc window, what
was cleared out of the way in the seconds before it opened, and the shard level
it opened with. That last figure is extrapolated. The shard pool is reported
only on a cast that spends shards, so the reading at the first spend inside a
window has the generation since the opening subtracted from it, at rates the
profile supplies.

### Report pages

`wcly analyse --html` writes one self-contained page per subject. The page
carries its own CSS, script and data, so it opens on a machine with none of
this installed and can be sent to the player it describes. Google Fonts is the
one external request, and the page falls back to system faces without it.

```bash
wcly analyse <code> --module havoc --html              # into reports/
wcly analyse <code> --module havoc --html somewhere    # into another directory
wcly analyse <code> --module havoc --player X --html p.html   # one named file
```

A page comes from the same run that prints the terminal output and writes the
metrics, so the three cannot disagree.

Every window on the Havoc page links to its own lead-in and window in Warcraft
Logs, filtered to that player's casts. Those URLs take report-relative
milliseconds, which is the base the event timestamps already use, so the range
is `fight.start_ms + t_rel_ms` and the source is the report-local actor id.

Templates are per module, in `wclyser/report/templates/`. The Havoc page draws
a raster of cast marks against a window, which no general table renderer would
produce.

`wcly report` is reserved for a cross-night document that reads the `metric`
table. That one will depend on `analyse` having run for the nights it compares.

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
