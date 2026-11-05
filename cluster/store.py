"""Persistent SQLite store for the reconstruction cluster trust boundary."""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS pairing_tokens (
    token_hash TEXT PRIMARY KEY, created_at REAL NOT NULL, expires_at REAL NOT NULL,
    consumed_at REAL
);
CREATE TABLE IF NOT EXISTS enrollments (
    enrollment_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL, poll_hash TEXT NOT NULL UNIQUE,
    csr_pem TEXT NOT NULL, display_name TEXT NOT NULL, capabilities_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending','approved','rejected','expired')),
    created_at REAL NOT NULL, expires_at REAL NOT NULL, node_id TEXT,
    purpose TEXT NOT NULL DEFAULT 'pairing' CHECK(purpose IN ('pairing','rotation')),
    certificate_pem TEXT, certificate_der BLOB, certificate_fingerprint TEXT
);
CREATE INDEX IF NOT EXISTS enrollments_state_created ON enrollments(state,created_at);
CREATE TABLE IF NOT EXISTS approval_requests (
    enrollment_id TEXT PRIMARY KEY, node_id TEXT NOT NULL,
    approved_by TEXT NOT NULL, requested_at REAL NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending','processed','expired')),
    FOREIGN KEY(enrollment_id) REFERENCES enrollments(enrollment_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS nodes (
    node_id TEXT PRIMARY KEY, certificate_fingerprint TEXT NOT NULL UNIQUE,
    certificate_der BLOB NOT NULL, certificate_pem TEXT NOT NULL,
    display_name TEXT NOT NULL, capabilities_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('ready','paused','draining','unsupported','error','revoked')),
    admin_enabled INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL, approved_at REAL NOT NULL, last_seen REAL,
    certificate_not_after REAL NOT NULL, revoked_at REAL, active_instance TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, state TEXT NOT NULL
    CHECK(state IN ('active','cancelled','tombstoned','completed','failed')),
    mode TEXT NOT NULL, selected_json TEXT NOT NULL, geometry_json TEXT NOT NULL,
    geometry_sha256 TEXT NOT NULL, engine_id TEXT NOT NULL, engine_sha256 TEXT NOT NULL,
    model_ids_json TEXT NOT NULL, model_set_sha256 TEXT NOT NULL,
    input_sha256 TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
    tombstoned_at REAL
);
CREATE TABLE IF NOT EXISTS job_inputs (
    job_id TEXT NOT NULL, revision INTEGER NOT NULL, input_id TEXT NOT NULL,
    role TEXT NOT NULL, type TEXT NOT NULL, source_path TEXT NOT NULL,
    bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, geometry_json TEXT,
    labels_json TEXT, PRIMARY KEY(job_id,revision,input_id),
    UNIQUE(job_id,revision,role),
    FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS stages (
    job_id TEXT NOT NULL, revision INTEGER NOT NULL, stage_key TEXT NOT NULL,
    action TEXT NOT NULL, mode TEXT NOT NULL, structures_json TEXT NOT NULL,
    parameters_json TEXT NOT NULL, resources_json TEXT NOT NULL,
    dependencies_json TEXT NOT NULL, input_ids_json TEXT NOT NULL,
    model_ids_json TEXT NOT NULL, status TEXT NOT NULL
    CHECK(status IN ('pending','ready','leased','running','uploading','validating','completed','retryable','failed','cancelled')),
    attempt INTEGER NOT NULL DEFAULT 0, fencing_generation INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL, error_code TEXT, updated_at REAL NOT NULL,
    PRIMARY KEY(job_id,revision,stage_key),
    FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS stages_state ON stages(job_id,revision,status);
CREATE TABLE IF NOT EXISTS leases (
    lease_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, job_revision INTEGER NOT NULL,
    stage_key TEXT NOT NULL, stage_attempt INTEGER NOT NULL,
    fencing_generation INTEGER NOT NULL, node_id TEXT NOT NULL, instance_id TEXT NOT NULL,
    engine_id TEXT NOT NULL, engine_sha256 TEXT NOT NULL, model_set_sha256 TEXT NOT NULL,
    input_sha256 TEXT NOT NULL, geometry_sha256 TEXT NOT NULL,
    resources_json TEXT NOT NULL,
    issued_at REAL NOT NULL, expires_at REAL NOT NULL, grace_until REAL NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('active','completed','released','expired','cancelled','revoked','stale')),
    last_progress REAL NOT NULL, progress_percent INTEGER NOT NULL DEFAULT 0,
    progress_message TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE,
    FOREIGN KEY(node_id) REFERENCES nodes(node_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_lease_per_node ON leases(node_id) WHERE state='active';
CREATE UNIQUE INDEX IF NOT EXISTS one_active_lease_per_stage ON leases(job_id,job_revision,stage_key) WHERE state='active';
CREATE INDEX IF NOT EXISTS leases_deadline ON leases(state,grace_until);
CREATE TABLE IF NOT EXISTS uploads (
    upload_id TEXT PRIMARY KEY, lease_id TEXT NOT NULL, artifact_id TEXT NOT NULL,
    type TEXT NOT NULL, role TEXT NOT NULL, bytes INTEGER NOT NULL, sha256 TEXT NOT NULL,
    geometry_json TEXT NOT NULL, labels_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending','received','validated','rejected')),
    path TEXT, received_at REAL, error_code TEXT,
    UNIQUE(lease_id,artifact_id), FOREIGN KEY(lease_id) REFERENCES leases(lease_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS stage_artifacts (
    job_id TEXT NOT NULL, revision INTEGER NOT NULL, stage_key TEXT NOT NULL,
    stage_attempt INTEGER NOT NULL, artifact_id TEXT NOT NULL, type TEXT NOT NULL,
    role TEXT NOT NULL, bytes INTEGER NOT NULL, sha256 TEXT NOT NULL,
    path TEXT NOT NULL, geometry_json TEXT NOT NULL, labels_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY(job_id,revision,stage_key,stage_attempt,artifact_id),
    UNIQUE(job_id,revision,stage_key,stage_attempt,role),
    FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS outbox (
    event_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, job_revision INTEGER NOT NULL,
    stage_key TEXT NOT NULL, stage_attempt INTEGER NOT NULL, payload_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending','claimed','acked','discarded')),
    claimed_by TEXT, claim_expires REAL, created_at REAL NOT NULL, acked_at REAL,
    UNIQUE(job_id,job_revision,stage_key,stage_attempt),
    FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(state,created_at);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        if self.path != ":memory:":
            target = Path(self.path).absolute()
            if any(part.casefold() in {"jobs", "cache"} for part in target.parts):
                raise ValueError("cluster database must be outside jobs and cache")
            target.parent.mkdir(parents=True, exist_ok=True)
            if any(part.exists() and (part.is_symlink() or bool(getattr(part, "is_junction", lambda: False)())) for part in (target.parent, *target.parent.parents)):
                raise ValueError("cluster database directory cannot be a symlink")
            if os.name != "nt":
                os.chmod(target.parent, 0o700)
        self._connection = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout=30000")
        self._connection.execute("PRAGMA foreign_keys=ON")
        if self.path != ":memory:":
            self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.executescript(SCHEMA)
        self._migrate()
        self._secure_files()

    def _columns(self, table: str) -> set[str]:
        return {row["name"] for row in self._connection.execute(f"PRAGMA table_info({table})")}

    def _migrate(self):
        # M1 development snapshots may predate the persisted resource budget
        # and admin-enable flag. Expire their leases so they cannot be replayed
        # without an immutable resource budget.
        if "admin_enabled" not in self._columns("nodes"):
            self._connection.execute("ALTER TABLE nodes ADD COLUMN admin_enabled INTEGER NOT NULL DEFAULT 1")
        if "purpose" not in self._columns("enrollments"):
            self._connection.execute("ALTER TABLE enrollments ADD COLUMN purpose TEXT NOT NULL DEFAULT 'pairing'")
        approval_sql = self._connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='approval_requests'").fetchone()
        if approval_sql and "node_id TEXT NOT NULL UNIQUE" in approval_sql[0]:
            self._connection.execute("ALTER TABLE approval_requests RENAME TO approval_requests_v0")
            self._connection.execute("CREATE TABLE approval_requests(enrollment_id TEXT PRIMARY KEY,node_id TEXT NOT NULL,approved_by TEXT NOT NULL,requested_at REAL NOT NULL,state TEXT NOT NULL CHECK(state IN ('pending','processed','expired')),FOREIGN KEY(enrollment_id) REFERENCES enrollments(enrollment_id) ON DELETE CASCADE)")
            self._connection.execute("INSERT INTO approval_requests SELECT enrollment_id,node_id,approved_by,requested_at,state FROM approval_requests_v0")
            self._connection.execute("DROP TABLE approval_requests_v0")
        if "resources_json" not in self._columns("leases"):
            self._connection.execute("UPDATE nodes SET active_instance=NULL WHERE node_id IN (SELECT node_id FROM leases WHERE state='active')")
            self._connection.execute("ALTER TABLE leases ADD COLUMN resources_json TEXT NOT NULL DEFAULT '{}' ")
            self._connection.execute("UPDATE leases SET state='expired' WHERE state='active'")
            self._connection.execute("UPDATE stages SET status='retryable',error_code='schema_migration',updated_at=strftime('%s','now') WHERE status IN ('leased','running','uploading','validating')")
        # An earlier coordinator build used this extra enrollment state before
        # approvals were split into the signer-only approval_requests table.
        self._connection.execute("UPDATE enrollments SET state='pending' WHERE state='provisioning'")
        self._connection.execute("PRAGMA user_version=1")

    def _secure_files(self):
        if self.path != ":memory:" and os.name != "nt":
            for target in (self.path, self.path + "-wal", self.path + "-shm"):
                if os.path.exists(target):
                    os.chmod(target, 0o600)

    @contextmanager
    def transaction(self):
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                self._secure_files()
                raise
            self._secure_files()

    @contextmanager
    def read(self):
        with self._lock:
            yield self._connection

    def close(self):
        with self._lock:
            self._connection.close()
