-- Initial schema.
--
-- Times are milliseconds. report.start_time_utc is unix milliseconds; every
-- other timestamp is relative to it, which is how the API reports them.
-- t_rel_ms on an event is relative to the start of its own pull.

CREATE TABLE report (
    code            TEXT    PRIMARY KEY,
    title           TEXT,
    owner           TEXT,
    guild           TEXT,
    region          TEXT,
    zone_id         INTEGER,
    zone_name       TEXT,
    start_time_utc  INTEGER NOT NULL,
    end_time_utc    INTEGER,
    raid_night      TEXT    NOT NULL,
    imported_at     TEXT    NOT NULL
);

CREATE TABLE fight (
    report_code     TEXT    NOT NULL REFERENCES report(code) ON DELETE CASCADE,
    fight_id        INTEGER NOT NULL,
    encounter_id    INTEGER NOT NULL,
    boss_name       TEXT    NOT NULL,
    difficulty      INTEGER,
    difficulty_name TEXT,
    raid_size       INTEGER,
    kill            INTEGER NOT NULL DEFAULT 0,
    end_percent     REAL,
    boss_percent    REAL,
    last_phase      INTEGER,
    start_ms        INTEGER NOT NULL,
    end_ms          INTEGER NOT NULL,
    duration_ms     INTEGER NOT NULL,
    -- Pull number within this report only. The sequence across raid nights is
    -- v_pull.pull_seq, computed on read, because storing it would go stale as
    -- soon as an older night is imported late.
    pull_number     INTEGER,
    PRIMARY KEY (report_code, fight_id)
);

CREATE INDEX fight_encounter ON fight(encounter_id, difficulty);

CREATE TABLE actor (
    report_code TEXT    NOT NULL REFERENCES report(code) ON DELETE CASCADE,
    actor_id    INTEGER NOT NULL,
    guid        INTEGER,
    name        TEXT    NOT NULL,
    server      TEXT,
    region      TEXT,
    type        TEXT,
    sub_type    TEXT,
    -- 'Name-Server-Region' for players, null for everything else.
    player_key  TEXT,
    -- actor_id of the owning player, for pets.
    pet_owner   INTEGER,
    PRIMARY KEY (report_code, actor_id)
);

CREATE INDEX actor_player ON actor(player_key);

-- One row per character, across every report.
CREATE TABLE player (
    player_key TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    server     TEXT,
    region     TEXT,
    class      TEXT,
    guid       INTEGER,
    first_seen TEXT,
    last_seen  TEXT
);

CREATE TABLE ability (
    ability_id INTEGER PRIMARY KEY,
    name       TEXT,
    icon       TEXT,
    school     INTEGER
);

-- Specialisation is a property of the pull. A player who respecs between
-- bosses has one specialisation for some fights and another for the rest.
CREATE TABLE fight_player (
    report_code TEXT    NOT NULL,
    fight_id    INTEGER NOT NULL,
    actor_id    INTEGER NOT NULL,
    player_key  TEXT    NOT NULL,
    class       TEXT,
    spec        TEXT,
    spec_id     INTEGER,
    role        TEXT,
    item_level  INTEGER,
    talent_hash TEXT,
    talent_json TEXT,
    -- 1 when this pull's events were fetched for this player, so their event
    -- coverage is complete rather than incidental.
    is_subject  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (report_code, fight_id, actor_id),
    FOREIGN KEY (report_code, fight_id)
        REFERENCES fight(report_code, fight_id) ON DELETE CASCADE
);

CREATE INDEX fight_player_key ON fight_player(player_key, spec);

-- One wide timeline table rather than one table per event type, because
-- analysis crosses types constantly: what was cast before a death, whether a
-- defensive was up when a boss ability landed.
CREATE TABLE event (
    event_pk        INTEGER PRIMARY KEY,
    report_code     TEXT    NOT NULL,
    fight_id        INTEGER NOT NULL,
    -- Which blob produced this row. Also the replacement key on re-import.
    stream          TEXT    NOT NULL,
    subject         TEXT    NOT NULL,
    ts_ms           INTEGER NOT NULL,
    t_rel_ms        INTEGER NOT NULL,
    event_type      TEXT    NOT NULL,
    source_id       INTEGER,
    target_id       INTEGER,
    -- Resolved identity. A pet resolves to its owner's player key so that
    -- pet damage aggregates with the player's own; source_id keeps the pet.
    source_key      TEXT,
    target_key      TEXT,
    source_is_pet   INTEGER NOT NULL DEFAULT 0,
    ability_id      INTEGER,
    -- The interrupted or dispelled spell.
    extra_ability_id INTEGER,
    amount          INTEGER,
    unmitigated     INTEGER,
    absorbed        INTEGER,
    mitigated       INTEGER,
    overkill        INTEGER,
    overheal        INTEGER,
    hit_type        INTEGER,
    tick            INTEGER,
    stack           INTEGER,
    -- The resource this event changed, and by how much.
    resource_type   INTEGER,
    resource_change INTEGER,
    resource_max    INTEGER,
    resource_waste  INTEGER,
    -- The actor's own resource pool at that moment, which is frequently a
    -- different resource from the one the event changed. A warlock spending a
    -- soul shard reports the change against shards and the pool against mana,
    -- so these must not share columns with the four above.
    pool_type       INTEGER,
    pool_amount     INTEGER,
    pool_max        INTEGER,
    hp_current      INTEGER,
    hp_max          INTEGER,
    pos_x           INTEGER,
    pos_y           INTEGER,
    facing          INTEGER,
    -- Dot-separated ability ids active on the actor at this moment, as WCL
    -- ships them. Answers "was this cast made under that buff" without a join.
    buffs_active    TEXT,
    FOREIGN KEY (report_code, fight_id)
        REFERENCES fight(report_code, fight_id) ON DELETE CASCADE
);

CREATE INDEX event_replace     ON event(report_code, fight_id, stream, subject);
CREATE INDEX event_fight_time  ON event(report_code, fight_id, t_rel_ms);
CREATE INDEX event_type_time   ON event(report_code, fight_id, event_type, t_rel_ms);
CREATE INDEX event_source_abil ON event(source_key, ability_id, event_type);

-- One row per imported blob. A matching hash means the blob has not changed
-- and import can skip it without touching the event table.
CREATE TABLE import_log (
    report_code TEXT    NOT NULL,
    fight_id    INTEGER NOT NULL,
    stream      TEXT    NOT NULL,
    subject     TEXT    NOT NULL,
    blob_sha256 TEXT    NOT NULL,
    row_count   INTEGER NOT NULL,
    imported_at TEXT    NOT NULL,
    PRIMARY KEY (report_code, fight_id, stream, subject)
);

-- Pre-aggregated table() and graph() responses, kept whole.
CREATE TABLE table_snapshot (
    report_code TEXT    NOT NULL,
    fight_id    INTEGER NOT NULL,
    kind        TEXT    NOT NULL,
    data_type   TEXT    NOT NULL,
    hostility   TEXT    NOT NULL,
    subject     TEXT    NOT NULL,
    json        TEXT    NOT NULL,
    fetched_at  TEXT    NOT NULL,
    PRIMARY KEY (report_code, fight_id, kind, data_type, hostility, subject)
);

-- Long-format results from the analysis layer. Every module writes here, so
-- trend and history queries need no knowledge of individual modules and a new
-- module needs no schema change. fight_id is -1 for a report-level metric,
-- because SQLite treats nulls in a primary key as distinct.
CREATE TABLE metric (
    player_key   TEXT    NOT NULL,
    report_code  TEXT    NOT NULL,
    fight_id     INTEGER NOT NULL DEFAULT -1,
    analysis_key TEXT    NOT NULL,
    metric_key   TEXT    NOT NULL,
    value        REAL,
    text_value   TEXT,
    computed_at  TEXT    NOT NULL,
    PRIMARY KEY (player_key, report_code, fight_id, analysis_key, metric_key)
);

-- Readable timeline, for browsing by hand.
CREATE VIEW v_event AS
SELECT
    r.raid_night,
    e.report_code,
    e.fight_id,
    f.boss_name,
    f.difficulty_name,
    f.pull_number,
    CASE f.kill WHEN 1 THEN 'kill' ELSE 'wipe' END AS outcome,
    e.t_rel_ms / 1000.0 AS t_s,
    e.event_type,
    e.stream,
    COALESCE(sa.name, e.source_key) AS source_name,
    e.source_key,
    e.source_is_pet,
    COALESCE(ta.name, e.target_key) AS target_name,
    ab.name AS ability,
    e.ability_id,
    xab.name AS extra_ability,
    e.amount,
    e.overkill,
    e.overheal,
    e.absorbed,
    e.stack,
    e.hp_current,
    e.hp_max,
    e.resource_type,
    e.resource_change,
    e.pool_amount,
    e.buffs_active,
    e.ts_ms
FROM event e
JOIN fight  f  ON f.report_code = e.report_code AND f.fight_id = e.fight_id
JOIN report r  ON r.code = e.report_code
LEFT JOIN actor sa  ON sa.report_code = e.report_code AND sa.actor_id = e.source_id
LEFT JOIN actor ta  ON ta.report_code = e.report_code AND ta.actor_id = e.target_id
LEFT JOIN ability ab  ON ab.ability_id = e.ability_id
LEFT JOIN ability xab ON xab.ability_id = e.extra_ability_id;

-- Pulls in order across every imported night, so "the eleventh Mythic pull of
-- this boss" is a query rather than a stored number.
CREATE VIEW v_pull AS
SELECT
    f.report_code,
    f.fight_id,
    r.raid_night,
    f.encounter_id,
    f.boss_name,
    f.difficulty,
    f.difficulty_name,
    f.kill,
    f.end_percent,
    f.duration_ms,
    f.pull_number AS pull_in_report,
    ROW_NUMBER() OVER (
        PARTITION BY f.encounter_id, f.difficulty
        ORDER BY r.raid_night, f.start_ms
    ) AS pull_seq
FROM fight f
JOIN report r ON r.code = f.report_code;

-- What each pull was fetched for, with its specialisation.
CREATE VIEW v_subject_pull AS
SELECT
    r.raid_night,
    fp.report_code,
    fp.fight_id,
    f.boss_name,
    f.difficulty_name,
    f.pull_number,
    CASE f.kill WHEN 1 THEN 'kill' ELSE 'wipe' END AS outcome,
    f.duration_ms / 1000.0 AS duration_s,
    fp.player_key,
    fp.class,
    fp.spec,
    fp.role,
    fp.item_level
FROM fight_player fp
JOIN fight  f ON f.report_code = fp.report_code AND f.fight_id = fp.fight_id
JOIN report r ON r.code = fp.report_code
WHERE fp.is_subject = 1;
