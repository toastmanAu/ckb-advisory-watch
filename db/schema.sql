-- ckb-advisory-watch SQLite schema (v0 draft).
--
-- Design choices worth revisiting:
--   * advisory.raw_json stores the canonical source payload so we can re-run
--     the matcher if the schema evolves without re-polling every source.
--   * affected_package uses an ecosystem + name tuple because the same name
--     ("serde") can legitimately exist in multiple ecosystems (crates.io vs
--     Go modules). Never match by name alone.
--   * version ranges are stored as raw strings (the semver / cargo / PEP 440
--     expression the advisory gave us). The matcher interprets them at query
--     time via the `packaging` / custom cargo parser — NOT pre-expanded, so
--     we don't explode storage for wide ranges.
--   * match rows are materialised (not a view) so we can tombstone / ack them.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Nervos projects we watch.
CREATE TABLE IF NOT EXISTS project (
    id              INTEGER PRIMARY KEY,
    slug            TEXT UNIQUE NOT NULL,          -- e.g. "nervosnetwork/ckb"
    display_name    TEXT NOT NULL,
    repo_url        TEXT NOT NULL,
    default_branch  TEXT NOT NULL DEFAULT 'main',
    last_sha        TEXT,                          -- tip we've seen
    last_checked    INTEGER,                       -- unix ts
    added_at        INTEGER NOT NULL
);

-- A dependency of a project, resolved at a specific commit sha.
--   (project_id, ecosystem, name, version, source_sha) uniquely identifies
--   the dep as-of a given snapshot. New commits can insert new rows, we
--   don't mutate existing ones — keeps an audit trail of when a version
--   landed / left.
CREATE TABLE IF NOT EXISTS project_dep (
    id              INTEGER PRIMARY KEY,
    project_id      INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    ecosystem       TEXT NOT NULL,                 -- cargo, npm, pypi, go, github
    name            TEXT NOT NULL,
    version         TEXT NOT NULL,                 -- resolved from lockfile
    is_direct       INTEGER NOT NULL DEFAULT 0,    -- 1 if in manifest, 0 if transitive
    source_sha      TEXT NOT NULL,                 -- project commit sha this was resolved at
    first_seen      INTEGER NOT NULL,
    last_seen       INTEGER NOT NULL,              -- updated each refresh while still present
    UNIQUE (project_id, ecosystem, name, version, source_sha)
);

CREATE INDEX IF NOT EXISTS project_dep_name ON project_dep (ecosystem, name);
CREATE INDEX IF NOT EXISTS project_dep_active ON project_dep (project_id, last_seen);

-- Raw advisories from upstream sources, deduped by (source, source_id).
CREATE TABLE IF NOT EXISTS advisory (
    id              INTEGER PRIMARY KEY,
    source          TEXT NOT NULL,                 -- ghsa, osv, rustsec, pypa
    source_id       TEXT NOT NULL,                 -- e.g. GHSA-xxxx-xxxx-xxxx, RUSTSEC-2024-0001
    published       INTEGER,                       -- unix ts
    modified        INTEGER,
    cve_ids         TEXT,                          -- json array of CVE-yyyy-nnnnn strings
    severity        TEXT,                          -- low | medium | high | critical
    cvss            REAL,                          -- numeric score if present
    summary         TEXT,
    details         TEXT,                          -- markdown
    references_json TEXT,                          -- json array of {type, url}
    raw_json        TEXT NOT NULL,                 -- canonical source payload
    ingested_at     INTEGER NOT NULL,
    UNIQUE (source, source_id)
);

CREATE INDEX IF NOT EXISTS advisory_modified ON advisory (modified DESC);

-- Which packages an advisory affects. One advisory → many rows.
CREATE TABLE IF NOT EXISTS advisory_affects (
    id              INTEGER PRIMARY KEY,
    advisory_id     INTEGER NOT NULL REFERENCES advisory(id) ON DELETE CASCADE,
    ecosystem       TEXT NOT NULL,
    name            TEXT NOT NULL,
    version_range   TEXT NOT NULL,                 -- raw expression
    fixed_in        TEXT
);

CREATE INDEX IF NOT EXISTS advisory_affects_lookup
    ON advisory_affects (ecosystem, name);

-- A matched exposure: a dep in a Nervos project that falls in an advisory's
-- affected version range. Materialised so we can track state.
CREATE TABLE IF NOT EXISTS match (
    id              INTEGER PRIMARY KEY,
    advisory_id     INTEGER NOT NULL REFERENCES advisory(id) ON DELETE CASCADE,
    project_id      INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    project_dep_id  INTEGER NOT NULL REFERENCES project_dep(id) ON DELETE CASCADE,
    first_matched   INTEGER NOT NULL,
    state           TEXT NOT NULL DEFAULT 'open',  -- open | acked | fixed | suppressed
    acked_at        INTEGER,
    ack_note        TEXT,
    UNIQUE (advisory_id, project_dep_id)
);

CREATE INDEX IF NOT EXISTS match_open ON match (state, first_matched DESC);

-- False-positive allowlist. Manually curated.
-- Match on (source_id, ecosystem, name) tuple — project-agnostic by default,
-- scope to project by setting project_id, else NULL = global.
CREATE TABLE IF NOT EXISTS suppression (
    id              INTEGER PRIMARY KEY,
    advisory_source_id  TEXT NOT NULL,             -- e.g. GHSA-...
    ecosystem       TEXT,                          -- NULL = any
    name            TEXT,                          -- NULL = any
    project_id      INTEGER REFERENCES project(id) ON DELETE CASCADE,
    reason          TEXT NOT NULL,
    added_at        INTEGER NOT NULL
);

-- Report emission log — lets us not re-send the same telegram alert twice.
CREATE TABLE IF NOT EXISTS emission (
    id              INTEGER PRIMARY KEY,
    match_id        INTEGER NOT NULL REFERENCES match(id) ON DELETE CASCADE,
    channel         TEXT NOT NULL,                 -- vault | website | telegram
    emitted_at      INTEGER NOT NULL,
    artifact_path   TEXT,                          -- path / URL / message id
    UNIQUE (match_id, channel)
);

-- Key/value store for poller state (ETag, last-modified, last bulk zip hash).
CREATE TABLE IF NOT EXISTS poller_state (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_at      INTEGER NOT NULL
);
