-- Carry the resource cost of a cast.
--
-- A cast event's classResources entry reports the pool the actor held and what
-- the spell cost from it. For a Destruction Warlock a Chaos Bolt reports a soul
-- shard pool out of 50 and a cost of 20, in tenths of a shard. The cost was
-- being dropped, which is the half of the pair that says how much was spent.

ALTER TABLE event ADD COLUMN pool_cost INTEGER;

-- Record the schema a blob was imported under. A migration that adds a column
-- leaves existing rows null in it, so an unchanged hash is no longer enough on
-- its own to skip the work; the schema version has to match as well.
ALTER TABLE import_log ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 0;

DROP VIEW v_event;

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
    e.pool_type,
    e.pool_amount,
    e.pool_max,
    e.pool_cost,
    e.buffs_active,
    e.ts_ms
FROM event e
JOIN fight  f  ON f.report_code = e.report_code AND f.fight_id = e.fight_id
JOIN report r  ON r.code = e.report_code
LEFT JOIN actor sa  ON sa.report_code = e.report_code AND sa.actor_id = e.source_id
LEFT JOIN actor ta  ON ta.report_code = e.report_code AND ta.actor_id = e.target_id
LEFT JOIN ability ab  ON ab.ability_id = e.ability_id
LEFT JOIN ability xab ON xab.ability_id = e.extra_ability_id;
