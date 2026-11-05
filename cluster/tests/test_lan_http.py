from __future__ import annotations

import hashlib
import http.client
import json
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone

from cluster.tests.test_coordinator import CoordinatorFixture
from cluster.lan_server import LAN_PREFIX, LanServer
from cluster.protocol import canonical_json
from cluster.resources import ResourceCatalog


class LanHttpTests(CoordinatorFixture):
    def setUp(self):
        super().setUp()
        root = self.root / "resources"
        root.mkdir()
        self.resource_bytes = b"synthetic offline resource"
        (root / "runner.bin").write_bytes(self.resource_bytes)
        self.resource_sha = hashlib.sha256(self.resource_bytes).hexdigest()
        now = datetime.now(timezone.utc)
        manifest = {"schema": 1, "engine_id": self.engine_id, "engine_version": "fixture",
                    "created_utc": now.isoformat(), "expires_utc": (now + timedelta(days=1)).isoformat(),
                    "platform": {"os": "windows", "arch": "x86_64"}, "runtime": {},
                    "canonical_source_bundle_sha256": "a" * 64,
                    "model_set_sha256": hashlib.sha256(canonical_json([])).hexdigest(),
                    "total_bytes": len(self.resource_bytes), "max_expanded_bytes": len(self.resource_bytes),
                    "files": [{"path": "runner.bin", "bytes": len(self.resource_bytes),
                               "sha256": self.resource_sha, "role": "runner"}], "models": []}
        manifest_path = root / "engine-manifest.json"
        manifest_path.write_bytes(canonical_json(manifest) + b"\n")
        catalog = {"engine": {"root": str(root), "manifest": str(manifest_path)}}
        with self.assertRaises(ValueError):
            ResourceCatalog(catalog)
        resources = ResourceCatalog(catalog, allow_unsigned=True)
        self.server = LanServer(("127.0.0.1", 0), self.coordinator, ["127.0.0.1/32"], resources)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def request(self, method, route, body=None, token=None, extra=None):
        headers = dict(extra or {})
        if body is not None and not isinstance(body, bytes):
            body = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = "Bearer " + token
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, LAN_PREFIX + route, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_loopback_register_resource_claim_and_fencing(self):
        self.enqueue()
        model_source = self.register_node()
        capabilities = self.nodes[model_source]
        node, instance = uuid.uuid4().hex, uuid.uuid4().hex
        status, _, raw = self.request("POST", "/session", {"node_id": node, "instance_id": instance, "client_protocol": 1})
        self.assertEqual(status, 200)
        token = json.loads(raw)["session_token"]
        status, _, _ = self.request("GET", "/node/resource/manifest", token=token)
        self.assertEqual(status, 200)
        status, headers, raw = self.request("GET", "/node/resource/file/" + self.resource_sha,
                                            token=token, extra={"Range": "bytes=0-4"})
        self.assertEqual((status, raw), (206, self.resource_bytes[:5]))
        self.assertIn("Content-Range", headers)
        status, _, _ = self.request("POST", "/node/heartbeat",
            {"instance_id": instance, "capabilities": capabilities, "state": "ready"}, token)
        self.assertEqual(status, 200)
        status, _, raw = self.request("POST", "/node/claim", {"instance_id": instance, "capabilities": capabilities}, token)
        self.assertEqual(status, 200)
        task = json.loads(raw)["task"]
        self.assertEqual(task["node_id"], node)
        input_id = task["inputs"][0]["id"]
        status, _, _ = self.request("GET", "/node/input/" + task["lease_id"] + "/" + input_id, token=token)
        self.assertEqual(status, 200)
        status, _, raw = self.request("POST", "/session", {"node_id": node, "instance_id": uuid.uuid4().hex, "client_protocol": 1})
        self.assertEqual(status, 200)
        next_token = json.loads(raw)["session_token"]
        self.assertNotEqual(token, next_token)
        status, _, _ = self.request("POST", "/node/renew", {"binding": self.binding(task)}, token)
        self.assertEqual(status, 401)
        status, _, _ = self.request("POST", "/session/close", {}, next_token)
        self.assertEqual(status, 200)
        status, _, _ = self.request("POST", "/session/close", {}, next_token)
        self.assertEqual(status, 200)

    def test_http_routes_require_session_and_limit_resource_ranges(self):
        status, _, _ = self.request("GET", "/node/resource/manifest")
        self.assertEqual(status, 401)
        status, _, _ = self.request("GET", "/node/resource/manifest", token="f" * 64)
        self.assertEqual(status, 401)
        status, _, _ = self.request("POST", "/session", {"node_id": "x", "instance_id": "a" * 32, "client_protocol": 1})
        self.assertEqual(status, 400)
        status, _, raw = self.request("POST", "/session", {"node_id": uuid.uuid4().hex, "instance_id": uuid.uuid4().hex, "client_protocol": 1})
        self.assertEqual(status, 200)
        token = json.loads(raw)["session_token"]
        status, _, _ = self.request("GET", "/node/resource/file/" + self.resource_sha,
                                     token=token, extra={"Range": "bytes=999-1000"})
        self.assertEqual(status, 416)


if __name__ == "__main__":
    unittest.main()
