from __future__ import annotations

import hashlib
import unittest

from cluster.lan_sessions import LanSessions
from cluster.protocol import ProtocolError
from cluster.store import Store


class LanSessionTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.now = 1000.0
        self.retired = []
        self.sessions = LanSessions(self.store, self.retire, prepare_node=lambda *_: None, clock=lambda: self.now)
        self.node, self.instance = "a" * 32, "b" * 32

    def tearDown(self):
        self.store.close()

    def retire(self, db, node_id, now, reason):
        self.retired.append((node_id, reason))

    def register(self, instance=None):
        return self.sessions.register({"node_id": self.node, "instance_id": instance or self.instance,
                                       "client_protocol": 1})

    def test_register_heartbeat_and_no_plaintext_token_in_store(self):
        reply = self.register()
        token = reply["session_token"]
        principal = self.sessions.authenticate(token)
        with self.store.read() as db:
            row = db.execute("SELECT * FROM lan_sessions").fetchone()
            self.assertEqual(row["token_sha256"], hashlib.sha256(token.encode()).hexdigest())
            self.assertNotEqual(row["token_sha256"], token)
        self.now += 80
        self.sessions.heartbeat(principal)
        self.now += 80
        self.assertEqual(self.sessions.authenticate(token), principal)
        self.now += 10
        with self.assertRaises(ProtocolError):
            self.sessions.authenticate(token)

    def test_replacement_fences_old_principal_and_close(self):
        first = self.register()
        old = self.sessions.authenticate(first["session_token"])
        second = self.register("c" * 32)
        self.assertEqual(second["session_epoch"], 2)
        self.assertEqual(self.retired, [(self.node, "session_replaced")])
        with self.assertRaises(ProtocolError):
            self.sessions.authenticate(first["session_token"])
        with self.store.transaction() as db:
            with self.assertRaises(ProtocolError):
                self.sessions.check_locked(db, old, self.now)
        current = self.sessions.authenticate(second["session_token"])
        self.sessions.close(current)
        self.assertEqual(self.retired[-1], (self.node, "session_closed"))
        with self.assertRaises(ProtocolError):
            self.sessions.authenticate(second["session_token"])

    def test_expiry_retires_once_and_disabled_node_cannot_reregister(self):
        first = self.register()
        self.now += 90
        self.assertEqual(self.sessions.reap(), 1)
        self.assertEqual(self.sessions.reap(), 0)
        self.assertEqual(self.retired, [(self.node, "session_expired")])
        with self.assertRaises(ProtocolError):
            self.sessions.authenticate(first["session_token"])
        with self.store.transaction() as db:
            db.execute("UPDATE lan_nodes SET enabled=0 WHERE node_id=?", (self.node,))
        with self.assertRaises(ProtocolError):
            self.register()

    def test_failure_in_retirement_rolls_back_session_replacement(self):
        first = self.register()
        def fail(*_):
            raise RuntimeError("synthetic failure")
        self.sessions.retire_leases = fail
        with self.assertRaises(RuntimeError):
            self.register()
        self.assertEqual(self.sessions.authenticate(first["session_token"]).epoch, 1)

    def test_invalid_payload_and_invalid_token_are_rejected(self):
        for body in ({"node_id": self.node, "instance_id": self.instance, "client_protocol": True},
                     {"node_id": self.node, "instance_id": self.instance, "client_protocol": 1, "extra": 1},
                     {"node_id": "bad", "instance_id": self.instance, "client_protocol": 1}):
            with self.subTest(body=body), self.assertRaises(ProtocolError):
                self.sessions.register(body)
        for token in ("bad", "A" * 64, "0" * 64):
            with self.subTest(token=token), self.assertRaises(ProtocolError):
                self.sessions.authenticate(token)


if __name__ == "__main__":
    unittest.main()
