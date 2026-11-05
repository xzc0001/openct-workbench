"""LAN HTTP session identities, independent of the mTLS certificate registry.

This component is not exposed by a server until the coordinator adapter supplies
transactional lease invalidation. Bearer tokens identify a session; HTTP does
not make them confidential or provide strong protection against a LAN observer.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import time
from dataclasses import dataclass
from typing import Callable

from .protocol import ProtocolError, require_id
from .store import Store

_TOKEN = re.compile(r"[0-9a-f]{64}\Z")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS lan_nodes (
    node_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1,
    epoch INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, last_seen REAL
);
CREATE TABLE IF NOT EXISTS lan_sessions (
    node_id TEXT PRIMARY KEY, instance_id TEXT NOT NULL, token_sha256 TEXT NOT NULL UNIQUE,
    epoch INTEGER NOT NULL, expires_at REAL NOT NULL,
    FOREIGN KEY(node_id) REFERENCES lan_nodes(node_id)
);
"""


@dataclass(frozen=True)
class LanPrincipal:
    node_id: str
    instance_id: str
    epoch: int
    token_sha256: str


class LanSessions:
    def __init__(self, store: Store, retire_leases: Callable, *, prepare_node: Callable,
                 ttl_seconds: int = 90,
                 heartbeat_seconds: int = 10, clock: Callable = time.time):
        if not callable(retire_leases) or not callable(prepare_node):
            raise ValueError("transactional node preparation and lease retirement are required")
        if type(ttl_seconds) is not int or type(heartbeat_seconds) is not int or not 1 <= heartbeat_seconds < ttl_seconds <= 3600:
            raise ValueError("invalid session timings")
        self.store, self.retire_leases, self.prepare_node, self.clock = store, retire_leases, prepare_node, clock
        self.ttl_seconds, self.heartbeat_seconds = ttl_seconds, heartbeat_seconds
        # The feature is only initialized when a LAN listener is explicitly enabled.
        with self.store.transaction() as db:
            for statement in _SCHEMA.strip().split(';'):
                if statement.strip():
                    db.execute(statement)

    def register(self, body: dict) -> dict:
        if not isinstance(body, dict) or set(body) != {"node_id", "instance_id", "client_protocol"}:
            raise ProtocolError(400, "invalid_session_request")
        node_id = require_id(body["node_id"], "node_id")
        instance_id = require_id(body["instance_id"], "instance_id")
        if type(body["client_protocol"]) is not int or body["client_protocol"] != 1:
            raise ProtocolError(400, "invalid_client_protocol")
        now = self.clock()
        token = secrets.token_hex(32)
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        with self.store.transaction() as db:
            node = db.execute("SELECT * FROM lan_nodes WHERE node_id=?", (node_id,)).fetchone()
            if node and not node["enabled"]:
                raise ProtocolError(403, "node_disabled")
            self.prepare_node(db, node_id, now)
            if not node:
                db.execute("INSERT INTO lan_nodes(node_id,created_at) VALUES(?,?)", (node_id, now))
                epoch = 1
            else:
                epoch = node["epoch"] + 1
                self.retire_leases(db, node_id, now, "session_replaced")
            db.execute("UPDATE lan_nodes SET epoch=?,last_seen=? WHERE node_id=?", (epoch, now, node_id))
            db.execute("DELETE FROM lan_sessions WHERE node_id=?", (node_id,))
            db.execute("INSERT INTO lan_sessions(node_id,instance_id,token_sha256,epoch,expires_at) VALUES(?,?,?,?,?)",
                       (node_id, instance_id, digest, epoch, now + self.ttl_seconds))
        return {"node_id": node_id, "instance_id": instance_id, "session_token": token,
                "session_epoch": epoch, "session_ttl_seconds": self.ttl_seconds,
                "heartbeat_seconds": self.heartbeat_seconds, "server_time": now}

    def authenticate(self, token: str) -> LanPrincipal:
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise ProtocolError(401, "session_invalid")
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        now = self.clock()
        with self.store.read() as db:
            row = db.execute("SELECT s.*,n.enabled,n.epoch AS current_epoch FROM lan_sessions s "
                             "JOIN lan_nodes n USING(node_id) WHERE s.token_sha256=?", (digest,)).fetchone()
        if not row or not row["enabled"] or row["epoch"] != row["current_epoch"] or row["expires_at"] <= now:
            raise ProtocolError(401, "session_invalid")
        return LanPrincipal(row["node_id"], row["instance_id"], row["epoch"], digest)

    def check_locked(self, db, principal: LanPrincipal, now: float):
        if not isinstance(principal, LanPrincipal):
            raise ProtocolError(401, "session_invalid")
        row = db.execute("SELECT s.*,n.enabled,n.epoch AS current_epoch FROM lan_sessions s "
                         "JOIN lan_nodes n USING(node_id) WHERE s.node_id=? AND s.token_sha256=?",
                         (principal.node_id, principal.token_sha256)).fetchone()
        if (not row or not row["enabled"] or row["epoch"] != principal.epoch
                or row["current_epoch"] != principal.epoch or row["instance_id"] != principal.instance_id
                or row["expires_at"] <= now):
            raise ProtocolError(401, "session_invalid")
        return row

    def heartbeat(self, principal: LanPrincipal):
        now = self.clock()
        with self.store.transaction() as db:
            self.touch_locked(db, principal, now)
        return {"server_time": now, "heartbeat_seconds": self.heartbeat_seconds}

    def touch_locked(self, db, principal: LanPrincipal, now: float):
        self.check_locked(db, principal, now)
        db.execute("UPDATE lan_sessions SET expires_at=? WHERE node_id=?", (now + self.ttl_seconds, principal.node_id))
        db.execute("UPDATE lan_nodes SET last_seen=? WHERE node_id=?", (now, principal.node_id))

    def close(self, principal: LanPrincipal):
        now = self.clock()
        with self.store.transaction() as db:
            self.check_locked(db, principal, now)
            self.retire_leases(db, principal.node_id, now, "session_closed")
            db.execute("DELETE FROM lan_sessions WHERE node_id=? AND epoch=?", (principal.node_id, principal.epoch))

    def reap(self):
        now = self.clock()
        with self.store.transaction() as db:
            expired = db.execute("SELECT node_id,epoch FROM lan_sessions WHERE expires_at<=?", (now,)).fetchall()
            for row in expired:
                self.retire_leases(db, row["node_id"], now, "session_expired")
                db.execute("DELETE FROM lan_sessions WHERE node_id=? AND epoch=?", (row["node_id"], row["epoch"]))
        return len(expired)
