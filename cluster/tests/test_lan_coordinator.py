from __future__ import annotations

import uuid
import unittest

from cluster.tests.test_coordinator import CoordinatorFixture
from cluster.protocol import ProtocolError


class LanCoordinatorTests(CoordinatorFixture):
    def test_lan_claim_reregistration_fences_old_session_and_lease(self):
        self.enqueue()
        source = self.register_node()
        capabilities = self.nodes[source]
        node_id, instance = uuid.uuid4().hex, uuid.uuid4().hex
        sessions = self.coordinator.enable_lan_sessions()
        first = sessions.register({"node_id": node_id, "instance_id": instance, "client_protocol": 1})
        principal = sessions.authenticate(first["session_token"])
        self.coordinator.heartbeat(principal, {"instance_id": instance, "capabilities": capabilities, "state": "ready"})
        task = self.coordinator.claim(principal, {"instance_id": instance, "capabilities": capabilities})["task"]
        self.assertIsNotNone(task)
        old_binding = self.binding(task)
        replacement_instance = uuid.uuid4().hex
        second = sessions.register({"node_id": node_id, "instance_id": replacement_instance, "client_protocol": 1})
        with self.assertRaises(ProtocolError):
            sessions.authenticate(first["session_token"])
        with self.assertRaises(ProtocolError):
            self.coordinator.renew(principal, {"binding": old_binding})
        new_principal = sessions.authenticate(second["session_token"])
        self.coordinator.heartbeat(new_principal, {"instance_id": replacement_instance, "capabilities": capabilities, "state": "ready"})
        new_task = self.coordinator.claim(new_principal, {"instance_id": replacement_instance, "capabilities": capabilities})["task"]
        self.assertEqual(new_task["stage_key"], task["stage_key"])
        self.assertGreater(new_task["fencing_generation"], task["fencing_generation"])
        with self.assertRaises(ProtocolError):
            self.coordinator.renew(new_principal, {"binding": old_binding})

    def test_existing_mtls_node_id_cannot_be_reused_as_lan(self):
        node_id = self.register_node()
        sessions = self.coordinator.enable_lan_sessions()
        with self.assertRaises(ProtocolError):
            sessions.register({"node_id": node_id, "instance_id": uuid.uuid4().hex, "client_protocol": 1})


if __name__ == "__main__":
    unittest.main()
