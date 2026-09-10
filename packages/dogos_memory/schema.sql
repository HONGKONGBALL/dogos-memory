-- DogOS SQLite v1. Initialize only a new database; never a destructive migration.
-- UTC Unix milliseconds. One owner and one live/simulation mode per file.
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS profile (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    dog_id TEXT NOT NULL UNIQUE CHECK (length(trim(dog_id)) > 0),
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    personality TEXT NOT NULL CHECK (length(trim(personality)) > 0),
    familiar_threshold INTEGER NOT NULL CHECK (
        typeof(familiar_threshold) = 'integer' AND familiar_threshold BETWEEN 1 AND 100
    ),
    mode TEXT NOT NULL CHECK (mode IN ('live', 'simulation'))
);

CREATE TABLE IF NOT EXISTS memories (
    memory_id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(memory_id)) > 0),
    session_id TEXT NOT NULL CHECK (length(trim(session_id)) > 0),
    peer_id TEXT NOT NULL CHECK (length(trim(peer_id)) > 0),
    kind TEXT NOT NULL CHECK (kind IN ('event', 'chat', 'thought')),
    event_type TEXT NOT NULL CHECK (length(trim(event_type)) > 0),
    content TEXT NOT NULL CHECK (length(trim(content)) > 0),
    occurred_at_ms INTEGER NOT NULL CHECK (
        typeof(occurred_at_ms) = 'integer' AND occurred_at_ms >= 0
    ),
    recorded_at_ms INTEGER NOT NULL CHECK (
        typeof(recorded_at_ms) = 'integer' AND recorded_at_ms >= 0
    ),
    source TEXT NOT NULL CHECK (source IN ('robot_feedback', 'operator', 'llm', 'simulation')),
    result TEXT NOT NULL CHECK (result IN ('completed', 'failed', 'unconfirmed', 'derived')),
    importance INTEGER NOT NULL DEFAULT 5 CHECK (
        typeof(importance) = 'integer' AND importance BETWEEN 1 AND 10
    ),
    affinity_delta INTEGER NOT NULL DEFAULT 0 CHECK (
        typeof(affinity_delta) = 'integer' AND affinity_delta BETWEEN 0 AND 100
    ),
    -- Guarantees every thought has at least one evidence link, even via raw SQL.
    evidence_anchor_id TEXT REFERENCES memories(memory_id) ON DELETE RESTRICT,
    CHECK (
        (kind = 'thought' AND result = 'derived' AND source IN ('llm', 'operator')
            AND affinity_delta = 0 AND evidence_anchor_id IS NOT NULL)
        OR
        (kind IN ('event', 'chat') AND result IN ('completed', 'failed', 'unconfirmed')
            AND source IN ('robot_feedback', 'operator', 'simulation')
            AND evidence_anchor_id IS NULL)
    ),
    CHECK (affinity_delta = 0 OR (kind = 'event' AND result = 'completed'))
);

CREATE TABLE IF NOT EXISTS memory_evidence (
    thought_id TEXT NOT NULL REFERENCES memories(memory_id) ON DELETE RESTRICT,
    evidence_id TEXT NOT NULL REFERENCES memories(memory_id) ON DELETE RESTRICT,
    PRIMARY KEY (thought_id, evidence_id),
    CHECK (thought_id <> evidence_id)
);

CREATE TABLE IF NOT EXISTS relationships (
    peer_id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(peer_id)) > 0),
    affinity INTEGER NOT NULL CHECK (typeof(affinity) = 'integer' AND affinity BETWEEN 0 AND 100),
    updated_at_ms INTEGER NOT NULL CHECK (typeof(updated_at_ms) = 'integer' AND updated_at_ms >= 0)
);

CREATE INDEX IF NOT EXISTS memories_peer_recent
    ON memories(peer_id, occurred_at_ms DESC, memory_id DESC);
CREATE INDEX IF NOT EXISTS memories_session ON memories(session_id);
CREATE INDEX IF NOT EXISTS evidence_reverse ON memory_evidence(evidence_id);

CREATE TRIGGER IF NOT EXISTS memories_scope BEFORE INSERT ON memories
BEGIN
    SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM profile)
        THEN RAISE(ABORT, 'profile_required') END;
    SELECT CASE WHEN NEW.peer_id = (SELECT dog_id FROM profile)
        THEN RAISE(ABORT, 'peer_cannot_be_owner') END;
    SELECT CASE WHEN NEW.kind <> 'thought' AND (
        ((SELECT mode FROM profile) = 'live' AND NEW.source = 'simulation') OR
        ((SELECT mode FROM profile) = 'simulation' AND NEW.source <> 'simulation')
    ) THEN RAISE(ABORT, 'memory_mode_mismatch') END;
END;

CREATE TRIGGER IF NOT EXISTS evidence_validate BEFORE INSERT ON memory_evidence
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM memories AS t JOIN memories AS e ON e.memory_id = NEW.evidence_id
        WHERE t.memory_id = NEW.thought_id AND t.kind = 'thought'
          AND e.kind IN ('event', 'chat') AND e.result = 'completed'
          AND t.peer_id = e.peer_id AND e.occurred_at_ms <= t.occurred_at_ms
    ) THEN RAISE(ABORT, 'evidence_must_be_prior_completed_fact_for_same_peer') END;
END;

CREATE TRIGGER IF NOT EXISTS thought_anchor AFTER INSERT ON memories
WHEN NEW.kind = 'thought'
BEGIN
    INSERT INTO memory_evidence (thought_id, evidence_id)
    VALUES (NEW.memory_id, NEW.evidence_anchor_id);
END;

CREATE TRIGGER IF NOT EXISTS relationship_from_event AFTER INSERT ON memories
WHEN NEW.affinity_delta > 0
BEGIN
    INSERT INTO relationships (peer_id, affinity, updated_at_ms)
    VALUES (NEW.peer_id, NEW.affinity_delta, NEW.occurred_at_ms)
    ON CONFLICT (peer_id) DO UPDATE SET
        affinity = min(100, relationships.affinity + excluded.affinity),
        updated_at_ms = max(relationships.updated_at_ms, excluded.updated_at_ms);
END;

CREATE TRIGGER IF NOT EXISTS memories_no_update BEFORE UPDATE ON memories
BEGIN SELECT RAISE(ABORT, 'memories_are_append_only'); END;
CREATE TRIGGER IF NOT EXISTS memories_no_delete BEFORE DELETE ON memories
BEGIN SELECT RAISE(ABORT, 'memories_are_append_only'); END;
-- REPLACE deletes bypass DELETE triggers unless recursive_triggers is on.
CREATE TRIGGER IF NOT EXISTS memories_no_replace BEFORE INSERT ON memories
WHEN EXISTS (SELECT 1 FROM memories WHERE memory_id = NEW.memory_id)
BEGIN SELECT RAISE(ABORT, 'memory_id_already_exists'); END;
CREATE TRIGGER IF NOT EXISTS evidence_no_update BEFORE UPDATE ON memory_evidence
BEGIN SELECT RAISE(ABORT, 'evidence_is_append_only'); END;
CREATE TRIGGER IF NOT EXISTS evidence_no_delete BEFORE DELETE ON memory_evidence
BEGIN SELECT RAISE(ABORT, 'evidence_is_append_only'); END;
CREATE TRIGGER IF NOT EXISTS profile_no_update BEFORE UPDATE ON profile
BEGIN SELECT RAISE(ABORT, 'profile_is_fixed_for_this_experiment'); END;
CREATE TRIGGER IF NOT EXISTS profile_no_replace BEFORE INSERT ON profile
WHEN EXISTS (SELECT 1 FROM profile)
BEGIN SELECT RAISE(ABORT, 'profile_is_fixed_for_this_experiment'); END;
CREATE TRIGGER IF NOT EXISTS profile_no_delete BEFORE DELETE ON profile
BEGIN SELECT RAISE(ABORT, 'profile_is_fixed_for_this_experiment'); END;

PRAGMA user_version = 1;
COMMIT;
