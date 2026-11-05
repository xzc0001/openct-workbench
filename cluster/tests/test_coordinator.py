from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import os
import sqlite3
import struct
import sys
import tempfile
import threading
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cluster.api import (
    Coordinator,
    ProtocolError,
    compute_geometry_sha256,
    compute_input_manifest_sha256,
    compute_model_set_sha256,
    required_inference_model_ids,
)
from cluster.artifacts import validate_output_file
from cluster.protocol import API_PREFIX, FileBody, canonical_json, digest_json
from cluster.resources import ResourceCatalog
from cluster.tls import create_device_server_context, create_enrollment_server_context


def _id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_ca():
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic test CA")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=365))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                         crl_sign=True, encipher_only=None, decipher_only=None), critical=True)
            .sign(key, hashes.SHA256()))
    return key, cert


def _make_csr(key=None):
    key = key or rsa.generate_private_key(public_exponent=65537, key_size=3072)
    csr = (x509.CertificateSigningRequestBuilder()
           .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "untrusted CSR subject")]))
           .sign(key, hashes.SHA256()))
    return key, csr.public_bytes(serialization.Encoding.PEM)


class CoordinatorFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cluster-synthetic-")
        self.root = Path(self.tmp.name)
        self.coordinator = Coordinator(
            self.root / "cluster.sqlite3", self.root / "cluster-artifacts",
            lease_seconds=10, expiry_grace_seconds=2, heartbeat_seconds=2, max_attempts=3,
        )
        self.coordinators = [self.coordinator]
        self.coordinator.update_settings({"enabled": True})
        original = {"dimensions_xyz": [2, 2, 2], "spacing_xyz_mm": [1.0, 1.0, 1.0],
                    "origin_lps_mm": [10.0, 20.0, 30.0],
                    "direction": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]}
        self.geometry = {**original, "original_geometry": original, "slice_count": 2,
                         "resampled_for_preview": True}
        self.engine_id = "openct.reconstruction.cpu.windows-x64"
        self.engine_sha = _sha(b"synthetic signed engine manifest")
        self.models = self._make_models()
        self.model_set_sha = compute_model_set_sha256(self.models)
        self.source_files = {}
        self.node_certs = {}
        self.instances = {}
        self.nodes = {}

    def tearDown(self):
        for coordinator in self.coordinators:
            try:
                coordinator.close()
            except Exception:
                pass
        self.tmp.cleanup()

    def _make_models(self):
        rows = []
        for number in (117, 291, 292, 293, 294, 295, 297, 298, 913):
            name = f"Dataset{number}_SyntheticModel_v1"
            rel = f"models/{name}/weights.bin"
            content = f"weights:{name}".encode()
            file_row = {"path": rel, "bytes": len(content), "sha256": _sha(content)}
            model_sha = hashlib.sha256(canonical_json([file_row])).hexdigest()
            model_id = hashlib.sha256(("totalsegmentator-model-v1:" + name).encode()).hexdigest()[:32]
            rows.append({"id": model_id, "name": name, "sha256": model_sha, "files": [rel]})
        return sorted(rows, key=lambda row: row["id"])

    def _write_source(self, job_id, revision, role, content):
        directory = self.root / "inputs" / job_id / str(revision)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{role}.bin"
        path.write_bytes(content)
        self.source_files[(job_id, revision, role)] = content
        return {"id": _id(f"input:{job_id}:{revision}:{role}"), "role": role,
                "type": {"ct": "nifti_ct", "native_ct": "nifti_ct", "geometry": "geometry_json", "native_instances": "instances_json"}[role],
                "bytes": len(content), "sha256": _sha(content), "source_path": str(path)}

    def build_plan(self, *, job_id=None, revision=1, selected=("lungs",), mode="quick"):
        job_id = job_id or uuid.uuid4().hex
        geometry_bytes = json.dumps(self.geometry, sort_keys=True, separators=(",", ":")).encode()
        inputs = [
            self._write_source(job_id, revision, "ct", b"synthetic canonical CT bytes only"),
            self._write_source(job_id, revision, "native_ct", b"synthetic native CT bytes only"),
            self._write_source(job_id, revision, "geometry", geometry_bytes),
            self._write_source(job_id, revision, "native_instances", b"[1,2]"),
        ]
        all_ids = [row["id"] for row in inputs]
        canonical_inputs = [row["id"] for row in inputs if row["role"] in {"ct", "geometry"}]
        native_ct_id = next(row["id"] for row in inputs if row["role"] == "native_ct")
        stages = []
        from cluster.coordinator import _canonical_modules
        structures, progressive = _canonical_modules()
        inference_plan = structures.inference_plan(list(selected), mode)
        for _task, task_key, _args, _label, _percent in inference_plan:
            stages.append({"stage_key": task_key, "action": "infer_stage", "mode": mode,
                           "structures": list(selected), "parameters": {"task_key": task_key, "thread_policy": "node_budget"},
                           "resources": {"threads": 1, "memory_bytes": 1024, "cpu_percent": 50},
                           "dependencies": [], "input_ids": canonical_inputs,
                           "model_ids": required_inference_model_ids(self.models, task_key)})
        dependencies = progressive.structure_dependencies(mode)
        for structure in selected:
            if structure not in dependencies or structure == "margin":
                continue
            structures_for_stage = [structure] + (["margin"] if structure == "nodules" and "margin" in selected else [])
            stage_inputs = canonical_inputs + ([native_ct_id] if structure == "ribs" else [])
            stages.append({"stage_key": "mesh_" + structure, "action": "mesh_stage", "mode": mode,
                           "structures": structures_for_stage, "parameters": {"thread_policy": "node_budget"},
                           "resources": {"threads": 1, "memory_bytes": 1024, "cpu_percent": 50},
                           "dependencies": sorted(dependencies[structure]), "input_ids": stage_inputs, "model_ids": []})
        if "suspects" in selected:
            required = {key for _task, key, args, _label, _percent in inference_plan
                        if key == "lung_vessels" or set(args.get("roi_subset", [])) & set(structures.LOBE_NAMES)}
            required.update(stage["stage_key"] for stage in stages if stage["action"] == "mesh_stage")
            stages.append({"stage_key": "candidate_suspects", "action": "candidate_stage", "mode": mode,
                           "structures": ["suspects"], "parameters": {"candidate_parameters": {}, "manual_points_lps": [], "thread_policy": "node_budget"},
                           "resources": {"threads": 1, "memory_bytes": 1024, "cpu_percent": 50},
                           "dependencies": sorted(required), "input_ids": canonical_inputs, "model_ids": []})
        values = {"mode": mode, "selected": list(selected), "stages": stages, "input_manifest": inputs,
                  "geometry": self.geometry, "engine_id": self.engine_id, "engine_sha256": self.engine_sha,
                  "model_ids": self.models, "model_set_sha256": self.model_set_sha,
                  "input_sha256": compute_input_manifest_sha256(inputs),
                  "geometry_sha256": compute_geometry_sha256(self.geometry)}
        return job_id, revision, values

    def enqueue(self, coordinator=None, **kwargs):
        coordinator = coordinator or self.coordinator
        job_id, revision, values = self.build_plan(**kwargs)
        coordinator.enqueue_job(job_id, revision, **values)
        return job_id, revision, values

    def register_node(self, *, der=None, model_rows=None, coordinator=None, node_id=None):
        coordinator = coordinator or self.coordinator
        node_id = node_id or uuid.uuid4().hex
        der = der or (b"synthetic-client-certificate-" + node_id.encode())
        models = model_rows or [{"id": row["id"], "sha256": row["sha256"]} for row in self.models]
        capabilities = {"engine": {"id": self.engine_id, "sha256": self.engine_sha},
                        "models": sorted(models, key=lambda row: row["id"]),
                        "resources": {"threads": 8, "memory_bytes": 1024 * 1024 * 1024, "cpu_percent": 80, "free_disk_bytes": 20 * 1024**3},
                        "platform": {"os": "windows", "arch": "x86_64", "version": "synthetic"},
                        "state": "ready"}
        normalized = validate_caps(capabilities)
        now = time.time()
        fingerprint = hashlib.sha256(der).hexdigest()
        with coordinator.store.transaction() as db:
            db.execute("INSERT INTO nodes(node_id,certificate_fingerprint,certificate_der,certificate_pem,display_name,capabilities_json,status,admin_enabled,created_at,approved_at,certificate_not_after) VALUES(?,?,?,?,?,?,'ready',1,?,?,?)",
                       (node_id, fingerprint, der, "synthetic", "synthetic node", json.dumps(normalized, sort_keys=True), now, now, now + 3600))
        self.node_certs[node_id] = der
        self.instances[node_id] = uuid.uuid4().hex
        self.nodes[node_id] = normalized
        return node_id

    def claim(self, node_id, coordinator=None, *, instance_id=None):
        coordinator = coordinator or self.coordinator
        return coordinator.claim(self.node_certs[node_id], {"instance_id": instance_id or self.instances[node_id], "capabilities": self.nodes[node_id]})

    def binding(self, task):
        names = ("protocol_version", "job_id", "job_revision", "stage_key", "stage_attempt", "lease_id", "fencing_generation", "node_id", "engine_id", "engine_sha256", "model_set_sha256", "input_sha256", "geometry_sha256")
        return {key: task[key] for key in names}

    def synthetic_nifti(self, geometry=None, pixels=None):
        geometry = geometry or self.geometry
        pixels = bytes(pixels or [0, 1, 0, 1, 0, 1, 0, 1])
        header = bytearray(348)
        struct.pack_into("<i", header, 0, 348)
        struct.pack_into("<8h", header, 40, 3, *geometry["dimensions_xyz"], 1, 1, 1, 1)
        struct.pack_into("<2h", header, 70, 2, 8)
        struct.pack_into("<8f", header, 76, 1.0, *geometry["spacing_xyz_mm"], 1.0, 1.0, 1.0, 1.0)
        struct.pack_into("<f", header, 108, 352.0)
        struct.pack_into("<2f", header, 112, 1.0, 0.0)
        struct.pack_into("<h", header, 254, 1)
        direction = geometry["direction"]
        spacing = geometry["spacing_xyz_mm"]
        origin = geometry["origin_lps_mm"]
        for row, offset, sign in ((0, 280, -1), (1, 296, -1), (2, 312, 1)):
            ras_row = [sign * direction[row * 3 + col] * spacing[col] for col in range(3)]
            ras_origin = sign * origin[row]
            struct.pack_into("<4f", header, offset, *ras_row, ras_origin)
        header[344:348] = b"n+1\x00"
        return gzip.compress(bytes(header) + b"\x00\x00\x00\x00" + pixels, mtime=0)

    def descriptor(self, artifact_type, role, data, *, labels=None, geometry=None, artifact_id=None):
        row = {"id": artifact_id or _id("artifact:" + role), "type": artifact_type, "role": role,
               "bytes": len(data), "sha256": _sha(data), "geometry": geometry or self.geometry}
        if labels is not None:
            row["labels"] = labels
        return row

    def complete_inference(self, node_id, task):
        if task["action"] != "infer_stage":
            raise AssertionError("expected an inference lease")
        from cluster.coordinator import _canonical_modules
        structures, _ = _canonical_modules()
        plan = {row[1]: row for row in structures.inference_plan(task["structures"], task["mode"])}
        row = plan[task["stage_key"]]
        labels = row[2].get("roi_subset") or {"lung_vessels": ["lung_arteries", "lung_veins", "lung_airways"], "lung_nodules": ["lung_nodules"]}.get(task["stage_key"], [])
        receipt = json.dumps({"device": "cpu", "offline_guard": True,
                              "tasks": [{"task": row[0], "key": task["stage_key"], "seconds": 0.1, "resolution_mm": 3.0}]},
                             separators=(",", ":")).encode()
        mask = self.synthetic_nifti()
        pairs = [(self.descriptor("inference_json", f"inference.{task['stage_key']}", receipt), receipt)]
        pairs.extend((self.descriptor("nifti_mask", f"prediction.{task['stage_key']}.{label}", mask, labels=[1]), mask) for label in labels)
        manifest = self.coordinator.begin_result(self.node_certs[node_id], {"binding": self.binding(task), "artifacts": [desc for desc, _ in pairs], "metrics": {}})
        content = {desc["id"]: blob for desc, blob in pairs}
        for upload in manifest["uploads"]:
            blob = content[upload["artifact_id"]]
            self.coordinator.put_result(self.node_certs[node_id], task["lease_id"], upload["upload_id"], blob, len(blob))
        self.coordinator.finalize_result(self.node_certs[node_id], {"binding": self.binding(task)})


def validate_caps(value):
    from cluster.protocol import validate_capabilities
    return validate_capabilities(value)


class SchedulerTests(CoordinatorFixture):
    def test_competing_claims_bind_exact_inputs_and_ignore_forged_identity_headers(self):
        job_id, _revision, _values = self.enqueue()
        node_a = self.register_node()
        node_b = self.register_node()
        second = Coordinator(self.root / "cluster.sqlite3", self.root / "cluster-artifacts", lease_seconds=10, expiry_grace_seconds=2, heartbeat_seconds=2)
        self.coordinators.append(second)
        barrier = threading.Barrier(2)
        results = []

        def compete(coordinator, node):
            barrier.wait()
            results.append((node, self.claim(node, coordinator)))

        threads = [threading.Thread(target=compete, args=(self.coordinator, node_a)), threading.Thread(target=compete, args=(second, node_b))]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=5)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        winners = [(node, row["task"]) for node, row in results if row["task"]]
        self.assertEqual(len(winners), 1)
        winner_id, task = winners[0]
        self.assertEqual(task["node_id"], winner_id)
        self.assertEqual(task["input_sha256"], compute_input_manifest_sha256(task["inputs"]))
        self.assertEqual({row["role"] for row in task["inputs"]}, {"ct", "geometry"})
        self.assertNotIn("source_path", task["inputs"][0])
        with self.coordinator.store.read() as db:
            active = db.execute("SELECT COUNT(*) FROM leases WHERE state='active'").fetchone()[0]
        self.assertEqual(active, 1)
        spoofed = self.coordinator.handle_device_request(
            "POST", API_PREFIX + "/node/claim",
            json.dumps({"instance_id": self.instances[winner_id], "capabilities": self.nodes[winner_id]}).encode(),
            self.node_certs[winner_id], {"X-Node-ID": node_b, "Content-Type": "application/json"},
        )
        replay = json.loads(spoofed.body)
        self.assertEqual(spoofed.status, 200)
        self.assertEqual(replay["task"]["node_id"], winner_id)
        other_instance = self.coordinator.claim(self.node_certs[winner_id], {"instance_id": uuid.uuid4().hex, "capabilities": self.nodes[winner_id]})
        self.assertIsNone(other_instance["task"])
        self.assertEqual(other_instance["reason"], "active_lease_other_instance")

    def test_dag_artifact_validation_outbox_single_publisher_and_idempotent_finalize(self):
        job_id, revision, _values = self.enqueue()
        node = self.register_node()
        claim = self.claim(node)
        infer = claim["task"]
        self.assertEqual(infer["stage_key"], "total")
        artifacts = []
        receipt = json.dumps({"device": "cpu", "offline_guard": True,
                              "tasks": [{"task": "total", "key": "total", "seconds": 0.25, "resolution_mm": 3.0}]},
                             separators=(",", ":")).encode()
        artifacts.append((self.descriptor("inference_json", "inference.total", receipt), receipt))
        lobe_names = ["lung_upper_lobe_left", "lung_lower_lobe_left", "lung_upper_lobe_right", "lung_middle_lobe_right", "lung_lower_lobe_right"]
        mask = self.synthetic_nifti()
        for label in lobe_names:
            artifacts.append((self.descriptor("nifti_mask", f"prediction.total.{label}", mask, labels=[1]), mask))
        begin = self.coordinator.begin_result(self.node_certs[node], {"binding": self.binding(infer), "artifacts": [row for row, _ in artifacts], "metrics": {"wall_seconds": 0.25, "cpu_seconds": None, "peak_rss_bytes": None, "transfer_seconds": None}})
        self.assertEqual(len(begin["uploads"]), len(artifacts))
        artifact_bytes = {descriptor["id"]: content for descriptor, content in artifacts}
        for upload in begin["uploads"]:
            content = artifact_bytes[upload["artifact_id"]]
            result = self.coordinator.put_result(self.node_certs[node], infer["lease_id"], upload["upload_id"], content, len(content))
            self.assertEqual(result, {"received": True, "duplicate": False})
            duplicate = self.coordinator.put_result(self.node_certs[node], infer["lease_id"], upload["upload_id"], content, len(content))
            self.assertEqual(duplicate, {"received": True, "duplicate": True})
        completed = self.coordinator.finalize_result(self.node_certs[node], {"binding": self.binding(infer)})
        self.assertEqual(completed, {"state": "completed", "duplicate": False})
        self.assertEqual(self.coordinator.finalize_result(self.node_certs[node], {"binding": self.binding(infer)}), {"state": "completed", "duplicate": True})

        publisher2 = Coordinator(self.root / "cluster.sqlite3", self.root / "cluster-artifacts", lease_seconds=10, expiry_grace_seconds=2, heartbeat_seconds=2)
        self.coordinators.append(publisher2)
        event = self.coordinator.claim_publish_event()
        self.assertIsNotNone(event)
        self.assertEqual(event["stage_key"], "total")
        self.assertTrue(event["artifacts"])
        self.assertIsNone(publisher2.claim_publish_event())
        self.assertTrue(self.coordinator.ack_publish_event(event["event_id"]))
        self.coordinator.release_publisher()

        mesh_task = self.claim(node)["task"]
        self.assertEqual(mesh_task["stage_key"], "mesh_lungs")
        self.assertNotEqual(mesh_task["input_sha256"], _values["input_sha256"])
        self.assertEqual(mesh_task["input_sha256"], compute_input_manifest_sha256(mesh_task["inputs"]))
        dependency = next(row for row in mesh_task["inputs"] if row["role"] == "inference.total")
        partial = self.coordinator.input_response(self.node_certs[node], mesh_task["lease_id"], dependency["id"], "bytes=0-15")
        self.assertEqual(partial.status, 206)
        self.assertEqual(len(b"".join(partial.body.iter_chunks())), 16)
        full = self.coordinator.input_response(self.node_certs[node], mesh_task["lease_id"], dependency["id"])
        full_bytes = b"".join(full.body.iter_chunks())
        self.assertEqual(len(full_bytes), dependency["bytes"])
        model_bytes = struct.pack("<9f", 0, 0, 0, 1, 0, 0, 0, 1, 0)
        index_bytes = struct.pack("<3I", 0, 1, 2)
        normal_bytes = struct.pack("<9f", 0, 0, 1, 0, 0, 1, 0, 0, 1)
        mesh_value = {"schema": 1, "selected": ["lungs"], "models": [{"id": "lungs", "name": "Synthetic lungs", "group": "lungs", "color": [0.2, 0.4, 0.6], "opacity": 1.0,
                     "positions": base64.b64encode(model_bytes).decode(), "indices": base64.b64encode(index_bytes).decode(), "normals": base64.b64encode(normal_bytes).decode(), "vertices": 3, "triangles": 1, "bounds_ras": [0, 1, 0, 1, 0, 0], "voxels": 1}],
                      "masks": [], "nodules": [], "missing": [], "canonical_geometry": self.geometry, "native_geometry": self.geometry["original_geometry"]}
        mesh_bytes = json.dumps(mesh_value, separators=(",", ":")).encode()
        mesh_descriptor = self.descriptor("mesh_json", "surface_export", mesh_bytes)
        mesh_begin = self.coordinator.begin_result(self.node_certs[node], {"binding": self.binding(mesh_task), "artifacts": [mesh_descriptor], "metrics": {}})
        upload = mesh_begin["uploads"][0]
        self.coordinator.put_result(self.node_certs[node], mesh_task["lease_id"], upload["upload_id"], mesh_bytes, len(mesh_bytes))
        self.assertEqual(self.coordinator.finalize_result(self.node_certs[node], {"binding": self.binding(mesh_task)}), {"state": "completed", "duplicate": False})
        self.assertIn(job_id, self.coordinator.active_job_ids())
        final_event = publisher2.claim_publish_event()
        self.assertIsNotNone(final_event)
        self.assertTrue(final_event["job_complete"])
        self.assertTrue(publisher2.ack_publish_event(final_event["event_id"]))
        self.assertNotIn(job_id, self.coordinator.active_job_ids())
        snapshot = self.coordinator.job_snapshot(job_id)
        self.assertEqual(snapshot["job_revision"], revision)
        self.assertEqual(snapshot["state"], "completed")
        with self.coordinator.store.read() as db:
            path = Path(db.execute("SELECT path FROM stage_artifacts WHERE job_id=? AND role='surface_export'", (job_id,)).fetchone()[0])
        self.assertEqual(path.read_bytes(), mesh_bytes)
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o400)

    def _inference_result_pairs(self, task):
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
                                         "seconds": 0.1, "resolution_mm": 3.0}]},
                             separators=(",", ":")).encode()
        mask = self.synthetic_nifti()
        pairs = [(self.descriptor("inference_json", f"inference.{task['stage_key']}", receipt), receipt)]
        pairs.extend((self.descriptor("nifti_mask", f"prediction.{task['stage_key']}.{label}",
                                      mask, labels=[1]), mask) for label in labels)
        return pairs

    def test_startup_preserves_live_upload_part_while_other_coordinator_is_opened(self):
        self.enqueue()
        node = self.register_node()
        task = self.claim(node)["task"]
        pairs = self._inference_result_pairs(task)
        body = {"binding": self.binding(task), "artifacts": [desc for desc, _data in pairs], "metrics": {}}
        manifest = self.coordinator.begin_result(self.node_certs[node], body)
        contents = {desc["id"]: data for desc, data in pairs}
        upload = manifest["uploads"][0]
        payload = contents[upload["artifact_id"]]
        part = self.coordinator.staging_root / f"{upload['upload_id']}.part"
        entered = threading.Event()
        resume = threading.Event()
        outcome = []

        class HeldStream(io.BytesIO):
            def read(self, size=-1):
                if not entered.is_set():
                    entered.set()
                    if not resume.wait(10):
                        raise TimeoutError("test stream was not released")
                return super().read(size)

        def upload_in_first_process():
            try:
                outcome.append(self.coordinator.put_result(
                    self.node_certs[node], task["lease_id"], upload["upload_id"],
                    HeldStream(payload), len(payload)))
            except BaseException as error:
                outcome.append(error)

        writer = threading.Thread(target=upload_in_first_process, daemon=True)
        writer.start()
        self.assertTrue(entered.wait(5), "upload did not reach its held body reader")
        self.assertTrue(part.is_file(), "upload part was not opened before stream read")
        second = Coordinator(self.root / "cluster.sqlite3", self.root / "cluster-artifacts",
                             lease_seconds=10, expiry_grace_seconds=2, heartbeat_seconds=2)
        try:
            self.assertTrue(part.is_file(), "a second coordinator startup removed a live upload part")
            self.assertTrue(writer.is_alive(), "the held stream unexpectedly completed")
        finally:
            resume.set()
            writer.join(timeout=10)
            second.close()
        self.assertFalse(writer.is_alive())
        self.assertEqual(outcome, [{"received": True, "duplicate": False}])
        with self.coordinator.store.read() as db:
            state = db.execute("SELECT state FROM uploads WHERE upload_id=?", (upload["upload_id"],)).fetchone()[0]
        self.assertEqual(state, "validated")

    def test_result_upload_rejects_simulated_enospc_with_storage_unavailable(self):
        self.enqueue()
        node = self.register_node()
        task = self.claim(node)["task"]
        pairs = self._inference_result_pairs(task)
        manifest = self.coordinator.begin_result(
            self.node_certs[node],
            {"binding": self.binding(task), "artifacts": [desc for desc, _data in pairs], "metrics": {}})
        contents = {desc["id"]: data for desc, data in pairs}
        upload = manifest["uploads"][0]
        with mock.patch("cluster.coordinator.shutil.disk_usage",
                        return_value=SimpleNamespace(free=0)):
            with self.assertRaises(ProtocolError) as caught:
                self.coordinator.put_result(
                    self.node_certs[node], task["lease_id"], upload["upload_id"],
                    contents[upload["artifact_id"]], upload["bytes"])
        self.assertEqual(caught.exception.status, 507)
        self.assertEqual(caught.exception.code, "storage_unavailable")
        self.assertFalse((self.coordinator.staging_root / f"{upload['upload_id']}.part").exists())

    def test_resource_exhaustion_is_terminal_and_network_failures_remain_bounded(self):
        job_id, _revision, _values = self.enqueue()
        node = self.register_node()
        task = self.claim(node)["task"]
        response = self.coordinator.handle_device_request(
            "POST", API_PREFIX + "/node/release",
            json.dumps({"binding": self.binding(task), "retryable": False,
                        "error_code": "resource_exhausted"}).encode(),
            self.node_certs[node], {"Content-Type": "application/json"})
        self.assertEqual(response.status, 200, response.body)
        self.assertEqual(json.loads(response.body)["state"], "failed")
        snapshot = self.coordinator.job_snapshot(job_id)
        self.assertEqual(snapshot["state"], "failed")
        stage = next(row for row in snapshot["stages"] if row["stage_key"] == task["stage_key"])
        self.assertEqual((stage["status"], stage["error_code"]), ("failed", "resource_exhausted"))
        no_retry = self.claim(node)
        self.assertIsNone(no_retry["task"])
        self.assertEqual(no_retry["reason"], "queue_empty")

        retry_job, _revision, _values = self.enqueue()
        retry_node = self.register_node()
        retry_task = self.claim(retry_node)["task"]
        attempts = []
        while True:
            attempts.append(retry_task["stage_attempt"])
            result = self.coordinator.release(self.node_certs[retry_node], {
                "binding": self.binding(retry_task), "retryable": True,
                "error_code": "input_transfer_failed"})
            if result["state"] == "failed":
                break
            retry_task = self.claim(retry_node)["task"]
            self.assertIsNotNone(retry_task)
        self.assertEqual(attempts, [1, 2, 3])
        retry_snapshot = self.coordinator.job_snapshot(retry_job)
        self.assertEqual(retry_snapshot["state"], "failed")
        self.assertEqual(self.claim(retry_node)["reason"], "queue_empty")

    def test_resource_exhaustion_failure_restores_previous_workbench_preview(self):
        import sys
        workbench = Path(__file__).resolve().parents[2] / "workbench"
        sys.path.insert(0, str(workbench))
        from cluster_bridge import ClusterBridge
        from study_updates import CONFIG_KEYS, begin, restore

        job_id, _revision, _values = self.enqueue()
        node = self.register_node()
        task = self.claim(node)["task"]
        result = self.coordinator.release(self.node_certs[node], {
            "binding": self.binding(task), "retryable": False,
            "error_code": "resource_exhausted"})
        self.assertEqual(result["state"], "failed")

        jobs = self.root / "workbench-jobs"
        job = jobs / job_id
        output = job / "output"
        output.mkdir(parents=True)
        previous_preview = b"synthetic previous-version preview"
        (output / "preview.html").write_bytes(previous_preview)
        status = {"id": job_id, "state": "running", "cluster_managed": True,
                  "cluster_revision": 1, "mode": "full", "structures": ["lungs"],
                  "inplace_operation": "run", "created_at": 1}
        previous_status = {**status, "state": "completed", "cluster_managed": False,
                           "cluster_revision": 0, "mode": "quick", "structures": ["heart"]}
        begin(job, previous_status)
        self.assertEqual((output / "revisions/study-previous/preview.html").read_bytes(), previous_preview)
        pending_preview = output / "preview.html.pending"
        pending_preview.write_bytes(b"synthetic partial failed update")
        pending_preview.replace(output / "preview.html")
        status_path = job / "status.json"

        def read(path):
            return json.loads(Path(path).read_text(encoding="utf-8"))

        def write(path, value):
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

        write(status_path, status)
        self.assertEqual(self.coordinator.job_snapshot(job_id)["state"], "failed")
        self.assertEqual(self.coordinator.job_snapshot(job_id)["job_revision"], 1)
        self.assertEqual(self.coordinator.active_job_ids(), [])
        finish_calls = []

        def finish_update(target, success, error=None):
            finish_calls.append((target, success, error))
            current = read(target / "status.json")
            if success or not current.get("inplace_operation"):
                return False
            old = restore(target)
            self.assertEqual((target / "output/preview.html").read_bytes(), previous_preview)
            for key in CONFIG_KEYS:
                current.pop(key, None)
            current.update(old)
            current.pop("inplace_operation", None)
            current.update(state="completed", error=error)
            write(target / "status.json", current)
            return True

        app = SimpleNamespace(ROOT=self.root, JOBS=jobs, LOCK=threading.RLock(),
                              read=read, write=write, finish_update=finish_update,
                              update=lambda target, **values: write(target / "status.json", {**read(target / "status.json"), **values}))
        bridge = object.__new__(ClusterBridge)
        bridge.app = app
        bridge.coordinator = self.coordinator
        bridge.last_sync = time.monotonic()
        self.assertTrue(bridge.snapshot(job)["state"] == "failed")
        bridge.sync_progress()
        self.assertEqual(len(finish_calls), 1)
        restored = read(status_path)
        self.assertEqual((output / "preview.html").read_bytes(), previous_preview)
        self.assertEqual(restored["state"], "completed")
        self.assertEqual(restored["structures"], ["heart"])
        self.assertNotIn("inplace_operation", restored)

    def test_revision_cancel_delete_expiry_and_fence_requeue(self):
        job_id, revision, first_values = self.enqueue()
        node_a = self.register_node()
        old_task = self.claim(node_a)["task"]
        with self.coordinator.store.transaction() as db:
            db.execute("UPDATE leases SET expires_at=?,grace_until=? WHERE lease_id=?", (time.time() - 2, time.time() - 1, old_task["lease_id"]))
        restarted = Coordinator(self.root / "cluster.sqlite3", self.root / "cluster-artifacts", lease_seconds=10, expiry_grace_seconds=2, heartbeat_seconds=2)
        self.coordinators.append(restarted)
        retry = self.claim(node_a, coordinator=restarted)["task"]
        self.assertEqual(retry["stage_attempt"], 2)
        self.assertEqual(retry["fencing_generation"], 2)
        with self.assertRaises(ProtocolError):
            restarted.renew(self.node_certs[node_a], {"binding": self.binding(old_task)})
        revised_id, new_revision, second_values = self.build_plan(job_id=job_id, revision=revision + 1)
        self.assertEqual(revised_id, job_id)
        restarted.enqueue_job(job_id, new_revision, **second_values)
        self.assertFalse(restarted.publication_guard(_id("missing event")))
        with self.assertRaises(ProtocolError):
            restarted.renew(self.node_certs[node_a], {"binding": self.binding(retry)})
        current = restarted.job_snapshot(job_id)
        self.assertEqual(current["revision"], new_revision)
        self.assertEqual(current["state"], "active")
        restarted.cancel_job(job_id)
        self.assertEqual(restarted.job_snapshot(job_id)["state"], "cancelled")
        with self.assertRaises(ProtocolError):
            restarted.input_response(self.node_certs[node_a], retry["lease_id"], retry["inputs"][0]["id"])
        restarted.tombstone_job(job_id)
        self.assertEqual(restarted.job_snapshot(job_id)["state"], "tombstoned")
        self.assertFalse((self.root / "cluster-artifacts" / "results" / job_id).exists())
        with self.assertRaises(ProtocolError):
            restarted.enqueue_job(job_id, new_revision + 1, **self.build_plan(job_id=job_id, revision=new_revision + 1)[2])

    def test_revocation_invalidates_old_cert_and_requeues_to_another_node(self):
        _job_id, _revision, _values = self.enqueue()
        node_a = self.register_node()
        node_b = self.register_node()
        task = self.claim(node_a)["task"]
        result = self.coordinator.revoke_node(node_a)
        self.assertEqual(result["leases_invalidated"], 1)
        with self.assertRaises(ProtocolError):
            self.coordinator.renew(self.node_certs[node_a], {"binding": self.binding(task)})
        retry = self.claim(node_b)["task"]
        self.assertEqual(retry["stage_attempt"], 2)
        self.assertEqual(retry["fencing_generation"], 2)
        self.assertEqual(retry["node_id"], node_b)

    def test_candidate_stage_restores_selected_same_id_masks_with_ct_binding(self):
        job_id = uuid.uuid4().hex
        revision = 1
        geometry_path = self.root / "candidate-geometry.json"
        geometry_path.write_text(json.dumps(self.geometry, sort_keys=True), encoding="utf-8")
        ct_bytes = b"synthetic canonical CT for candidate replay"
        ct_path = self.root / "candidate-ct.nii.gz"
        ct_path.write_bytes(ct_bytes)
        review_role = "candidate_review"
        candidate_mask_id = _id("candidate-mask-opaque-artifact")
        mask_role = "candidate_mask." + candidate_mask_id
        crop_geometry = {"dimensions_xyz": [1, 1, 1], "spacing_xyz_mm": [1.0, 1.0, 1.0], "origin_lps_mm": [10.0, 20.0, 30.0],
                         "direction": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0], "coordinate_system": "LPS", "dtype": "uint8"}
        mask_bytes = self.synthetic_nifti(crop_geometry, b"\x01")
        mask_path = self.root / "candidate-mask.nii.gz"
        mask_path.write_bytes(mask_bytes)
        ct_id, geometry_id = _id("candidate-ct-input"), _id("candidate-geometry-input")
        review_id, mask_input_id = _id("candidate-review-input"), _id("candidate-mask-input")
        review_row = {"id": "suspect_001", "mask_role": mask_role, "name": "synthetic candidate", "volume_mm3": 1.0}
        review_value = {"version": 2, "source_ct_sha256": _sha(ct_bytes), "parameters": {}, "candidates": [review_row],
                        "identity": {_sha(b"synthetic-region-fingerprint"): "suspect_001"}, "seconds": 0.1,
                        "method": "synthetic mask only", "limitations": ["synthetic test"]}
        review_bytes = json.dumps(review_value, separators=(",", ":")).encode()
        review_path = self.root / "candidate-review.json"
        review_path.write_bytes(review_bytes)
        inputs = [
            {"id": ct_id, "role": "ct", "type": "nifti_ct", "bytes": len(ct_bytes), "sha256": _sha(ct_bytes), "source_path": str(ct_path), "geometry": self.geometry},
            {"id": geometry_id, "role": "geometry", "type": "geometry_json", "bytes": geometry_path.stat().st_size, "sha256": _sha(geometry_path.read_bytes()), "source_path": str(geometry_path)},
            {"id": review_id, "role": review_role, "type": "candidates_json", "bytes": len(review_bytes), "sha256": _sha(review_bytes), "source_path": str(review_path), "geometry": self.geometry},
            {"id": mask_input_id, "role": mask_role, "type": "nifti_mask", "bytes": len(mask_bytes), "sha256": _sha(mask_bytes), "source_path": str(mask_path), "geometry": crop_geometry, "labels": [1]},
        ]
        structures, progressive = __import__("cluster.coordinator", fromlist=["_canonical_modules"])._canonical_modules()
        selected = ["suspects"]
        inference_plan = structures.inference_plan(selected, "quick")
        stages = []
        for _task, key, _args, _label, _percent in inference_plan:
            stages.append({"stage_key": key, "action": "infer_stage", "mode": "quick", "structures": selected,
                           "parameters": {"task_key": key, "thread_policy": "node_budget"},
                           "resources": {"threads": 1, "memory_bytes": 1024, "cpu_percent": 50}, "dependencies": [],
                           "input_ids": [ct_id, geometry_id], "model_ids": required_inference_model_ids(self.models, key)})
        required = {key for _task, key, args, _label, _percent in inference_plan if key == "lung_vessels" or set(args.get("roi_subset", [])) & set(structures.LOBE_NAMES)}
        stages.append({"stage_key": "candidate_suspects", "action": "candidate_stage", "mode": "quick", "structures": ["suspects"],
                       "parameters": {"candidate_parameters": {}, "manual_points_lps": [], "selected_candidate_ids": ["suspect_001"], "thread_policy": "node_budget"},
                       "resources": {"threads": 1, "memory_bytes": 1024, "cpu_percent": 50}, "dependencies": sorted(required),
                       "input_ids": [row["id"] for row in inputs], "model_ids": []})
        self.coordinator.enqueue_job(job_id, revision, mode="quick", selected=selected, stages=stages, input_manifest=inputs,
                                     geometry=self.geometry, engine_id=self.engine_id, engine_sha256=self.engine_sha,
                                     model_ids=self.models, model_set_sha256=self.model_set_sha,
                                     input_sha256=compute_input_manifest_sha256(inputs), geometry_sha256=compute_geometry_sha256(self.geometry))
        node = self.register_node()
        for _ in range(len(inference_plan)):
            task = self.claim(node)["task"]
            self.assertIsNotNone(task)
            self.assertEqual(task["action"], "infer_stage")
            self.complete_inference(node, task)
        candidate_task = self.claim(node)["task"]
        self.assertEqual(candidate_task["stage_key"], "candidate_suspects")
        self.assertEqual(candidate_task["parameters"]["selected_candidate_ids"], ["suspect_001"])
        role_inputs = {row["role"]: row for row in candidate_task["inputs"]}
        self.assertIn("candidate_review", role_inputs)
        self.assertIn(mask_role, role_inputs)
        self.assertNotIn("native_ct", role_inputs)
        output_mask = self.descriptor("nifti_mask", mask_role, mask_bytes, labels=[1], geometry=crop_geometry, artifact_id=candidate_mask_id)
        output_review_value = {**review_value, "candidates": [review_row]}
        output_review = json.dumps(output_review_value, separators=(",", ":")).encode()
        output_review_descriptor = self.descriptor("candidates_json", "candidate_review", output_review, geometry=self.geometry)
        manifest = self.coordinator.begin_result(self.node_certs[node], {"binding": self.binding(candidate_task), "artifacts": [output_review_descriptor, output_mask], "metrics": {}})
        blobs = {output_review_descriptor["id"]: output_review, output_mask["id"]: mask_bytes}
        for upload in manifest["uploads"]:
            content = blobs[upload["artifact_id"]]
            self.coordinator.put_result(self.node_certs[node], candidate_task["lease_id"], upload["upload_id"], content, len(content))
        self.coordinator.finalize_result(self.node_certs[node], {"binding": self.binding(candidate_task)})
        self.assertEqual(self.coordinator.job_snapshot(job_id)["state"], "completed")

    def test_oversized_json_unapproved_certificate_and_bad_artifact_are_rejected(self):
        _job, _revision, _values = self.enqueue()
        node = self.register_node()
        unauth = self.coordinator.handle_device_request("POST", API_PREFIX + "/node/claim", b"{}", b"unapproved DER", {"Content-Type": "application/json"})
        self.assertEqual(unauth.status, 401)
        oversized = self.coordinator.handle_device_request("POST", API_PREFIX + "/node/claim", b" " * (1024 * 1024 + 1), self.node_certs[node], {"Content-Type": "application/json"})
        self.assertEqual(oversized.status, 413)
        task = self.claim(node)["task"]
        bad = self.descriptor("mesh_json", "html_preview", b"<html></html>")
        with self.assertRaises(ProtocolError):
            self.coordinator.begin_result(self.node_certs[node], {"binding": self.binding(task), "artifacts": [bad], "metrics": {}})
        wrong_hash = self.descriptor("inference_json", "inference.total", b"{}")
        # The exact output role set is checked before any bytes can be staged.
        with self.assertRaises(ProtocolError):
            self.coordinator.begin_result(self.node_certs[node], {"binding": self.binding(task), "artifacts": [wrong_hash], "metrics": {}})
        bad_mesh = {"schema": 1, "selected": ["lungs"], "models": [{"id": "lungs", "positions": base64.b64encode(struct.pack("<9f", *([0.0] * 9))).decode(),
                    "indices": base64.b64encode(struct.pack("<3I", 0, 1, 99)).decode(), "normals": base64.b64encode(struct.pack("<9f", *([0.0] * 9))).decode(), "vertices": 3, "triangles": 1}],
                    "masks": [], "nodules": [], "missing": [], "canonical_geometry": self.geometry, "native_geometry": self.geometry["original_geometry"]}
        path = self.root / "unsafe-mesh.json"
        raw = json.dumps(bad_mesh).encode();path.write_bytes(raw)
        descriptor = {"bytes": len(raw), "sha256": _sha(raw), "type": "mesh_json", "role": "surface_export", "geometry": self.geometry}
        with self.assertRaises(ProtocolError):
            validate_output_file(path, descriptor, self.geometry, "mesh_stage", "mesh_lungs")


class EnrollmentAndTlsTests(CoordinatorFixture):
    def test_pairing_csr_approval_signer_separation_replay_and_tls_modes(self):
        ca_key, ca_cert = _make_ca()
        ca_pem = ca_cert.public_bytes(serialization.Encoding.PEM)
        ca_key_pem = ca_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        token = self.coordinator.create_pairing_token()["pairing_token"]
        _node_key, csr_bytes = _make_csr()
        csr = csr_bytes.decode("ascii")
        capabilities = {"engine": {"id": self.engine_id, "sha256": self.engine_sha},
                        "models": [], "resources": {"threads": 4, "memory_bytes": 1024 * 1024 * 1024, "cpu_percent": 50},
                        "platform": {"os": "windows", "arch": "x86_64", "version": "synthetic"}, "state": "ready"}
        enrollment = self.coordinator.enrollment.submit({"pairing_token": token, "csr_pem": csr, "display_name": "Synthetic worker", "capabilities": capabilities})
        with self.assertRaises(ProtocolError):
            self.coordinator.enrollment.submit({"pairing_token": token, "csr_pem": csr, "display_name": "Synthetic worker", "capabilities": capabilities})
        pending = self.coordinator.approve_enrollment(enrollment["enrollment_id"])
        self.assertEqual(pending["state"], "pending_signing")
        listed = next(row for row in self.coordinator.list_enrollments() if row["enrollment_id"] == enrollment["enrollment_id"])
        self.assertEqual(listed["state"], "pending_signing")
        self.assertTrue(listed["approval_pending"])
        signer = Coordinator(self.root / "cluster.sqlite3", self.root / "cluster-artifacts", ca_cert_pem=ca_pem, ca_key_pem=ca_key_pem)
        self.coordinators.append(signer)
        self.assertEqual(signer.process_approvals(), 1)
        poll = self.coordinator.enrollment.poll({"enrollment_id": enrollment["enrollment_id"], "poll_token": enrollment["poll_token"]})
        self.assertEqual(poll["state"], "approved")
        issued = x509.load_pem_x509_certificate(poll["certificate_pem"].encode())
        self.assertEqual(issued.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value, pending["node_id"])
        self.assertEqual(issued.public_key().key_size, 3072)
        self.assertIn(ExtendedKeyUsageOID.CLIENT_AUTH, issued.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value)
        with self.assertRaises(ProtocolError):
            self.coordinator.enrollment.poll({"enrollment_id": enrollment["enrollment_id"], "poll_token": "0" * 64})
        temp = self.root / "tls"
        temp.mkdir()
        cert_path, key_path, ca_path = temp / "server.pem", temp / "server.key", temp / "ca.pem"
        cert_path.write_bytes(ca_pem);key_path.write_bytes(ca_key_pem);ca_path.write_bytes(ca_pem)
        device_context = create_device_server_context(str(cert_path), str(key_path), str(ca_path))
        enrollment_context = create_enrollment_server_context(str(cert_path), str(key_path))
        self.assertEqual(device_context.verify_mode, 2)  # ssl.CERT_REQUIRED
        self.assertEqual(enrollment_context.verify_mode, 0)  # ssl.CERT_NONE
        self.assertGreaterEqual(int(device_context.minimum_version), 771)  # TLS 1.2

    def test_certificate_rotation_preserves_node_id_and_retires_old_der(self):
        ca_key, ca_cert = _make_ca()
        ca_pem = ca_cert.public_bytes(serialization.Encoding.PEM)
        ca_key_pem = ca_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        caps = {"engine": {"id": self.engine_id, "sha256": self.engine_sha}, "models": [],
                "resources": {"threads": 4, "memory_bytes": 1024 * 1024 * 1024, "cpu_percent": 50},
                "platform": {"os": "windows", "arch": "x86_64", "version": "synthetic"}, "state": "ready"}

        _, first_csr = _make_csr()
        initial = self.coordinator.enrollment.submit({"pairing_token": self.coordinator.create_pairing_token()["pairing_token"],
                                                      "csr_pem": first_csr.decode("ascii"), "display_name": "Synthetic worker", "capabilities": caps})
        approved = self.coordinator.approve_enrollment(initial["enrollment_id"])
        node_id = approved["node_id"]
        signer = Coordinator(self.root / "cluster.sqlite3", self.root / "cluster-artifacts", ca_cert_pem=ca_pem, ca_key_pem=ca_key_pem)
        self.coordinators.append(signer)
        self.assertEqual(signer.process_approvals(), 1)
        first = self.coordinator.enrollment.poll({"enrollment_id": initial["enrollment_id"], "poll_token": initial["poll_token"]})
        old_der = x509.load_pem_x509_certificate(first["certificate_pem"].encode()).public_bytes(serialization.Encoding.DER)
        instance = uuid.uuid4().hex

        _, next_csr = _make_csr()
        rotate_body = {"instance_id": instance, "csr_pem": next_csr.decode("ascii"), "capabilities": caps}
        response = self.coordinator.handle_device_request("POST", API_PREFIX + "/node/rotate", json.dumps(rotate_body).encode(), old_der,
                                                          {"Content-Type": "application/json"})
        self.assertEqual(response.status, 200)
        rotation = json.loads(response.body)
        listed = next(row for row in self.coordinator.list_enrollments() if row["enrollment_id"] == rotation["enrollment_id"])
        self.assertEqual(listed["purpose"], "rotation")
        self.assertEqual(listed["node_id"], node_id)
        denied = self.coordinator.handle_device_request("POST", API_PREFIX + "/node/rotate", json.dumps(rotate_body).encode(), old_der,
                                                        {"Content-Type": "application/json"})
        self.assertEqual(denied.status, 409)  # an unresolved request cannot create a second rotation
        self.assertEqual(self.coordinator.approve_enrollment(rotation["enrollment_id"])["state"], "pending_signing")
        old_heartbeat = self.coordinator.handle_device_request("POST", API_PREFIX + "/node/heartbeat", json.dumps({"instance_id": instance, "capabilities": caps, "state": "ready"}).encode(), old_der,
                                                               {"Content-Type": "application/json"})
        self.assertEqual(old_heartbeat.status, 401)
        self.assertEqual(signer.process_approvals(), 1)
        replacement = self.coordinator.enrollment.poll({"enrollment_id": rotation["enrollment_id"], "poll_token": rotation["poll_token"]})
        new_der = x509.load_pem_x509_certificate(replacement["certificate_pem"].encode()).public_bytes(serialization.Encoding.DER)
        self.assertNotEqual(_sha(old_der), _sha(new_der))
        self.assertEqual(replacement["node_id"], node_id)
        new_heartbeat = self.coordinator.handle_device_request("POST", API_PREFIX + "/node/heartbeat", json.dumps({"instance_id": instance, "capabilities": caps, "state": "ready"}).encode(), new_der,
                                                               {"Content-Type": "application/json"})
        self.assertEqual(new_heartbeat.status, 200)

    def test_signed_resource_manifest_digest_range_and_tamper_rejection(self):
        signer = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        public_pem = signer.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        package_root = self.root / "package"
        package_root.mkdir()
        package_file = package_root / "engine.bin"
        file_bytes = b"synthetic signed engine bytes"
        package_file.write_bytes(file_bytes)
        now = datetime.now(timezone.utc)
        engine_name = "Dataset298_Synthetic_localizer"
        model_path = f"models/{engine_name}/weights.bin"
        model_bytes = b"synthetic weights"
        model_file = package_root / model_path
        model_file.parent.mkdir(parents=True)
        model_file.write_bytes(model_bytes)
        file_rows = [
            {"path": "engine.bin", "bytes": len(file_bytes), "sha256": _sha(file_bytes), "role": "runner"},
            {"path": model_path, "bytes": len(model_bytes), "sha256": _sha(model_bytes), "role": "model"},
        ]
        file_rows.sort(key=lambda row: row["path"])
        model_id = hashlib.sha256(("totalsegmentator-model-v1:" + engine_name).encode()).hexdigest()[:32]
        model_hash_rows = [{"path": row["path"], "bytes": row["bytes"], "sha256": row["sha256"]} for row in file_rows if row["path"] == model_path]
        model = {"id": model_id, "name": engine_name, "sha256": hashlib.sha256(canonical_json(model_hash_rows)).hexdigest(), "files": [model_path]}
        model_set = hashlib.sha256(canonical_json([{"id": model_id, "sha256": model["sha256"], "files": [model_path]}])).hexdigest()
        manifest = {"schema": 1, "engine_id": self.engine_id, "engine_version": "1.0.0", "created_utc": now.isoformat().replace("+00:00", "Z"),
                    "expires_utc": (now + timedelta(days=2)).isoformat().replace("+00:00", "Z"), "platform": {"os": "windows", "arch": "x86_64"},
                    "runtime": {}, "canonical_source_bundle_sha256": _sha(b"source"), "model_set_sha256": model_set,
                    "total_bytes": len(file_bytes) + len(model_bytes), "max_expanded_bytes": len(file_bytes) + len(model_bytes),
                    "files": file_rows, "models": [model]}
        manifest_raw = canonical_json(manifest) + b"\n"
        signature = signer.sign(manifest_raw, padding.PKCS1v15(), hashes.SHA256())
        manifest_path, signature_path = self.root / "manifest.json", self.root / "manifest.sig"
        manifest_path.write_bytes(manifest_raw);signature_path.write_bytes(signature)
        entry = {"manifest": str(manifest_path), "signature": str(signature_path), "root": str(package_root), "public_key_pem": public_pem}
        catalog = ResourceCatalog({"engine": entry, "model": entry})
        response = catalog.response("model", "manifest")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, manifest_raw)
        ranged = catalog.response("engine", "file/" + _sha(file_bytes), "bytes=3-9")
        self.assertEqual(ranged.status, 206)
        self.assertEqual(b"".join(ranged.body.iter_chunks()), file_bytes[3:10])
        manifest_path.write_bytes(manifest_raw + b" ")
        with self.assertRaises(Exception):
            ResourceCatalog({"engine": entry})


if __name__ == "__main__":
    unittest.main()
