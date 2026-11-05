from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import signal
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID

from .gateway_fixture import (
    LoopbackGateway,
    _certificate_pem,
    _issue_leaf,
    _new_ca,
    _private_key_pem,
    _write,
    json_http,
)
from .test_coordinator import CoordinatorFixture, _make_csr, _sha
from cluster.api import Coordinator
from cluster.artifacts import validate_output_file
from cluster.protocol import API_PREFIX, canonical_json
from cluster.server import _GatewayServer, _Handler
from cluster.tls import create_enrollment_server_context


class GatewayHttpIntegrationTests(CoordinatorFixture):
    """The frozen v1 JSON and binary routes over actual loopback HTTPS/mTLS."""

    def setUp(self):
        super().setUp()
        self.coordinator.close()
        self.coordinators = []
        self.resource_signer = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        self.resource_catalog = self._write_signed_resource()
        self.coordinator = Coordinator(
            self.root / "cluster.sqlite3", self.root / "cluster-artifacts",
            lease_seconds=30, expiry_grace_seconds=2, heartbeat_seconds=2, max_attempts=3,
            resource_catalog=self.resource_catalog)
        self.coordinators = [self.coordinator]
        self.gateway = LoopbackGateway(self.root, self.coordinator)
        self.capabilities = {
            "engine": {"id": self.engine_id, "sha256": self.engine_sha},
            "models": [{key: row[key] for key in ("id", "name", "sha256", "files")} for row in self.models],
            "resources": {"threads": 8, "memory_bytes": 1024**3, "cpu_percent": 80,
                          "free_disk_bytes": 20 * 1024**3},
            "platform": {"os": "windows", "arch": "x86_64", "version": "synthetic-loopback"},
            "state": "ready",
        }
        self.instance_id = uuid.uuid4().hex
        self._identities: list[tuple[Path, Path, bytes, str]] = []

    def _write_signed_resource(self):
        root = self.root / "signed-resource"
        root.mkdir()
        payload = b"synthetic-resource-range-hash-fixture"
        resource_path = root / "runner.bin"
        resource_path.write_bytes(payload)
        now = datetime.now(timezone.utc)
        model_set = hashlib.sha256(canonical_json([])).hexdigest()
        manifest = {
            "schema": 1,
            "engine_id": self.engine_id,
            "engine_version": "test-v1",
            "created_utc": now.isoformat().replace("+00:00", "Z"),
            "expires_utc": (now + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
            "platform": {"os": "windows", "arch": "x86_64"},
            "runtime": {},
            "canonical_source_bundle_sha256": _sha(b"synthetic source bundle"),
            "model_set_sha256": model_set,
            "total_bytes": len(payload),
            "max_expanded_bytes": len(payload),
            "files": [{"path": "runner.bin", "bytes": len(payload),
                       "sha256": _sha(payload), "role": "runner"}],
            "models": [],
        }
        manifest_raw = canonical_json(manifest) + b"\n"
        signature = self.resource_signer.sign(manifest_raw, __import__(
            "cryptography.hazmat.primitives.asymmetric.padding", fromlist=["PKCS1v15"]).PKCS1v15(), hashes.SHA256())
        manifest_path = self.root / "signed-resource-manifest.json"
        signature_path = self.root / "signed-resource-manifest.sig"
        manifest_path.write_bytes(manifest_raw)
        signature_path.write_bytes(signature)
        public_key = self.resource_signer.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        entry = {"manifest": str(manifest_path), "signature": str(signature_path),
                 "root": str(root), "public_key_pem": public_key}
        return {"engine": entry, "model": entry}

    def _client_context(self, identity=None, *, ca_path=None):
        certificate_path, key_path = identity[:2] if identity else (None, None)
        return self.gateway.client_context(certificate_path=certificate_path,
                                           key_path=key_path, ca_path=ca_path)

    def _http_json(self, port, method, path, body, *, identity=None, headers=None,
                   hostname="localhost", expected_pin=None):
        return json_http(port, method, path, body, self._client_context(identity),
                         hostname=hostname, headers=headers,
                         expected_leaf_sha256=expected_pin)

    def _enroll_node(self, *, approve=True, name="Loopback synthetic worker"):
        key, csr = _make_csr()
        response = self._http_json(
            self.gateway.enrollment_port, "POST", API_PREFIX + "/enroll",
            {"pairing_token": self.coordinator.create_pairing_token()["pairing_token"],
             "csr_pem": csr.decode("ascii"), "display_name": name,
             "capabilities": self.capabilities})
        status, _headers, raw = response
        self.assertEqual(status, 200, raw)
        enrollment = json.loads(raw)
        self.assertEqual(set(enrollment), {"enrollment_id", "poll_token", "expires_in"})
        body = {"enrollment_id": enrollment["enrollment_id"], "poll_token": enrollment["poll_token"]}
        status, _headers, raw = self._http_json(
            self.gateway.enrollment_port, "POST", API_PREFIX + "/enroll/status", body)
        self.assertEqual(status, 200, raw)
        pending = json.loads(raw)
        self.assertEqual(set(pending), {"state", "node_id", "expires_in"})
        self.assertEqual(pending["state"], "pending_approval")
        self.assertIsNone(pending["node_id"])
        if not approve:
            return {"enrollment": enrollment, "key": key, "pending": pending}
        queued = self.coordinator.approve_enrollment(enrollment["enrollment_id"])
        self.assertEqual(queued["state"], "pending_signing")
        status, _headers, raw = self._http_json(
            self.gateway.enrollment_port, "POST", API_PREFIX + "/enroll/status", body)
        self.assertEqual(status, 200, raw)
        provisioning = json.loads(raw)
        self.assertEqual(set(provisioning), {"state", "node_id", "expires_in"})
        self.assertEqual(provisioning["state"], "provisioning")
        self.assertEqual(provisioning["node_id"], queued["node_id"])
        self.assertEqual(self.gateway.signer.process_approvals(), 1)
        status, _headers, raw = self._http_json(
            self.gateway.enrollment_port, "POST", API_PREFIX + "/enroll/status", body,
            expected_pin=self.gateway.server_cert_sha256)
        self.assertEqual(status, 200, raw)
        approved = json.loads(raw)
        self.assertEqual(set(approved), {"state", "node_id", "certificate_pem", "expires_in"})
        self.assertEqual(approved["state"], "approved")
        self.assertEqual(approved["node_id"], provisioning["node_id"])
        identity = self.gateway.client_identity_from_enrollment(key, approved["certificate_pem"])
        self._identities.append((*identity, approved["node_id"]))
        return {"enrollment": enrollment, "key": key, "pending": pending,
                "approved": approved, "identity": (*identity, approved["node_id"])}

    def test_real_http_enrollment_heartbeat_claim_input_range_result_and_resource_hash(self):
        job_id, _revision, _values = self.enqueue()
        enrolled = self._enroll_node()
        identity = enrolled["identity"]
        node_id = enrolled["approved"]["node_id"]

        status, _headers, raw = self._http_json(
            self.gateway.device_port, "POST", API_PREFIX + "/node/heartbeat",
            {"instance_id": self.instance_id, "capabilities": self.capabilities,
             "state": "ready"}, identity=identity, expected_pin=self.gateway.server_cert_sha256,
            headers={"Cookie": "workbench_session=not-an-identity"})
        self.assertEqual(status, 200, raw)
        heartbeat = json.loads(raw)
        self.assertEqual(set(heartbeat), {"server_time", "heartbeat_seconds"})
        self.assertIsInstance(heartbeat["server_time"], (int, float))

        headers = {"X-Node-ID": uuid.uuid4().hex, "Cookie": "workbench_session=forged"}
        status, _headers, raw = self._http_json(
            self.gateway.device_port, "POST", API_PREFIX + "/node/claim",
            {"instance_id": self.instance_id, "capabilities": self.capabilities},
            identity=identity, headers=headers)
        self.assertEqual(status, 200, raw)
        claim = json.loads(raw)
        self.assertEqual(set(claim), {"task", "reason", "server_time"})
        task = claim["task"]
        self.assertIsNotNone(task)
        self.assertIsNone(claim["reason"])
        self.assertEqual(task["node_id"], node_id)
        self.assertNotEqual(task["node_id"], headers["X-Node-ID"])
        self.assertEqual(task["input_sha256"], self._task_input_digest(task["inputs"]))
        ct = next(item for item in task["inputs"] if item["role"] == "ct")

        first_end = min(9, ct["bytes"] - 1)
        status, part_headers, part = self._http_json(
            self.gateway.device_port, "GET",
            API_PREFIX + f"/node/input/{task['lease_id']}/{ct['id']}", None,
            identity=identity, headers={"Range": f"bytes=0-{first_end}"})
        self.assertEqual(status, 206)
        self.assertEqual(part_headers["content-range"], f"bytes 0-{first_end}/{ct['bytes']}")
        chunks = [part]
        next_start = first_end + 1
        while next_start < ct["bytes"]:
            status, more_headers, block = self._http_json(
                self.gateway.device_port, "GET",
                API_PREFIX + f"/node/input/{task['lease_id']}/{ct['id']}", None,
                identity=identity, headers={"Range": f"bytes={next_start}-"})
            self.assertEqual(status, 206)
            self.assertTrue(more_headers["content-range"].startswith(f"bytes {next_start}-"))
            chunks.append(block)
            next_start += len(block)
        assembled = b"".join(chunks)
        self.assertEqual(len(assembled), ct["bytes"])
        self.assertEqual(_sha(assembled), ct["sha256"])

        resource_digest = _sha(b"synthetic-resource-range-hash-fixture")
        status, manifest_headers, raw = self._http_json(
            self.gateway.device_port, "GET", API_PREFIX + "/node/resource/engine/manifest", None,
            identity=identity)
        self.assertEqual(status, 200, raw)
        self.assertEqual(_sha(raw), manifest_headers["etag"].strip('"'))
        status, _headers, sig = self._http_json(
            self.gateway.device_port, "GET", API_PREFIX + "/node/resource/engine/manifest.sig", None,
            identity=identity)
        self.assertEqual(status, 200)
        self.assertEqual(len(sig), 384)
        ranged = []
        for start, end in ((0, 5), (6, 13), (14, len(b"synthetic-resource-range-hash-fixture") - 1)):
            status, headers, block = self._http_json(
                self.gateway.device_port, "GET",
                API_PREFIX + f"/node/resource/engine/file/{resource_digest}", None,
                identity=identity, headers={"Range": f"bytes={start}-{end}"})
            self.assertEqual(status, 206)
            self.assertEqual(headers["content-range"], f"bytes {start}-{end}/{len(b'synthetic-resource-range-hash-fixture')}")
            ranged.append(block)
        resource = b"".join(ranged)
        self.assertEqual(_sha(resource), resource_digest)
        self.assertEqual(resource, b"synthetic-resource-range-hash-fixture")

        from cluster.coordinator import _canonical_modules
        structures, _progressive = _canonical_modules()
        plan = {row[1]: row for row in structures.inference_plan(task["structures"], task["mode"])}
        planned = plan[task["stage_key"]]
        labels = planned[2].get("roi_subset") or {
            "lung_vessels": ["lung_arteries", "lung_veins", "lung_airways"],
            "lung_nodules": ["lung_nodules"],
        }.get(task["stage_key"], [])
        receipt = json.dumps({"device": "cpu", "offline_guard": True,
                              "tasks": [{"task": planned[0], "key": task["stage_key"],
                                         "seconds": 0.125, "resolution_mm": 3.0}]},
                             separators=(",", ":")).encode()
        mask = self.synthetic_nifti()
        pairs = [(self.descriptor("inference_json", f"inference.{task['stage_key']}", receipt), receipt)]
        pairs.extend((self.descriptor("nifti_mask", f"prediction.{task['stage_key']}.{label}",
                                      mask, labels=[1]), mask) for label in labels)
        metrics = {"wall_seconds": 0.125, "cpu_seconds": None,
                   "peak_rss_bytes": None, "transfer_seconds": None}
        status, _headers, raw = self._http_json(
            self.gateway.device_port, "POST", API_PREFIX + "/node/result/begin",
            {"binding": self.binding(task), "artifacts": [desc for desc, _blob in pairs],
             "metrics": metrics}, identity=identity)
        self.assertEqual(status, 200, raw)
        begin = json.loads(raw)
        self.assertEqual(set(begin), {"uploads"})
        content_by_id = {desc["id"]: content for desc, content in pairs}
        self.assertEqual(len(begin["uploads"]), len(content_by_id))
        for upload in begin["uploads"]:
            self.assertEqual(set(upload), {"upload_id", "artifact_id", "type", "bytes", "sha256"})
            content = content_by_id[upload["artifact_id"]]
            status, _headers, raw = self._http_json(
                self.gateway.device_port, "PUT",
                API_PREFIX + f"/node/result/{task['lease_id']}/{upload['upload_id']}",
                content, identity=identity)
            self.assertEqual(status, 200, raw)
            self.assertEqual(json.loads(raw), {"received": True, "duplicate": False})
        status, _headers, raw = self._http_json(
            self.gateway.device_port, "POST", API_PREFIX + "/node/result/finalize",
            {"binding": self.binding(task)}, identity=identity)
        self.assertEqual(status, 200, raw)
        self.assertEqual(json.loads(raw), {"state": "completed", "duplicate": False})
        event = self.coordinator.claim_publish_event("loopback-test-publisher")
        self.assertEqual(event["job_id"], job_id)
        self.assertEqual(event["job_revision"], task["job_revision"])
        self.assertEqual(event["stage_key"], task["stage_key"])
        self.assertFalse(event["job_complete"])
        self.assertTrue(self.coordinator.ack_publish_event(event["event_id"], "loopback-test-publisher"))

    @staticmethod
    def _task_input_digest(inputs):
        rows = sorted(({key: row[key] for key in ("id", "type", "bytes", "sha256", "role")} for row in inputs),
                      key=lambda row: (row["role"], row["id"]))
        return hashlib.sha256(canonical_json(rows)).hexdigest()

    def tearDown(self):
        if hasattr(self, "gateway"):
            self.gateway.close()
        super().tearDown()

    def test_real_tls_rejects_wrong_ca_hostname_expiry_unregistered_and_cookie_identity(self):
        wrong_ca_key, wrong_ca = _new_ca("wrong synthetic client trust root")
        wrong_ca_path = _write(self.root / "wrong-ca.pem", _certificate_pem(wrong_ca))
        wrong_context = self.gateway.client_context(ca_path=wrong_ca_path)
        with self.assertRaises(ssl.SSLCertVerificationError):
            json_http(self.gateway.enrollment_port, "POST", API_PREFIX + "/enroll/status", {}, wrong_context)

        trusted = self.gateway.client_context()
        with self.assertRaises(ssl.SSLCertVerificationError):
            json_http(self.gateway.enrollment_port, "POST", API_PREFIX + "/enroll/status", {}, trusted,
                      hostname="wrong-host.invalid")

        # Device listener requires a verified client certificate at TLS handshake;
        # a browser session cookie cannot substitute for that certificate.
        with self.assertRaises((ssl.SSLError, OSError, TimeoutError)):
            json_http(self.gateway.device_port, "POST", API_PREFIX + "/node/heartbeat",
                      {"instance_id": self.instance_id, "capabilities": self.capabilities,
                       "state": "ready"}, trusted, headers={"Cookie": "workbench_session=valid"})

        rogue_cert, rogue_key, _der = self.gateway.issue_unregistered_client()
        rogue_identity = (rogue_cert, rogue_key)
        status, _headers, raw = self._http_json(
            self.gateway.device_port, "POST", API_PREFIX + "/node/heartbeat",
            {"instance_id": self.instance_id, "capabilities": self.capabilities,
             "state": "ready"}, identity=rogue_identity)
        self.assertEqual(status, 401, raw)
        self.assertEqual(json.loads(raw), {"error": "node_certificate_invalid"})

        # An expired but otherwise trusted hostname certificate is rejected by
        # the client before HTTP. It uses a separate real gateway listener.
        expired_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        now = datetime.now(timezone.utc)
        expired_cert = _issue_leaf(
            self.gateway.server_ca_key, self.gateway.server_ca_cert, expired_key,
            "localhost", dns_names=("localhost",), eku=(ExtendedKeyUsageOID.SERVER_AUTH,),
            not_before=now - timedelta(days=2), not_after=now - timedelta(days=1))
        expired_chain = _write(self.root / "expired-server.pem",
                               _certificate_pem(expired_cert) + _certificate_pem(self.gateway.server_ca_cert))
        expired_key_path = _write(self.root / "expired-server.key", _private_key_pem(expired_key))
        expired_server = _GatewayServer(
            ("127.0.0.1", 0), _Handler, self.coordinator, "enrollment",
            create_enrollment_server_context(str(expired_chain), str(expired_key_path)))
        expired_thread = threading.Thread(target=expired_server.serve_forever,
                                          kwargs={"poll_interval": 0.05}, daemon=True)
        expired_thread.start()
        try:
            with self.assertRaises(ssl.SSLCertVerificationError):
                json_http(expired_server.server_address[1], "POST", API_PREFIX + "/enroll/status",
                          {}, self.gateway.client_context())
        finally:
            expired_server.shutdown()
            expired_server.server_close()
            expired_thread.join(timeout=5)
        self.assertFalse(expired_thread.is_alive())

    def test_revoked_certificate_and_forged_header_cannot_change_tls_identity(self):
        self.enqueue()
        enrolled = self._enroll_node(name="Revocation synthetic worker")
        identity = enrolled["identity"]
        node_id = enrolled["approved"]["node_id"]
        status, _headers, _raw = self._http_json(
            self.gateway.device_port, "POST", API_PREFIX + "/node/heartbeat",
            {"instance_id": self.instance_id, "capabilities": self.capabilities,
             "state": "ready"}, identity=identity)
        self.assertEqual(status, 200)
        self.coordinator.revoke_node(node_id)
        status, _headers, raw = self._http_json(
            self.gateway.device_port, "POST", API_PREFIX + "/node/heartbeat",
            {"instance_id": self.instance_id, "capabilities": self.capabilities,
             "state": "ready"}, identity=identity,
            headers={"X-Node-ID": uuid.uuid4().hex})
        self.assertEqual(status, 401, raw)
        self.assertEqual(json.loads(raw), {"error": "node_certificate_invalid"})

    def test_low_ram_unsupported_node_can_enroll_and_report_zero_memory(self):
        unsupported = {**self.capabilities,
                       "resources": {**self.capabilities["resources"], "memory_bytes": 0},
                       "state": "unsupported"}
        self.capabilities = unsupported
        enrolled = self._enroll_node(name="Low RAM synthetic worker")
        identity = enrolled["identity"]
        heartbeat = {"instance_id": self.instance_id, "capabilities": unsupported,
                     "state": "unsupported"}
        status, _headers, raw = self._http_json(
            self.gateway.device_port, "POST", API_PREFIX + "/node/heartbeat", heartbeat,
            identity=identity)
        self.assertEqual(status, 200, raw)
        status, _headers, raw = self._http_json(
            self.gateway.device_port, "POST", API_PREFIX + "/node/claim",
            {"instance_id": self.instance_id, "capabilities": unsupported}, identity=identity)
        self.assertEqual(status, 200, raw)
        claim = json.loads(raw)
        self.assertIsNone(claim["task"])
        self.assertEqual(claim["reason"], "node_not_ready")

    def test_capabilities_are_closed_five_field_wire_objects(self):
        self.assertEqual(set(self.capabilities), {"engine", "models", "resources", "platform", "state"})
        invalid = {**self.capabilities, "supported": True}
        status, _headers, raw = self._http_json(
            self.gateway.enrollment_port, "POST", API_PREFIX + "/enroll",
            {"pairing_token": self.coordinator.create_pairing_token()["pairing_token"],
             "csr_pem": "invalid", "display_name": "bad extras", "capabilities": invalid})
        # The closed capability schema is validated before CSR parsing.
        self.assertEqual(status, 400, raw)
        self.assertEqual(json.loads(raw), {"error": "invalid_capabilities"})

    def test_expired_client_certificate_is_rejected_during_mtls_handshake(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        now = datetime.now(timezone.utc)
        cert = _issue_leaf(
            self.gateway.node_ca_key, self.gateway.node_ca_cert, key,
            "expired synthetic node", eku=(ExtendedKeyUsageOID.CLIENT_AUTH,),
            not_before=now - timedelta(days=2), not_after=now - timedelta(days=1))
        cert_path = _write(self.root / "expired-client.pem", _certificate_pem(cert))
        key_path = _write(self.root / "expired-client.key", _private_key_pem(key))
        with self.assertRaises((ssl.SSLError, OSError, TimeoutError)):
            json_http(self.gateway.device_port, "POST", API_PREFIX + "/node/heartbeat",
                      {"instance_id": self.instance_id, "capabilities": self.capabilities,
                       "state": "ready"}, self.gateway.client_context(
                           certificate_path=cert_path, key_path=key_path))

    def test_shutdown_waits_at_most_tls_handshake_timeout_for_stalled_client(self):
        stalled = socket.create_connection(("127.0.0.1", self.gateway.device_port), timeout=2)
        time.sleep(0.1)
        finished = threading.Event()
        started = time.monotonic()

        def stop():
            self.gateway.device_server.shutdown()
            finished.set()

        stopper = threading.Thread(target=stop, daemon=True)
        stopper.start()
        self.assertTrue(finished.wait(6.5), "server shutdown exceeded the bounded TLS handshake timeout")
        self.assertLess(time.monotonic() - started, 6.5)
        stopper.join(timeout=1)
        stalled.close()
        self.gateway.device_server.server_close()
        self.assertFalse(self.gateway.threads[0].is_alive())

    @unittest.skipUnless(os.name == "posix", "Python SIGTERM delivery differs on Windows")
    def test_gateway_process_exits_cleanly_on_sigterm(self):
        def free_port():
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                return probe.getsockname()[1]

        device_port, enrollment_port = free_port(), free_port()
        command = [
            sys.executable, "-m", "cluster.server",
            "--db", str(self.root / "sigterm.sqlite3"),
            "--artifacts", str(self.root / "sigterm-artifacts"),
            "--device-host", "127.0.0.1", "--device-port", str(device_port),
            "--enrollment-host", "127.0.0.1", "--enrollment-port", str(enrollment_port),
            "--server-chain", str(self.gateway.server_chain_path),
            "--server-key", str(self.gateway.server_key_path),
            "--client-ca", str(self.gateway.node_ca_path),
            "--node-ca-cert", str(self.gateway.node_ca_path),
            "--node-ca-key", str(self.root / "sigterm-node-ca.key"),
        ]
        _write(self.root / "sigterm-node-ca.key", _private_key_pem(self.gateway.node_ca_key))
        environment = os.environ.copy()
        source_root = str(Path(__file__).resolve().parents[2])
        test_deps = str(Path(source_root) / ".cache" / "reconstruction-test-deps")
        environment["PYTHONPATH"] = os.pathsep.join([test_deps, source_root, environment.get('PYTHONPATH','')])
        process = subprocess.Popen(command, cwd=source_root, env=environment,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 8
            ready = False
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    with socket.create_connection(("127.0.0.1", device_port), timeout=0.2):
                        ready = True
                        break
                except OSError:
                    time.sleep(0.05)
            self.assertTrue(ready, "gateway child did not bind its device listener")
            process.send_signal(signal.SIGTERM)
            try:
                code = process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                self.fail("gateway process did not stop after SIGTERM")
            self.assertEqual(code, 0, process.stderr.read() if process.stderr else "")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            if process.stderr:
                process.stderr.close()

    def test_gateway_without_ca_key_and_separate_signer_process_approve_and_stop(self):
        def free_port(excluded=()):
            while True:
                with socket.socket() as probe:
                    probe.bind(("127.0.0.1", 0))
                    candidate = probe.getsockname()[1]
                if candidate not in excluded:
                    return candidate

        device_port = free_port()
        enrollment_port = free_port({device_port})
        db_path = self.root / "process-isolation.sqlite3"
        artifact_root = self.root / "process-isolation-artifacts"
        admin = Coordinator(db_path, artifact_root, lease_seconds=30,
                            expiry_grace_seconds=2, heartbeat_seconds=2)
        admin.update_settings({"enabled": True})
        command = [
            sys.executable, "-m", "cluster.server",
            "--db", str(db_path), "--artifacts", str(artifact_root),
            "--device-host", "127.0.0.1", "--device-port", str(device_port),
            "--enrollment-host", "127.0.0.1", "--enrollment-port", str(enrollment_port),
            "--server-chain", str(self.gateway.server_chain_path),
            "--server-key", str(self.gateway.server_key_path),
            "--client-ca", str(self.gateway.node_ca_path),
            "--node-ca-cert", str(self.gateway.node_ca_path),
        ]
        self.assertNotIn("--node-ca-key", command)
        source_root = str(Path(__file__).resolve().parents[2])
        test_deps = str(Path(source_root) / ".cache" / "reconstruction-test-deps")
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join([test_deps, source_root, environment.get('PYTHONPATH','')])
        server_process = subprocess.Popen(command, cwd=source_root, env=environment,
                                          stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                          text=True)
        try:
            deadline = time.monotonic() + 10
            ready = False
            while time.monotonic() < deadline and server_process.poll() is None:
                try:
                    status, _headers, _body = json_http(
                        enrollment_port, "POST", API_PREFIX + "/enroll/status", {},
                        self.gateway.client_context(), timeout=1.0)
                    if status == 400:
                        ready = True
                        break
                except OSError:
                    time.sleep(0.05)
            self.assertTrue(ready, "gateway child did not start enrollment TLS listener")

            key, csr = _make_csr()
            pairing_token = admin.create_pairing_token()["pairing_token"]
            status, _headers, raw = json_http(
                enrollment_port, "POST", API_PREFIX + "/enroll",
                {"pairing_token": pairing_token, "csr_pem": csr.decode("ascii"),
                 "display_name": "Process-isolated synthetic worker",
                 "capabilities": self.capabilities}, self.gateway.client_context())
            self.assertEqual(status, 200, raw)
            enrollment = json.loads(raw)
            poll_body = {"enrollment_id": enrollment["enrollment_id"],
                         "poll_token": enrollment["poll_token"]}
            queued = admin.approve_enrollment(enrollment["enrollment_id"])
            self.assertEqual(queued["state"], "pending_signing")
            status, _headers, raw = json_http(
                enrollment_port, "POST", API_PREFIX + "/enroll/status", poll_body,
                self.gateway.client_context())
            self.assertEqual(status, 200, raw)
            self.assertEqual(json.loads(raw)["state"], "provisioning")

            signer_key_path = _write(self.root / "process-isolated-node-ca.key",
                                     _private_key_pem(self.gateway.node_ca_key))
            signer_command = [
                sys.executable, "-m", "cluster.signer",
                "--db", str(db_path),
                "--node-ca-cert", str(self.gateway.node_ca_path),
                "--node-ca-key", str(signer_key_path), "--once",
            ]
            signer_run = subprocess.run(signer_command, cwd=source_root, env=environment,
                                        capture_output=True, text=True, timeout=20)
            self.assertEqual(signer_run.returncode, 0, signer_run.stderr)
            self.assertEqual(json.loads(signer_run.stdout), {"processed": 1})

            status, _headers, raw = json_http(
                enrollment_port, "POST", API_PREFIX + "/enroll/status", poll_body,
                self.gateway.client_context())
            self.assertEqual(status, 200, raw)
            approved = json.loads(raw)
            self.assertEqual(approved["state"], "approved")
            identity = self.gateway.client_identity_from_enrollment(key, approved["certificate_pem"])
            status, _headers, raw = json_http(
                device_port, "POST", API_PREFIX + "/node/heartbeat",
                {"instance_id": self.instance_id, "capabilities": self.capabilities,
                 "state": "ready"},
                self.gateway.client_context(certificate_path=identity[0], key_path=identity[1]))
            self.assertEqual(status, 200, raw)
        finally:
            if server_process.poll() is None:
                server_process.send_signal(signal.SIGTERM)
                try:
                    server_process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    server_process.kill()
                    server_process.wait(timeout=5)
            if server_process.stderr:
                server_process.stderr.close()
            admin.close()

    def test_nifti1_irregular_origin_uses_only_format_derived_float32_rounding(self):
        geometry = {
            "dimensions_xyz": [2, 2, 2],
            "spacing_xyz_mm": [0.73456789, 0.81234567, 1.12567891],
            "origin_lps_mm": [500.123456, -501.234567, 499.876543],
            "direction": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
            "original_geometry": {
                "dimensions_xyz": [2, 2, 2],
                "spacing_xyz_mm": [0.73456789, 0.81234567, 1.12567891],
                "origin_lps_mm": [500.123456, -501.234567, 499.876543],
                "direction": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
            },
            "slice_count": 2,
            "resampled_for_preview": True,
        }
        data = self.synthetic_nifti(geometry)
        descriptor = self.descriptor("nifti_mask", "prediction.total.lung_upper_lobe_left",
                                     data, labels=[1], geometry=geometry)
        path = self.root / "irregular-origin-mask.nii.gz"
        path.write_bytes(data)
        validate_output_file(path, descriptor, geometry, "infer_stage", "total")

        payload = bytearray(gzip.decompress(data))
        # Add 0.01 mm to the stored RAS x-origin. That exceeds the exact
        # float32 quantization bound and must remain a hard geometry mismatch.
        original = struct.unpack_from("<f", payload, 280 + 12)[0]
        struct.pack_into("<f", payload, 280 + 12, original - 0.01)
        changed = gzip.compress(bytes(payload), mtime=0)
        path.write_bytes(changed)
        descriptor = {**descriptor, "bytes": len(changed), "sha256": _sha(changed)}
        with self.assertRaises(Exception):
            validate_output_file(path, descriptor, geometry, "infer_stage", "total")


if __name__ == "__main__":
    unittest.main()
