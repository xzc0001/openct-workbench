"""Transactional coordinator for leased, immutable reconstruction work."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import math
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Iterator

from .artifacts import (
    ARTIFACT_LIMITS,
    CORE_GEOMETRY,
    _validate_candidates,
    validate_artifact_descriptor,
    validate_output_file,
)
from .enrollment import EnrollmentService
from .protocol import (
    API_PREFIX,
    FileBody,
    INPUT_LIMITS,
    INPUT_TYPES,
    JSON_BODY_LIMIT,
    OUTPUT_TYPES,
    ProtocolError,
    Response,
    STAGE_RE,
    canonical_json,
    digest_json,
    normalize_path,
    require_id,
    require_int,
    require_sha256,
    strict_json_loads,
    validate_capabilities,
    validate_role,
)
from .resources import ResourceCatalog
from .store import Store
from .tls import certificate_fingerprint


@dataclass(frozen=True)
class Limits:
    max_threads: int = 1024
    max_memory_bytes: int = 2**63 - 1
    max_cpu_percent: int = 100
    max_inputs_per_job: int = 2048
    max_stages_per_job: int = 512
    max_artifacts_per_result: int = 256
    max_result_bytes: int = 1024 * 1024 * 1024
    storage_reserve_bytes: int = 16 * 1024 * 1024
    input_type_limits: dict[str, int] = field(default_factory=lambda: dict(INPUT_LIMITS))
    artifact_type_limits: dict[str, int] = field(default_factory=lambda: dict(ARTIFACT_LIMITS))


def _canonical_modules():
    root = Path(__file__).resolve().parents[1]
    # The deployed workbench is named app; the source tree calls it workbench.
    # If app exists, fail closed on a missing module instead of silently using
    # a second, potentially stale copy from a different directory.
    app = root / "app"
    base = app if os.path.lexists(app) else root / "workbench"
    loaded = []
    for module_name, filename in (("_cluster_canonical_structures", "structures.py"), ("_cluster_canonical_progressive", "progressive.py")):
        spec = importlib.util.spec_from_file_location(module_name, base / filename)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        loaded.append(module)
    return loaded[0], loaded[1]


def _geometry_normalized(value: dict) -> dict:
    if not isinstance(value, dict) or not CORE_GEOMETRY <= set(value):
        raise ProtocolError(400, "invalid_geometry")
    result = dict(value)
    dimensions = result["dimensions_xyz"]
    if not isinstance(dimensions, list) or len(dimensions) != 3 or any(isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 65536 for n in dimensions):
        raise ProtocolError(400, "invalid_geometry")
    for key, count in (("spacing_xyz_mm", 3), ("origin_lps_mm", 3), ("direction", 9)):
        values = result[key]
        if not isinstance(values, list) or len(values) != count or any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) for n in values):
            raise ProtocolError(400, "invalid_geometry")
    if any(n <= 0 for n in result["spacing_xyz_mm"]):
        raise ProtocolError(400, "invalid_geometry")
    return result


def compute_input_manifest_sha256(inputs: list[dict]) -> str:
    rows = []
    for item in inputs:
        rows.append({key: item[key] for key in ("id", "role", "type", "bytes", "sha256")})
    return digest_json(sorted(rows, key=lambda row: (row["role"], row["id"])))


def compute_model_set_sha256(models: list[dict]) -> str:
    normalized = []
    for item in models:
        row = {"id": item["id"], "sha256": item["sha256"]}
        if "files" in item:
            row["files"] = list(item["files"])
        normalized.append(row)
    return digest_json(sorted(normalized, key=lambda row: row["id"]))


def required_inference_model_ids(models: list[dict], task_key: str) -> list[dict]:
    dataset_by_task = {
        "total": "Dataset297",
        "heart_highres": "Dataset293",
        "aorta_highres": "Dataset293",
        "lung_highres": "Dataset291",
        "trachea_highres": "Dataset291",
        "ribs_highres": "Dataset295",
        "spine_highres": "Dataset292",
        "scapulae_highres": "Dataset294",
        "lung_vessels": "Dataset117",
        "lung_nodules": "Dataset913",
    }
    dataset = dataset_by_task.get(task_key)
    if not dataset:
        raise ProtocolError(400, "inference_model_plan_invalid")
    rows = []
    for dataset_name in ("Dataset298", dataset):
        matches = [row for row in models if isinstance(row.get("name"), str) and row["name"].startswith(dataset_name + "_")]
        if len(matches) != 1:
            raise ProtocolError(400, "inference_model_missing")
        row = matches[0]
        expected_id = hashlib.sha256(("totalsegmentator-model-v1:" + row["name"]).encode("utf-8")).hexdigest()[:32]
        if row["id"] != expected_id:
            raise ProtocolError(400, "inference_model_identity_invalid")
        rows.append({"id": row["id"], "sha256": row["sha256"]})
    return sorted(rows, key=lambda row: row["id"])


def compute_geometry_sha256(geometry: dict) -> str:
    return digest_json(geometry)


def _sha_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _is_linklike(path: Path) -> bool:
    junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(junction and junction())


def _assert_private_root(path: Path) -> Path:
    path = path.absolute()
    if any(part.casefold() in {"jobs", "cache"} for part in path.parts):
        raise ValueError("cluster artifacts must be outside jobs and cache")
    path.mkdir(parents=True, exist_ok=True)
    if any(part.exists() and _is_linklike(part) for part in (path, *path.parents)):
        raise ValueError("cluster artifact root cannot be a symlink")
    path = path.resolve()
    if os.name != "nt":
        os.chmod(path, 0o700)
    return path


def _safe_json_parameter(value: Any, depth: int = 0) -> None:
    if depth > 32:
        raise ProtocolError(400, "invalid_stage_parameters")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or key.casefold() in {"path", "filepath", "url", "script", "command", "argv", "executable", "patient", "patient_name", "patient_id", "sopinstanceuid", "studyinstanceuid"}:
                raise ProtocolError(400, "invalid_stage_parameters")
            _safe_json_parameter(item, depth + 1)
    elif isinstance(value, list):
        if len(value) > 8192:
            raise ProtocolError(400, "invalid_stage_parameters")
        for item in value:
            _safe_json_parameter(item, depth + 1)
    elif isinstance(value, str):
        if len(value) > 512 or any(ord(ch) < 32 for ch in value) or value.casefold().startswith(("http://", "https://", "file://", "javascript:")) or re.match(r"^(?:[a-z]:[\\/]|/|\\\\)", value):
            raise ProtocolError(400, "invalid_stage_parameters")
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ProtocolError(400, "invalid_stage_parameters")
    elif isinstance(value, int):
        if value < -(2**63) or value > 2**63 - 1:
            raise ProtocolError(400, "invalid_stage_parameters")
    elif value is not None and not isinstance(value, bool):
        raise ProtocolError(400, "invalid_stage_parameters")


class Coordinator:
    def __init__(self, db_path: str | Path, artifact_root: str | Path, *,
                 ca_cert_pem: bytes | str | None = None, ca_key_pem: bytes | str | None = None,
                 lease_seconds: int = 60, expiry_grace_seconds: int = 15,
                 heartbeat_seconds: int = 10, max_attempts: int = 3,
                 limits: Limits | None = None, resource_catalog: dict | None = None):
        self.store = Store(db_path)
        self.publisher_id = uuid.uuid4().hex
        self.artifact_root = _assert_private_root(Path(artifact_root))
        self.staging_root = self.artifact_root / ".staging"
        self.results_root = self.artifact_root / "results"
        self.staging_root.mkdir(exist_ok=True)
        self.results_root.mkdir(exist_ok=True)
        if os.name != "nt":
            os.chmod(self.staging_root, 0o700)
            os.chmod(self.results_root, 0o700)
        self.lease_seconds = require_int(lease_seconds, "lease_seconds", 10, 3600)
        self.expiry_grace_seconds = require_int(expiry_grace_seconds, "expiry_grace_seconds", 0, 300)
        self.heartbeat_seconds = require_int(heartbeat_seconds, "heartbeat_seconds", 1, 120)
        self.max_attempts = require_int(max_attempts, "max_attempts", 1, 10)
        self.limits = limits or Limits()
        require_int(self.limits.storage_reserve_bytes, "storage_reserve_bytes", 0, 2**63 - 1)
        self.enrollment = EnrollmentService(self.store, ca_cert_pem=ca_cert_pem, ca_key_pem=ca_key_pem)
        self.resources = ResourceCatalog(resource_catalog)
        self._upload_locks: dict[str, threading.Lock] = {}
        self._upload_locks_guard = threading.Lock()
        self._publisher_ids: set[str] = set()
        self._recover_on_start()

    def close(self):
        try:
            self.release_publisher()
        except sqlite3.Error:
            pass
        self.store.close()

    def create_pairing_token(self) -> dict:
        return self.enrollment.create_pairing_token()

    def approve_enrollment(self, enrollment_id: str, approved_by: str = "admin") -> dict:
        return self.enrollment.approve(enrollment_id, approved_by)

    def reject_enrollment(self, enrollment_id: str) -> None:
        self.enrollment.reject(enrollment_id)

    def process_approvals(self) -> int:
        """Only the separately protected enrollment signer should call this."""
        return self.enrollment.process_approvals()

    def list_enrollments(self) -> list[dict]:
        with self.store.read() as db:
            rows = db.execute("SELECT e.enrollment_id,e.display_name,e.capabilities_json,e.state,e.created_at,e.expires_at,e.node_id,e.purpose,a.state AS approval_state FROM enrollments e LEFT JOIN approval_requests a USING(enrollment_id) ORDER BY e.created_at DESC LIMIT 500").fetchall()
        return [{"enrollment_id": row["enrollment_id"], "display_name": row["display_name"], "capabilities": json.loads(row["capabilities_json"]), "state": "pending_signing" if row["state"] == "pending" and row["approval_state"] == "pending" else row["state"], "approval_pending": row["approval_state"] == "pending", "approval_state": row["approval_state"], "purpose": row["purpose"], "created_at": row["created_at"], "expires_at": row["expires_at"], "node_id": row["node_id"]} for row in rows]

    def request_certificate_rotation(self, cert_der: bytes, body: dict) -> dict:
        node = self._authenticate(cert_der)
        self.reap_expired()
        return self.enrollment.submit_rotation(node["node_id"], body)

    def list_nodes(self) -> list[dict]:
        now = time.time()
        with self.store.read() as db:
            rows = db.execute("SELECT n.node_id,n.display_name,n.capabilities_json,n.status,n.admin_enabled,n.created_at,n.approved_at,n.last_seen,n.certificate_not_after,n.revoked_at,EXISTS(SELECT 1 FROM leases l WHERE l.node_id=n.node_id AND l.state='active' AND l.grace_until>?) AS busy FROM nodes n ORDER BY n.created_at DESC", (now,)).fetchall()
        return [{"node_id": r["node_id"], "display_name": r["display_name"], "capabilities": json.loads(r["capabilities_json"]), "state": "revoked" if r["revoked_at"] else "disabled" if not r["admin_enabled"] else "busy" if r["busy"] else "offline" if not r["last_seen"] or now - r["last_seen"] > 3 * self.heartbeat_seconds else r["status"], "created_at": r["created_at"], "approved_at": r["approved_at"], "last_seen": r["last_seen"], "certificate_expires_at": r["certificate_not_after"], "revoked_at": r["revoked_at"]} for r in rows]

    def revoke_node(self, node_id: str) -> dict:
        require_id(node_id, "node_id")
        now = time.time()
        with self.store.transaction() as db:
            node = db.execute("SELECT node_id FROM nodes WHERE node_id=?", (node_id,)).fetchone()
            if not node:
                raise ProtocolError(404, "node_not_found")
            db.execute("UPDATE nodes SET status='revoked',admin_enabled=0,revoked_at=? WHERE node_id=?", (now, node_id))
            leases = db.execute("SELECT lease_id,job_id,job_revision,stage_key FROM leases WHERE node_id=? AND state='active'", (node_id,)).fetchall()
            for lease in leases:
                db.execute("UPDATE leases SET state='revoked' WHERE lease_id=?", (lease["lease_id"],))
                stage = db.execute("SELECT attempt,max_attempts FROM stages WHERE job_id=? AND revision=? AND stage_key=?", (lease["job_id"], lease["job_revision"], lease["stage_key"])).fetchone()
                if stage:
                    retryable = stage["attempt"] < stage["max_attempts"]
                    db.execute("UPDATE stages SET status=?,error_code='node_revoked',updated_at=? WHERE job_id=? AND revision=? AND stage_key=? AND status IN ('leased','running','uploading','validating')",
                               ("retryable" if retryable else "failed", now, lease["job_id"], lease["job_revision"], lease["stage_key"]))
                    if not retryable:
                        self._fail_job_locked(db, lease["job_id"], lease["job_revision"], "node_revoked", now)
                db.execute("UPDATE uploads SET state='rejected',error_code='node_revoked' WHERE lease_id=? AND state!='validated'", (lease["lease_id"],))
            db.execute("UPDATE nodes SET active_instance=NULL WHERE node_id=?", (node_id,))
        self._remove_pending_for_node(leases)
        return {"node_id": node_id, "revoked": True, "leases_invalidated": len(leases)}

    def set_node_enabled(self, node_id: str, enabled: bool) -> dict:
        require_id(node_id, "node_id")
        if not isinstance(enabled, bool):
            raise ProtocolError(400, "invalid_enabled")
        with self.store.transaction() as db:
            row = db.execute("SELECT status FROM nodes WHERE node_id=?", (node_id,)).fetchone()
            if not row:
                raise ProtocolError(404, "node_not_found")
            if row["status"] == "revoked":
                raise ProtocolError(409, "node_revoked")
            db.execute("UPDATE nodes SET admin_enabled=? WHERE node_id=?", (int(enabled), node_id))
            if not enabled:
                leases = db.execute("SELECT lease_id,job_id,job_revision,stage_key FROM leases WHERE node_id=? AND state='active'", (node_id,)).fetchall()
                for lease in leases:
                    db.execute("UPDATE leases SET state='cancelled' WHERE lease_id=?", (lease["lease_id"],))
                    stage = db.execute("SELECT attempt,max_attempts FROM stages WHERE job_id=? AND revision=? AND stage_key=?", (lease["job_id"], lease["job_revision"], lease["stage_key"])).fetchone()
                    retryable = bool(stage and stage["attempt"] < stage["max_attempts"])
                    now = time.time()
                    db.execute("UPDATE stages SET status=?,error_code='node_paused',updated_at=? WHERE job_id=? AND revision=? AND stage_key=? AND status IN ('leased','running','uploading','validating')", ("retryable" if retryable else "failed", now, lease["job_id"], lease["job_revision"], lease["stage_key"]))
                    if not retryable:
                        self._fail_job_locked(db, lease["job_id"], lease["job_revision"], "node_paused", now)
            else:
                leases = []
        return {"node_id": node_id, "enabled": enabled, "leases_invalidated": len(leases)}

    def update_settings(self, values: dict) -> dict:
        allowed = {"enabled", "local_fallback", "scheduling_policy", "node_allowances"}
        if not isinstance(values, dict) or set(values) - allowed:
            raise ProtocolError(400, "invalid_settings")
        if "enabled" in values and not isinstance(values["enabled"], bool):
            raise ProtocolError(400, "invalid_settings")
        if "local_fallback" in values and not isinstance(values["local_fallback"], bool):
            raise ProtocolError(400, "invalid_settings")
        if "scheduling_policy" in values and values["scheduling_policy"] not in {"balanced", "least_loaded", "oldest_first"}:
            raise ProtocolError(400, "invalid_settings")
        if "node_allowances" in values:
            allowances = values["node_allowances"]
            if not isinstance(allowances, dict) or len(allowances) > 10000:
                raise ProtocolError(400, "invalid_settings")
            clean = {}
            for node_id, cap in allowances.items():
                require_id(node_id, "node_id")
                if not isinstance(cap, dict) or set(cap) - {"threads", "memory_bytes", "cpu_percent"}:
                    raise ProtocolError(400, "invalid_settings")
                clean[node_id] = {key: require_int(value, key, 1, self.limits.max_threads if key == "threads" else self.limits.max_memory_bytes if key == "memory_bytes" else self.limits.max_cpu_percent) for key, value in cap.items()}
            values = {**values, "node_allowances": clean}
        now = time.time()
        with self.store.transaction() as db:
            for key, value in values.items():
                db.execute("INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at", (key, json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")), now))
        return self.get_settings()

    def get_settings(self) -> dict:
        with self.store.read() as db:
            return {row["key"]: json.loads(row["value_json"]) for row in db.execute("SELECT key,value_json FROM settings WHERE key NOT LIKE 'job_plan:%' AND key NOT LIKE 'lease_metrics:%' AND key!='publisher_owner'")}

    def enqueue_job(self, job_id: str, revision: int, *, mode: str, selected: list[str], stages: list[dict],
                    input_manifest: list[dict], geometry: dict, engine_id: str, engine_sha256: str,
                    model_ids: list[dict], model_set_sha256: str, input_sha256: str, geometry_sha256: str) -> dict:
        return self._enqueue_job(job_id, revision, mode=mode, selected=selected, stages=stages,
                                 input_manifest=input_manifest, geometry=geometry, engine_id=engine_id,
                                 engine_sha256=engine_sha256, model_ids=model_ids, model_set_sha256=model_set_sha256,
                                 input_sha256=input_sha256, geometry_sha256=geometry_sha256, revise=False)

    def revise_job(self, job_id: str, new_revision: int, **values) -> dict:
        return self._enqueue_job(job_id, new_revision, revise=True, **values)

    def _enqueue_job(self, job_id: str, revision: int, *, mode: str, selected: list[str], stages: list[dict],
                     input_manifest: list[dict], geometry: dict, engine_id: str, engine_sha256: str,
                     model_ids: list[dict], model_set_sha256: str, input_sha256: str, geometry_sha256: str,
                     revise: bool) -> dict:
        require_id(job_id, "job_id")
        require_int(revision, "job_revision", 1, 2**31 - 1)
        if mode not in {"quick", "full"} or not isinstance(selected, list) or not selected or len(selected) > 64 or any(not isinstance(item, str) for item in selected):
            raise ProtocolError(400, "invalid_job_plan")
        if len(set(selected)) != len(selected):
            raise ProtocolError(400, "invalid_job_plan")
        if not isinstance(engine_id, str) or not 1 <= len(engine_id) <= 80:
            raise ProtocolError(400, "invalid_engine_id")
        require_sha256(engine_sha256, "engine_sha256")
        require_sha256(model_set_sha256, "model_set_sha256")
        require_sha256(input_sha256, "input_sha256")
        require_sha256(geometry_sha256, "geometry_sha256")
        geometry = _geometry_normalized(geometry)
        actual_geometry_hash = compute_geometry_sha256(geometry)
        if actual_geometry_hash != geometry_sha256:
            raise ProtocolError(400, "geometry_hash_mismatch")
        if not isinstance(model_ids, list) or len(model_ids) > 512:
            raise ProtocolError(400, "invalid_model_manifest")
        normalized_models = []
        seen_models = set()
        for model in model_ids:
            if not isinstance(model, dict) or set(model) != {"id", "name", "sha256", "files"} or not isinstance(model["id"], str) or not 1 <= len(model["id"]) <= 128 or model["id"] in seen_models:
                raise ProtocolError(400, "invalid_model_manifest")
            if not isinstance(model["name"], str) or not re.fullmatch(r"Dataset\d+_[A-Za-z0-9_.-]+", model["name"]):
                raise ProtocolError(400, "invalid_model_manifest")
            expected_model_id = hashlib.sha256(("totalsegmentator-model-v1:" + model["name"]).encode("utf-8")).hexdigest()[:32]
            if model["id"] != expected_model_id:
                raise ProtocolError(400, "model_identity_mismatch")
            require_sha256(model["sha256"], "model sha256")
            files = model["files"]
            if not isinstance(files, list) or len(files) > 100000 or any(not isinstance(p, str) or not p or ".." in Path(p).parts or "\\" in p for p in files) or files != sorted(files):
                raise ProtocolError(400, "invalid_model_manifest")
            row = {"id": model["id"], "name": model["name"], "sha256": model["sha256"], "files": files}
            normalized_models.append(row)
            seen_models.add(row["id"])
        normalized_models.sort(key=lambda row: row["id"])
        if compute_model_set_sha256(normalized_models) != model_set_sha256:
            raise ProtocolError(400, "model_set_hash_mismatch")
        if not isinstance(input_manifest, list) or not 1 <= len(input_manifest) <= self.limits.max_inputs_per_job:
            raise ProtocolError(400, "invalid_input_manifest")
        inputs, input_ids, input_roles = [], set(), set()
        for item in input_manifest:
            allowed = {"id", "role", "type", "bytes", "sha256", "source_path", "geometry", "labels"}
            if not isinstance(item, dict) or set(item) - allowed or not {"id", "role", "type", "bytes", "sha256", "source_path"} <= set(item):
                raise ProtocolError(400, "invalid_input_manifest")
            item_id = require_id(item["id"], "input id")
            kind = item["type"]
            if kind not in INPUT_TYPES or kind not in self.limits.input_type_limits:
                raise ProtocolError(400, "input_type_forbidden")
            role = validate_role(item["role"], kind)
            byte_count = require_int(item["bytes"], "input bytes", 1, min(self.limits.input_type_limits[kind], self.limits.max_result_bytes))
            digest = require_sha256(item["sha256"], "input sha256")
            source_path = Path(item["source_path"]).absolute() if isinstance(item["source_path"], str) else None
            if source_path is None or _is_linklike(source_path) or not source_path.is_file():
                raise ProtocolError(400, "input_source_invalid")
            if source_path.stat().st_size != byte_count or _sha_file(source_path) != digest:
                raise ProtocolError(409, "input_source_changed")
            if item_id in input_ids or role in input_roles:
                raise ProtocolError(400, "duplicate_input")
            input_ids.add(item_id)
            input_roles.add(role)
            input_row = {"id": item_id, "role": role, "type": kind, "bytes": byte_count, "sha256": digest, "source_path": str(source_path)}
            if "geometry" in item:
                input_row["geometry"] = _geometry_normalized(item["geometry"])
            if "labels" in item:
                labels = item["labels"]
                if not isinstance(labels, list) or not labels or len(labels) > 256 or any(isinstance(x, bool) or not isinstance(x, int) or not 0 <= x <= 65535 for x in labels):
                    raise ProtocolError(400, "invalid_input_labels")
                input_row["labels"] = sorted(set(labels))
            inputs.append(input_row)
        if not {"ct", "geometry"} <= input_roles:
            raise ProtocolError(400, "required_inputs_missing")
        if compute_input_manifest_sha256(inputs) != input_sha256:
            raise ProtocolError(400, "input_manifest_hash_mismatch")
        self._validate_stages(stages, mode, selected, inputs, normalized_models, geometry)
        stages = sorted(stages, key=lambda row: row["stage_key"])
        plan_hash = digest_json({"mode": mode, "selected": selected, "stages": stages, "engine_id": engine_id, "engine_sha256": engine_sha256, "models": normalized_models, "model_set_sha256": model_set_sha256, "geometry_sha256": geometry_sha256, "input_sha256": input_sha256, "inputs": [{key: row[key] for key in ("id", "role", "type", "bytes", "sha256")} for row in inputs]})
        now = time.time()
        with self.store.transaction() as db:
            prior = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if prior and not revise:
                old_plan = db.execute("SELECT value_json FROM settings WHERE key=?", (f"job_plan:{job_id}",)).fetchone()
                if prior["revision"] == revision and prior["state"] in {"active", "completed"} and prior["input_sha256"] == input_sha256 and prior["geometry_sha256"] == geometry_sha256 and old_plan and json.loads(old_plan["value_json"]) == {"hash": plan_hash}:
                    return self.job_snapshot(job_id)
                if prior["state"] == "tombstoned" or revision <= prior["revision"]:
                    raise ProtocolError(409, "job_already_exists")
            if revise and (not prior or revision <= prior["revision"]):
                raise ProtocolError(409, "revision_not_advanced")
            if prior:
                db.execute("UPDATE leases SET state='stale' WHERE job_id=? AND state='active'", (job_id,))
                db.execute("UPDATE stages SET status='cancelled',error_code='revision_replaced',updated_at=? WHERE job_id=? AND revision=? AND status NOT IN ('completed','failed','cancelled')", (now, job_id, prior["revision"]))
                db.execute("UPDATE outbox SET state='discarded' WHERE job_id=? AND job_revision!=? AND state!='acked'", (job_id, revision))
                db.execute("UPDATE jobs SET revision=?,state='active',mode=?,selected_json=?,geometry_json=?,geometry_sha256=?,engine_id=?,engine_sha256=?,model_ids_json=?,model_set_sha256=?,input_sha256=?,updated_at=?,tombstoned_at=NULL WHERE job_id=?",
                           (revision, mode, json.dumps(selected, ensure_ascii=False), json.dumps(geometry, ensure_ascii=False, sort_keys=True), geometry_sha256, engine_id, engine_sha256, json.dumps(normalized_models, ensure_ascii=False, sort_keys=True), model_set_sha256, input_sha256, now, job_id))
            else:
                db.execute("INSERT INTO jobs(job_id,revision,state,mode,selected_json,geometry_json,geometry_sha256,engine_id,engine_sha256,model_ids_json,model_set_sha256,input_sha256,created_at,updated_at) VALUES(?,?,'active',?,?,?,?,?,?,?,?,?,?,?)",
                           (job_id, revision, mode, json.dumps(selected, ensure_ascii=False), json.dumps(geometry, ensure_ascii=False, sort_keys=True), geometry_sha256, engine_id, engine_sha256, json.dumps(normalized_models, ensure_ascii=False, sort_keys=True), model_set_sha256, input_sha256, now, now))
            for input_row in inputs:
                db.execute("INSERT INTO job_inputs(job_id,revision,input_id,role,type,source_path,bytes,sha256,geometry_json,labels_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (job_id, revision, input_row["id"], input_row["role"], input_row["type"], input_row["source_path"], input_row["bytes"], input_row["sha256"], json.dumps(input_row.get("geometry"), ensure_ascii=False, sort_keys=True) if "geometry" in input_row else None, json.dumps(input_row.get("labels")) if "labels" in input_row else None))
            for stage in stages:
                db.execute("INSERT INTO stages(job_id,revision,stage_key,action,mode,structures_json,parameters_json,resources_json,dependencies_json,input_ids_json,model_ids_json,status,attempt,fencing_generation,max_attempts,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,'pending',0,0,?,?)",
                           (job_id, revision, stage["stage_key"], stage["action"], mode, json.dumps(stage["structures"], ensure_ascii=False), json.dumps(stage["parameters"], ensure_ascii=False, sort_keys=True), json.dumps(stage["resources"], sort_keys=True), json.dumps(stage["dependencies"]), json.dumps(stage["input_ids"]), json.dumps(stage["model_ids"], sort_keys=True), self.max_attempts, now))
            db.execute("INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at", (f"job_plan:{job_id}", json.dumps({"hash": plan_hash}), now))
        return self.job_snapshot(job_id)

    def _validate_stages(self, stages, mode, selected, inputs, models, geometry):
        structures_mod, progressive_mod = _canonical_modules()
        if not isinstance(stages, list) or not 1 <= len(stages) <= self.limits.max_stages_per_job:
            raise ProtocolError(400, "invalid_stage_plan")
        selected_set = set(selected)
        if not selected_set <= structures_mod.SUPPORTED:
            raise ProtocolError(400, "invalid_job_structures")
        canonical_inference = {row[1]: row for row in structures_mod.inference_plan(selected, mode)}
        by_key, keys = {}, set()
        input_by_id = {row["id"]: row for row in inputs}
        model_by_id = {row["id"]: row for row in models}
        for original in stages:
            allowed = {"stage_key", "action", "mode", "structures", "parameters", "resources", "dependencies", "input_ids", "model_ids", "minimum_memory_bytes"}
            if not isinstance(original, dict) or set(original) - allowed or not {"stage_key", "action", "mode", "structures", "parameters", "resources", "dependencies"} <= set(original):
                raise ProtocolError(400, "invalid_stage_plan")
            stage = dict(original)
            key = stage["stage_key"]
            if not isinstance(key, str) or not STAGE_RE.fullmatch(key) or key in keys:
                raise ProtocolError(400, "invalid_stage_key")
            action = stage["action"]
            if action not in {"infer_stage", "mesh_stage", "candidate_stage"} or stage["mode"] != mode:
                raise ProtocolError(400, "invalid_stage_action")
            rows = stage["structures"]
            if not isinstance(rows, list) or any(not isinstance(x, str) or x not in selected_set for x in rows) or len(rows) > 64:
                raise ProtocolError(400, "invalid_stage_structures")
            params = stage["parameters"]
            if not isinstance(params, dict):
                raise ProtocolError(400, "invalid_stage_parameters")
            _safe_json_parameter(params)
            resources = stage["resources"]
            if not isinstance(resources, dict) or set(resources) - {"threads", "memory_bytes", "cpu_percent", "minimum_memory_bytes"} or not {"threads", "memory_bytes", "cpu_percent"} <= set(resources):
                raise ProtocolError(400, "invalid_stage_resources")
            resources = {"threads": require_int(resources["threads"], "stage threads", 1, self.limits.max_threads), "memory_bytes": require_int(resources["memory_bytes"], "stage memory", 1, self.limits.max_memory_bytes), "cpu_percent": require_int(resources["cpu_percent"], "stage cpu", 1, self.limits.max_cpu_percent)}
            min_memory = stage.get("minimum_memory_bytes", resources.get("minimum_memory_bytes", resources["memory_bytes"]))
            resources["minimum_memory_bytes"] = require_int(min_memory, "minimum_memory_bytes", 1, resources["memory_bytes"])
            dependencies = stage.get("dependencies", [])
            if not isinstance(dependencies, list) or len(dependencies) > self.limits.max_stages_per_job or any(not isinstance(x, str) or not STAGE_RE.fullmatch(x) or x == key for x in dependencies) or len(set(dependencies)) != len(dependencies):
                raise ProtocolError(400, "invalid_stage_dependencies")
            input_ids = stage.get("input_ids", [])
            if not isinstance(input_ids, list) or len(input_ids) > self.limits.max_inputs_per_job or any(not isinstance(x, str) or x not in input_by_id for x in input_ids) or len(set(input_ids)) != len(input_ids):
                raise ProtocolError(400, "invalid_stage_inputs")
            if not input_ids:
                required_roles = {"ct", "geometry"}
                if action == "mesh_stage" and rows == ["ribs"]:
                    required_roles.add("native_ct")
                input_ids = [row["id"] for row in inputs if row["role"] in required_roles]
            direct_roles = {input_by_id[item_id]["role"] for item_id in input_ids}
            allowed_roles = {"ct", "geometry"}
            if action == "mesh_stage" and rows == ["ribs"]:
                allowed_roles.add("native_ct")
            if action == "candidate_stage":
                allowed_roles.add("candidate_review")
                direct_roles = {role for role in direct_roles if not role.startswith("candidate_mask.")}
                if not {"ct", "geometry"} <= direct_roles or direct_roles - allowed_roles:
                    raise ProtocolError(400, "stage_input_roles_invalid")
            elif not {"ct", "geometry"} <= direct_roles or direct_roles - allowed_roles or (action == "mesh_stage" and rows == ["ribs"] and "native_ct" not in direct_roles):
                raise ProtocolError(400, "stage_input_roles_invalid")
            stage_models = stage.get("model_ids", [])
            if not isinstance(stage_models, list) or len(stage_models) > 512:
                raise ProtocolError(400, "invalid_stage_models")
            norm_stage_models = []
            for model in stage_models:
                if not isinstance(model, dict) or set(model) != {"id", "sha256"} or model["id"] not in model_by_id or model_by_id[model["id"]]["sha256"] != model["sha256"]:
                    raise ProtocolError(400, "stage_model_not_pinned")
                norm_stage_models.append({"id": model["id"], "sha256": model["sha256"]})
            if len({x["id"] for x in norm_stage_models}) != len(norm_stage_models):
                raise ProtocolError(400, "invalid_stage_models")
            if action == "infer_stage":
                allowed_inference_parameters = ({"task_key": key}, {"task_key": key, "thread_policy": "node_budget"})
                if key not in canonical_inference or params not in allowed_inference_parameters:
                    raise ProtocolError(400, "inference_not_canonical")
                expected_models = required_inference_model_ids(models, key)
                if norm_stage_models != expected_models:
                    raise ProtocolError(400, "inference_models_not_canonical")
                if dependencies:
                    raise ProtocolError(400, "inference_dependencies_invalid")
            elif action == "mesh_stage":
                if not rows:
                    raise ProtocolError(400, "mesh_stage_not_canonical")
                primary = rows[0]
                expected_rows = [primary] + (["margin"] if primary == "nodules" and "margin" in selected_set else [])
                if rows != expected_rows or key != "mesh_" + primary:
                    raise ProtocolError(400, "mesh_stage_not_canonical")
                required = progressive_mod.structure_dependencies(mode).get(primary)
                if not required or not required <= set(dependencies):
                    raise ProtocolError(400, "mesh_dependencies_invalid")
                if set(dependencies) - set(canonical_inference) - {"mesh_" + item for item in selected}:
                    raise ProtocolError(400, "mesh_dependencies_invalid")
            else:
                if norm_stage_models:
                    raise ProtocolError(400, "non_inference_models_forbidden")
                if key != "candidate_suspects" or "suspects" not in selected_set:
                    raise ProtocolError(400, "candidate_stage_not_canonical")
                allowed_candidate_param_sets = ({"candidate_parameters", "manual_points_lps", "thread_policy"}, {"candidate_parameters", "manual_points_lps", "thread_policy", "selected_candidate_ids"})
                if set(params) not in allowed_candidate_param_sets or params["thread_policy"] != "node_budget":
                    raise ProtocolError(400, "candidate_parameters_not_canonical")
                params.setdefault("selected_candidate_ids", [])
                try:
                    structures_mod.validate_candidate_parameters(params["candidate_parameters"])
                except (TypeError, ValueError):
                    raise ProtocolError(400, "candidate_parameters_not_canonical") from None
                selected_candidate_ids = params["selected_candidate_ids"]
                if not isinstance(selected_candidate_ids, list) or len(selected_candidate_ids) > 40 or any(not isinstance(item, str) or not re.fullmatch(r"suspect_[0-9]{3,6}", item) for item in selected_candidate_ids) or len(set(selected_candidate_ids)) != len(selected_candidate_ids):
                    raise ProtocolError(400, "candidate_selection_invalid")
                points = params["manual_points_lps"]
                if not isinstance(points, list) or len(points) > 40:
                    raise ProtocolError(400, "candidate_points_invalid")
                for point in points:
                    if not isinstance(point, list) or len(point) != 3 or any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in point):
                        raise ProtocolError(400, "candidate_points_invalid")
                    delta = [float(point[axis]) - float(geometry["origin_lps_mm"][axis]) for axis in range(3)]
                    index = [sum(float(geometry["direction"][row * 3 + axis]) * delta[row] for row in range(3)) / float(geometry["spacing_xyz_mm"][axis]) for axis in range(3)]
                    if any(index[axis] < -0.5 or index[axis] > geometry["dimensions_xyz"][axis] - 0.5 for axis in range(3)):
                        raise ProtocolError(400, "candidate_points_outside_ct")
                candidate_review = next((row for row in inputs if row["role"] == "candidate_review"), None)
                candidate_masks = {row["role"]: row for row in inputs if row["role"].startswith("candidate_mask.")}
                if selected_candidate_ids and not candidate_review:
                    raise ProtocolError(400, "candidate_review_required")
                if candidate_review:
                    ct_sha = next(row["sha256"] for row in inputs if row["role"] == "ct")
                    if candidate_review.get("type") != "candidates_json" or candidate_review.get("geometry") not in (None, geometry):
                        raise ProtocolError(400, "candidate_review_invalid")
                    try:
                        review_bytes = Path(candidate_review["source_path"]).read_bytes()
                        review_value = _validate_candidates(review_bytes)
                    except (OSError, ProtocolError):
                        raise ProtocolError(400, "candidate_review_invalid") from None
                    if review_value.get("source_ct_sha256") != ct_sha:
                        raise ProtocolError(400, "candidate_review_source_mismatch")
                    review_rows = review_value.get("candidates", review_value.get("listed_largest_candidates", []))
                    rows_by_id = {row["id"]: row for row in review_rows if isinstance(row, dict) and isinstance(row.get("id"), str)}
                    if not set(selected_candidate_ids) <= set(rows_by_id):
                        raise ProtocolError(400, "candidate_selection_not_in_review")
                    expected_mask_roles = set()
                    for candidate_id in selected_candidate_ids:
                        mask_role = rows_by_id[candidate_id].get("mask_role")
                        if not isinstance(mask_role, str) or not mask_role.startswith("candidate_mask."):
                            raise ProtocolError(400, "candidate_mask_missing")
                        validate_role(mask_role, "nifti_mask")
                        expected_mask_roles.add(mask_role)
                    if set(candidate_masks) != expected_mask_roles:
                        raise ProtocolError(400, "candidate_mask_manifest_mismatch")
                    for role, mask_input in candidate_masks.items():
                        descriptor = {"id": mask_input["id"], "type": mask_input["type"], "role": role, "bytes": mask_input["bytes"], "sha256": mask_input["sha256"], "geometry": mask_input.get("geometry"), "labels": mask_input.get("labels", [])}
                        try:
                            validate_output_file(mask_input["source_path"], descriptor, geometry, "candidate_stage", key)
                        except (OSError, ProtocolError):
                            raise ProtocolError(400, "candidate_mask_invalid") from None
                elif candidate_masks:
                    raise ProtocolError(400, "candidate_review_required")
                required = {task_key for task_key, task in canonical_inference.items() if task_key == "lung_vessels" or set(task[2].get("roi_subset", [])) & set(structures_mod.LOBE_NAMES)}
                required.update(stage_key for stage_key, row in by_key.items() if row["action"] == "mesh_stage")
                if required - set(dependencies):
                    raise ProtocolError(400, "candidate_dependencies_invalid")
            stage.update(resources=resources, dependencies=dependencies, input_ids=input_ids, model_ids=norm_stage_models)
            by_key[key] = stage
            keys.add(key)
        if set().union(*(set(stage["dependencies"]) for stage in by_key.values())) - keys:
            raise ProtocolError(400, "stage_dependency_missing")
        # A topological walk rejects cycles before any scheduler state is written.
        visiting, visited = set(), set()
        def visit(key):
            if key in visiting:
                raise ProtocolError(400, "stage_dependency_cycle")
            if key in visited:
                return
            visiting.add(key)
            for dependency in by_key[key]["dependencies"]:
                visit(dependency)
            visiting.remove(key)
            visited.add(key)
        for key in keys:
            visit(key)
        for task_key, task in canonical_inference.items():
            if task_key not in by_key or by_key[task_key]["action"] != "infer_stage":
                raise ProtocolError(400, "canonical_inference_missing")
        for structure in selected:
            if structure == "suspects":
                if "candidate_suspects" not in by_key:
                    raise ProtocolError(400, "candidate_stage_missing")
            elif structure not in {"segments", "fractures", "margin"}:
                mesh_key = "mesh_" + structure
                if mesh_key not in by_key or by_key[mesh_key]["action"] != "mesh_stage":
                    raise ProtocolError(400, "mesh_stage_missing")
        if isinstance(stages, list):
            stages[:] = [by_key[key] for key in by_key]

    def active_job_ids(self) -> list[str]:
        with self.store.read() as db:
            return [row[0] for row in db.execute("""SELECT j.job_id FROM jobs j WHERE j.state='active'
                OR (j.state IN ('completed','failed') AND EXISTS(SELECT 1 FROM outbox o
                    WHERE o.job_id=j.job_id AND o.job_revision=j.revision AND o.state IN ('pending','claimed')))
                ORDER BY j.created_at""")]

    def is_current(self, job_id: str, revision: int) -> bool:
        with self.store.read() as db:
            row = db.execute("SELECT revision,state FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return bool(row and row["revision"] == revision and row["state"] in {"active", "completed"})

    def publication_guard(self, event_id: str) -> bool:
        require_id(event_id, "event_id")
        with self.store.read() as db:
            row = db.execute("SELECT o.job_id,o.job_revision,o.state,j.revision,j.state AS job_state FROM outbox o JOIN jobs j USING(job_id) WHERE o.event_id=?", (event_id,)).fetchone()
        return bool(row and row["state"] in {"pending", "claimed"} and row["job_revision"] == row["revision"] and row["job_state"] in {"active", "completed", "failed"})

    def job_snapshot(self, job_id: str) -> dict:
        require_id(job_id, "job_id")
        with self.store.read() as db:
            job = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not job:
                raise ProtocolError(404, "job_not_found")
            stages = db.execute("SELECT * FROM stages WHERE job_id=? AND revision=? ORDER BY stage_key", (job_id, job["revision"])).fetchall()
            artifacts = db.execute("SELECT a.* FROM stage_artifacts a JOIN stages s ON s.job_id=a.job_id AND s.revision=a.revision AND s.stage_key=a.stage_key WHERE a.job_id=? AND a.revision=? AND a.stage_attempt=s.attempt ORDER BY a.stage_key,a.artifact_id", (job_id, job["revision"])).fetchall()
            leases = db.execute("SELECT lease_id,stage_key,stage_attempt,node_id,state,expires_at,progress_percent,progress_message FROM leases WHERE job_id=? AND job_revision=? ORDER BY issued_at DESC", (job_id, job["revision"])).fetchall()
        artifact_map = {}
        for artifact in artifacts:
            artifact_map.setdefault(artifact["stage_key"], []).append(self._artifact_public(artifact))
        state = job["state"]
        stage_rows = []
        for stage in stages:
            stage_rows.append({"stage_key": stage["stage_key"], "action": stage["action"], "structures": json.loads(stage["structures_json"]), "status": stage["status"], "attempt": stage["attempt"], "max_attempts": stage["max_attempts"], "dependencies": json.loads(stage["dependencies_json"]), "error_code": stage["error_code"], "artifacts": artifact_map.get(stage["stage_key"], [])})
        return {"job_id": job["job_id"], "revision": job["revision"], "job_revision": job["revision"], "state": state, "mode": job["mode"], "selected": json.loads(job["selected_json"]), "geometry": json.loads(job["geometry_json"]), "geometry_sha256": job["geometry_sha256"], "engine_id": job["engine_id"], "engine_sha256": job["engine_sha256"], "model_set_sha256": job["model_set_sha256"], "input_sha256": job["input_sha256"], "stages": stage_rows, "leases": [{"lease_id": row["lease_id"], "stage_key": row["stage_key"], "stage_attempt": row["stage_attempt"], "node_id": row["node_id"], "state": row["state"], "expires_at": row["expires_at"], "progress_percent": row["progress_percent"], "progress_message": row["progress_message"]} for row in leases]}

    def cancel_job(self, job_id: str) -> dict:
        return self._invalidate_job(job_id, "cancelled")

    def _fail_job_locked(self, db, job_id: str, revision: int, error_code: str, now: float):
        db.execute("UPDATE jobs SET state='failed',updated_at=? WHERE job_id=? AND revision=? AND state='active'", (now, job_id, revision))
        db.execute("UPDATE leases SET state='cancelled' WHERE job_id=? AND job_revision=? AND state='active'", (job_id, revision))
        db.execute("UPDATE stages SET status='cancelled',error_code=?,updated_at=? WHERE job_id=? AND revision=? AND status IN ('pending','ready','leased','running','uploading','validating','retryable')", (error_code, now, job_id, revision))
        db.execute("UPDATE uploads SET state='rejected',error_code=? WHERE lease_id IN (SELECT lease_id FROM leases WHERE job_id=? AND job_revision=?) AND state='pending'", (error_code, job_id, revision))

    def tombstone_job(self, job_id: str) -> dict:
        result = self._invalidate_job(job_id, "tombstoned")
        # Tombstone commits before any file removal, so late requests cannot recreate data.
        root = self.results_root / job_id
        self._remove_tree_if_safe(root)
        return result

    def _invalidate_job(self, job_id, state):
        require_id(job_id, "job_id")
        now = time.time()
        with self.store.transaction() as db:
            row = db.execute("SELECT revision,state FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row:
                raise ProtocolError(404, "job_not_found")
            if row["state"] == "tombstoned":
                return {"job_id": job_id, "state": "tombstoned", "leases_invalidated": 0}
            if row["state"] == "cancelled" and state == "cancelled":
                return {"job_id": job_id, "state": state, "leases_invalidated": 0}
            leases = db.execute("SELECT lease_id FROM leases WHERE job_id=? AND job_revision=? AND state='active'", (job_id, row["revision"])).fetchall()
            db.execute("UPDATE jobs SET state=?,updated_at=?,tombstoned_at=? WHERE job_id=?", (state, now, now if state == "tombstoned" else None, job_id))
            db.execute("UPDATE leases SET state=? WHERE job_id=? AND job_revision=? AND state='active'", (state, job_id, row["revision"]))
            db.execute("UPDATE stages SET status='cancelled',error_code=?,updated_at=? WHERE job_id=? AND revision=? AND status NOT IN ('completed','failed','cancelled')", (state, now, job_id, row["revision"]))
            db.execute("UPDATE outbox SET state='discarded' WHERE job_id=? AND state!='acked'", (job_id,))
            db.execute("UPDATE uploads SET state='rejected',error_code=? WHERE lease_id IN (SELECT lease_id FROM leases WHERE job_id=?) AND state!='validated'", (state, job_id))
        self._remove_pending_for_leases([row["lease_id"] for row in leases])
        return {"job_id": job_id, "state": state, "leases_invalidated": len(leases)}

    def _remove_tree_if_safe(self, target: Path):
        if not target.exists():
            return
        resolved = target.resolve()
        if _is_linklike(target) or not resolved.is_relative_to(self.results_root):
            raise RuntimeError("refusing to remove an unsafe cluster artifact path")
        shutil.rmtree(resolved)

    def heartbeat(self, cert_der: bytes, body: dict) -> dict:
        node = self._authenticate(cert_der)
        if not isinstance(body, dict) or set(body) - {"instance_id", "capabilities", "state", "lease_id"} or not {"instance_id", "capabilities", "state"} <= set(body):
            raise ProtocolError(400, "invalid_heartbeat")
        instance_id = require_id(body["instance_id"], "instance_id")
        capabilities = validate_capabilities(body["capabilities"])
        state = body["state"]
        if state not in {"ready", "busy", "paused", "draining", "unsupported", "error", "offline"}:
            raise ProtocolError(400, "invalid_node_state")
        lease_id = body.get("lease_id")
        if lease_id is not None:
            require_id(lease_id, "lease_id")
        now = time.time()
        with self.store.transaction() as db:
            self._reap_locked(db, now)
            if "lan_principal" in node:
                self.lan_sessions.touch_locked(db, node["lan_principal"], now)
            current = db.execute("SELECT * FROM nodes WHERE node_id=? AND certificate_fingerprint=?", (node["node_id"], node["fingerprint"])).fetchone()
            if not current or current["status"] == "revoked" or not current["admin_enabled"]:
                raise ProtocolError(403, "node_disabled")
            active = db.execute("SELECT * FROM leases WHERE node_id=? AND state='active'", (node["node_id"],)).fetchone()
            if active and (active["instance_id"] != instance_id or lease_id != active["lease_id"]):
                raise ProtocolError(409, "active_lease_instance_mismatch")
            if active and state not in {"busy", "ready"}:
                raise ProtocolError(409, "active_lease_state_mismatch")
            if not active and state == "busy":
                raise ProtocolError(400, "busy_without_lease")
            capabilities["state"] = "unsupported" if capabilities["state"] == "unsupported" else "ready" if state == "busy" else state
            status = capabilities["state"] if capabilities["state"] in {"ready", "paused", "draining", "unsupported", "error"} else "error"
            db.execute("UPDATE nodes SET capabilities_json=?,status=?,last_seen=?,active_instance=? WHERE node_id=?",
                       (json.dumps(capabilities, separators=(",", ":"), sort_keys=True), status, now, instance_id, node["node_id"]))
        return {"server_time": now, "heartbeat_seconds": self.heartbeat_seconds}

    def claim(self, cert_der: bytes, body: dict) -> dict:
        node = self._authenticate(cert_der)
        if not isinstance(body, dict) or set(body) != {"instance_id", "capabilities"}:
            raise ProtocolError(400, "invalid_claim")
        instance_id = require_id(body["instance_id"], "instance_id")
        caps = validate_capabilities(body["capabilities"])
        now = time.time()
        if caps["state"] != "ready":
            return {"task": None, "reason": "node_not_ready", "server_time": now}
        with self.store.transaction() as db:
            self._reap_locked(db, now)
            if "lan_principal" in node:
                self.lan_sessions.check_locked(db, node["lan_principal"], now)
            node_row = db.execute("SELECT * FROM nodes WHERE node_id=? AND certificate_fingerprint=?", (node["node_id"], node["fingerprint"])).fetchone()
            if not node_row or node_row["status"] == "revoked" or not node_row["admin_enabled"]:
                raise ProtocolError(403, "node_disabled")
            if node_row["status"] != "ready":
                return {"task": None, "reason": "node_not_ready", "server_time": now}
            active = db.execute("SELECT * FROM leases WHERE node_id=? AND state='active'", (node["node_id"],)).fetchone()
            if active:
                if active["instance_id"] != instance_id:
                    return {"task": None, "reason": "active_lease_other_instance", "server_time": now}
                if active["expires_at"] + self.expiry_grace_seconds < now:
                    return {"task": None, "reason": "lease_expiring", "server_time": now}
                return {"task": self._task_for_lease(db, active, node_row, caps, now), "reason": None, "server_time": now}
            db.execute("UPDATE nodes SET capabilities_json=?,last_seen=?,active_instance=? WHERE node_id=?",
                       (json.dumps(caps, sort_keys=True, separators=(",", ":")), now, instance_id, node["node_id"]))
            settings = {row["key"]: json.loads(row["value_json"]) for row in db.execute("SELECT key,value_json FROM settings WHERE key IN ('enabled','node_allowances','scheduling_policy')")}
            if settings.get("enabled", False) is False:
                return {"task": None, "reason": "cluster_disabled", "server_time": now}
            allowance = (settings.get("node_allowances") or {}).get(node["node_id"], {})
            rows = db.execute("SELECT s.*,j.mode,j.selected_json,j.geometry_json,j.geometry_sha256,j.engine_id,j.engine_sha256,j.model_ids_json,j.model_set_sha256,j.input_sha256,j.revision AS current_revision FROM stages s JOIN jobs j USING(job_id) WHERE j.state='active' AND s.revision=j.revision AND s.status IN ('pending','retryable') ORDER BY j.created_at,s.stage_key").fetchall()
            failed_jobs = set()
            for stage in rows:
                if stage["job_id"] in failed_jobs:
                    continue
                if stage["attempt"] >= stage["max_attempts"]:
                    db.execute("UPDATE stages SET status='failed',error_code='attempts_exhausted',updated_at=? WHERE job_id=? AND revision=? AND stage_key=?", (now, stage["job_id"], stage["revision"], stage["stage_key"]))
                    self._fail_job_locked(db, stage["job_id"], stage["revision"], "attempts_exhausted", now)
                    failed_jobs.add(stage["job_id"])
                    continue
                dependencies = json.loads(stage["dependencies_json"])
                if dependencies:
                    marks = ",".join("?" for _ in dependencies)
                    done = db.execute(f"SELECT stage_key FROM stages WHERE job_id=? AND revision=? AND status='completed' AND stage_key IN ({marks})", (stage["job_id"], stage["revision"], *dependencies)).fetchall()
                    if {row["stage_key"] for row in done} != set(dependencies):
                        continue
                effective = self._effective_resources(stage, caps["resources"], allowance)
                if effective is None or not self._node_matches(stage, caps, node_row, allowance):
                    continue
                lease_id = uuid.uuid4().hex
                attempt = stage["attempt"] + 1
                fence = stage["fencing_generation"] + 1
                lease_input_rows = self._stage_input_rows(db, stage)
                task_input_sha = compute_input_manifest_sha256([{key: row[key] for key in ("id", "role", "type", "bytes", "sha256")} for row in lease_input_rows])
                expires = now + self.lease_seconds
                grace_until = expires + self.expiry_grace_seconds
                db.execute("UPDATE stages SET status='leased',attempt=?,fencing_generation=?,error_code=NULL,updated_at=? WHERE job_id=? AND revision=? AND stage_key=?",
                           (attempt, fence, now, stage["job_id"], stage["revision"], stage["stage_key"]))
                db.execute("INSERT INTO leases(lease_id,job_id,job_revision,stage_key,stage_attempt,fencing_generation,node_id,instance_id,engine_id,engine_sha256,model_set_sha256,input_sha256,geometry_sha256,resources_json,issued_at,expires_at,grace_until,state,last_progress) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?)",
                           (lease_id, stage["job_id"], stage["revision"], stage["stage_key"], attempt, fence, node["node_id"], instance_id, stage["engine_id"], stage["engine_sha256"], stage["model_set_sha256"], task_input_sha, stage["geometry_sha256"], json.dumps(effective, sort_keys=True, separators=(",", ":")), now, expires, grace_until, now))
                lease = db.execute("SELECT * FROM leases WHERE lease_id=?", (lease_id,)).fetchone()
                job = db.execute("SELECT * FROM jobs WHERE job_id=?", (stage["job_id"],)).fetchone()
                task = self._task_for_lease(db, lease, node_row, caps, now, effective, job=job, stage=stage)
                return {"task": task, "reason": None, "server_time": now}
            return {"task": None, "reason": self._no_task_reason(db, node["node_id"], caps, now), "server_time": now}

    def _no_task_reason(self, db, node_id, caps, now):
        pending = db.execute("SELECT COUNT(*) FROM stages s JOIN jobs j USING(job_id) WHERE j.state='active' AND s.revision=j.revision AND s.status IN ('pending','retryable')").fetchone()[0]
        if pending == 0:
            return "queue_empty"
        ready = db.execute("SELECT COUNT(*) FROM stages s JOIN jobs j USING(job_id) WHERE j.state='active' AND s.revision=j.revision AND s.status IN ('pending','retryable') AND NOT EXISTS(SELECT 1 FROM json_each(s.dependencies_json) d JOIN stages dep ON dep.job_id=s.job_id AND dep.revision=s.revision AND dep.stage_key=d.value WHERE dep.status!='completed')").fetchone()[0]
        if ready == 0:
            return "waiting_for_dependencies"
        return "no_matching_resources"

    def _effective_resources(self, stage, available, allowance):
        requested = json.loads(stage["resources_json"])
        avail_threads = min(available["threads"], allowance.get("threads", available["threads"]))
        avail_mem = min(available["memory_bytes"], allowance.get("memory_bytes", available["memory_bytes"]))
        avail_cpu = min(available["cpu_percent"], allowance.get("cpu_percent", available["cpu_percent"]))
        params = json.loads(stage["parameters_json"])
        if params.get("thread_policy") == "node_budget":
            threads = max(1, avail_threads)
        else:
            threads = min(requested["threads"], avail_threads)
        memory = min(requested["memory_bytes"], avail_mem)
        if memory < requested["minimum_memory_bytes"] or threads < 1:
            return None
        return {"threads": threads, "memory_bytes": memory, "cpu_percent": min(requested["cpu_percent"], avail_cpu)}

    def _node_matches(self, stage, caps, node_row, allowance):
        if caps["state"] != "ready":
            return False
        if caps["engine"]["id"] != stage["engine_id"] or caps["engine"]["sha256"] != stage["engine_sha256"]:
            return False
        available_models = {(row["id"], row["sha256"]) for row in caps["models"]}
        required_models = {(row["id"], row["sha256"]) for row in json.loads(stage["model_ids_json"])}
        if not required_models <= available_models:
            return False
        resources = caps["resources"]
        budget = self._effective_resources(stage, resources, allowance)
        return budget is not None

    def _task_for_lease(self, db, lease, node_row, caps, now, effective=None, *, job=None, stage=None):
        if job is None:
            job = db.execute("SELECT * FROM jobs WHERE job_id=?", (lease["job_id"],)).fetchone()
        if stage is None:
            stage = db.execute("SELECT * FROM stages WHERE job_id=? AND revision=? AND stage_key=?", (lease["job_id"], lease["job_revision"], lease["stage_key"])).fetchone()
        if not job or not stage:
            raise ProtocolError(409, "lease_stale")
        effective = json.loads(lease["resources_json"])
        input_rows = self._stage_input_rows(db, stage)
        task_input_sha = compute_input_manifest_sha256([{key: row[key] for key in ("id", "role", "type", "bytes", "sha256")} for row in input_rows])
        if task_input_sha != lease["input_sha256"]:
            raise ProtocolError(409, "lease_inputs_changed")
        task = {
            "protocol_version": 1,
            "job_id": lease["job_id"],
            "job_revision": lease["job_revision"],
            "stage_key": lease["stage_key"],
            "stage_attempt": lease["stage_attempt"],
            "lease_id": lease["lease_id"],
            "fencing_generation": lease["fencing_generation"],
            "node_id": lease["node_id"],
            "engine_id": lease["engine_id"],
            "engine_sha256": lease["engine_sha256"],
            "model_set_sha256": lease["model_set_sha256"],
            "input_sha256": lease["input_sha256"],
            "geometry_sha256": lease["geometry_sha256"],
            "action": stage["action"],
            "mode": stage["mode"],
            "structures": json.loads(stage["structures_json"]),
            "parameters": json.loads(stage["parameters_json"]),
            "inputs": [{key: row[key] for key in ("id", "role", "type", "bytes", "sha256")} for row in input_rows],
            "geometry": json.loads(job["geometry_json"]),
            "resources": {key: effective[key] for key in ("threads", "memory_bytes", "cpu_percent")},
            "model_ids": json.loads(stage["model_ids_json"]),
            "lease_remaining_seconds": max(0, int(lease["expires_at"] - now)),
            "server_time": now,
        }
        return task

    def _stage_input_rows(self, db, stage):
        selected = set(json.loads(stage["input_ids_json"]))
        if not selected:
            selected = {row["input_id"] for row in db.execute("SELECT input_id FROM job_inputs WHERE job_id=? AND revision=?", (stage["job_id"], stage["revision"]))}
        rows = list(db.execute("SELECT input_id AS id,role,type,bytes,sha256,source_path,geometry_json,labels_json FROM job_inputs WHERE job_id=? AND revision=?", (stage["job_id"], stage["revision"])))
        result = []
        for row in rows:
            if row["id"] in selected:
                result.append({"id": row["id"], "role": row["role"], "type": row["type"], "bytes": row["bytes"], "sha256": row["sha256"], "source_path": row["source_path"], "geometry": json.loads(row["geometry_json"]) if row["geometry_json"] else None, "labels": json.loads(row["labels_json"]) if row["labels_json"] else None})
        dependencies = json.loads(stage["dependencies_json"])
        for dependency in dependencies:
            result.extend({**self._artifact_public(row), "source_path": row["path"]} for row in db.execute("SELECT a.* FROM stage_artifacts a JOIN stages s ON s.job_id=a.job_id AND s.revision=a.revision AND s.stage_key=a.stage_key WHERE a.job_id=? AND a.revision=? AND a.stage_key=? AND a.stage_attempt=s.attempt AND s.status='completed' ORDER BY a.artifact_id", (stage["job_id"], stage["revision"], dependency)))
        if selected - {row["id"] for row in result}:
            raise ProtocolError(409, "stage_input_missing")
        return sorted(result, key=lambda row: (row["role"], row["id"]))

    def _artifact_public(self, row):
        return {"id": row["artifact_id"], "role": row["role"], "type": row["type"], "bytes": row["bytes"], "sha256": row["sha256"], "path": row["path"], "geometry": json.loads(row["geometry_json"]), "labels": json.loads(row["labels_json"])}

    def renew(self, cert_der: bytes, body: dict) -> dict:
        node = self._authenticate(cert_der)
        binding = self._binding(body)
        now = time.time()
        with self.store.transaction() as db:
            lease = self._live_lease(db, node, binding, now)
            expires = now + self.lease_seconds
            db.execute("UPDATE leases SET expires_at=?,grace_until=?,last_progress=? WHERE lease_id=? AND state='active'", (expires, expires + self.expiry_grace_seconds, now, lease["lease_id"]))
        return {"lease_remaining_seconds": self.lease_seconds, "server_time": now}

    def progress(self, cert_der: bytes, body: dict) -> dict:
        node = self._authenticate(cert_der)
        if not isinstance(body, dict) or set(body) != {"binding", "percent", "message"}:
            raise ProtocolError(400, "invalid_progress")
        binding = self._binding({"binding": body["binding"]})
        percent = require_int(body["percent"], "percent", 0, 99)
        message = body["message"]
        if not isinstance(message, str) or len(message) > 120 or any(ord(ch) < 32 for ch in message) or re.search(r"[A-Za-z]:[\\/]|(?:^|\s)/(?:[^ ]+)|https?://", message, re.I):
            raise ProtocolError(400, "invalid_progress_message")
        if re.search(r"(?:patient|姓名|住院号|病历号|患者|患者号|uid)", message, re.I):
            raise ProtocolError(400, "invalid_progress_message")
        now = time.time()
        with self.store.transaction() as db:
            lease = self._live_lease(db, node, binding, now)
            db.execute("UPDATE leases SET last_progress=?,progress_percent=?,progress_message=? WHERE lease_id=?", (now, percent, message[:120], lease["lease_id"]))
            db.execute("UPDATE stages SET status='running',updated_at=? WHERE job_id=? AND revision=? AND stage_key=? AND status IN ('leased','running')", (now, lease["job_id"], lease["job_revision"], lease["stage_key"]))
        return {"server_time": now}

    def release(self, cert_der: bytes, body: dict) -> dict:
        node = self._authenticate(cert_der)
        if not isinstance(body, dict) or set(body) - {"binding", "retryable", "error_code"} or not {"binding", "retryable"} <= set(body) or not isinstance(body["retryable"], bool):
            raise ProtocolError(400, "invalid_release")
        binding = self._binding({"binding": body["binding"]})
        error_code = body.get("error_code", "worker_released")
        allowed_errors = {"storage_unavailable", "resource_exhausted", "cancelled", "input_transfer_failed", "engine_unavailable", "output_invalid", "runner_failed", "upload_failed", "task_failed", "worker_released"}
        if not isinstance(error_code, str) or error_code not in allowed_errors:
            raise ProtocolError(400, "invalid_error_code")
        now = time.time()
        with self.store.transaction() as db:
            lease = self._live_lease(db, node, binding, now)
            stage = db.execute("SELECT attempt,max_attempts FROM stages WHERE job_id=? AND revision=? AND stage_key=?", (lease["job_id"], lease["job_revision"], lease["stage_key"])).fetchone()
            retryable = body["retryable"] and stage["attempt"] < stage["max_attempts"]
            db.execute("UPDATE leases SET state='released' WHERE lease_id=?", (lease["lease_id"],))
            db.execute("UPDATE stages SET status=?,error_code=?,updated_at=? WHERE job_id=? AND revision=? AND stage_key=?", ("retryable" if retryable else "failed", error_code, now, lease["job_id"], lease["job_revision"], lease["stage_key"]))
            db.execute("UPDATE uploads SET state='rejected',error_code=? WHERE lease_id=? AND state!='validated'", (error_code, lease["lease_id"]))
            if not retryable:
                self._fail_job_locked(db, lease["job_id"], lease["job_revision"], error_code, now)
        self._remove_pending_for_leases([lease["lease_id"]])
        return {"state": "retryable" if retryable else "failed", "server_time": now}

    def begin_result(self, cert_der: bytes, body: dict) -> dict:
        node = self._authenticate(cert_der)
        if not isinstance(body, dict) or set(body) != {"binding", "artifacts", "metrics"}:
            raise ProtocolError(400, "invalid_result_manifest")
        binding = self._binding({"binding": body["binding"]})
        if not isinstance(body["artifacts"], list) or not 1 <= len(body["artifacts"]) <= self.limits.max_artifacts_per_result:
            raise ProtocolError(400, "invalid_result_manifest")
        metrics = self._validate_metrics(body["metrics"])
        now = time.time()
        with self.store.transaction() as db:
            lease = self._live_lease(db, node, binding, now)
            stage = db.execute("SELECT * FROM stages WHERE job_id=? AND revision=? AND stage_key=?", (lease["job_id"], lease["job_revision"], lease["stage_key"])).fetchone()
            job = db.execute("SELECT geometry_json FROM jobs WHERE job_id=?", (lease["job_id"],)).fetchone()
            geometry = json.loads(job["geometry_json"])
            descriptors = [validate_artifact_descriptor(row, stage["action"], stage["stage_key"], geometry) for row in body["artifacts"]]
            ids = [row["id"] for row in descriptors]
            roles = [row["role"] for row in descriptors]
            if len(set(ids)) != len(ids) or len(set(roles)) != len(roles) or sum(row["bytes"] for row in descriptors) > self.limits.max_result_bytes:
                raise ProtocolError(400, "invalid_result_manifest")
            self._validate_result_roles(stage, descriptors)
            prior_rows = db.execute("SELECT * FROM uploads WHERE lease_id=?", (lease["lease_id"],)).fetchall()
            if prior_rows:
                prior = [{"id": row["artifact_id"], "type": row["type"], "role": row["role"], "bytes": row["bytes"], "sha256": row["sha256"], "geometry": json.loads(row["geometry_json"]), "labels": json.loads(row["labels_json"])} for row in prior_rows]
                if sorted(prior, key=lambda row: row["id"]) != sorted(descriptors, key=lambda row: row["id"]):
                    raise ProtocolError(409, "result_manifest_conflict")
                return {"uploads": [{"upload_id": row["upload_id"], "artifact_id": row["artifact_id"], "type": row["type"], "bytes": row["bytes"], "sha256": row["sha256"]} for row in prior_rows]}
            for descriptor in descriptors:
                upload_id = uuid.uuid4().hex
                db.execute("INSERT INTO uploads(upload_id,lease_id,artifact_id,type,role,bytes,sha256,geometry_json,labels_json,state) VALUES(?,?,?,?,?,?,?,?,?,'pending')",
                           (upload_id, lease["lease_id"], descriptor["id"], descriptor["type"], descriptor["role"], descriptor["bytes"], descriptor["sha256"], json.dumps(descriptor["geometry"], sort_keys=True, separators=(",", ":")), json.dumps(descriptor.get("labels", []), separators=(",", ":"))))
            db.execute("UPDATE stages SET status='uploading',updated_at=? WHERE job_id=? AND revision=? AND stage_key=?", (now, lease["job_id"], lease["job_revision"], lease["stage_key"]))
        with self.store.transaction() as db:
            uploads = db.execute("SELECT upload_id,artifact_id,type,bytes,sha256 FROM uploads WHERE lease_id=? ORDER BY artifact_id", (binding["lease_id"],)).fetchall()
        # Metrics are persisted only once and remain server metadata; null means unmeasured.
        with self.store.transaction() as db:
            db.execute("UPDATE leases SET progress_message='uploading' WHERE lease_id=?", (binding["lease_id"],))
            db.execute("INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
                       (f"lease_metrics:{binding['lease_id']}", json.dumps(metrics, allow_nan=False, sort_keys=True), now))
        return {"uploads": [dict(row) for row in uploads]}

    def _validate_result_roles(self, stage, descriptors):
        roles = {row["role"] for row in descriptors}
        kinds = [row["type"] for row in descriptors]
        action = stage["action"]
        if action == "infer_stage":
            structures_mod, _ = _canonical_modules()
            job_mode = stage["mode"]
            with self.store.read() as db:
                job = db.execute("SELECT selected_json FROM jobs WHERE job_id=?", (stage["job_id"],)).fetchone()
            plan = structures_mod.inference_plan(json.loads(job["selected_json"]), job_mode)
            task = next((row for row in plan if row[1] == stage["stage_key"]), None)
            if not task:
                raise ProtocolError(422, "inference_not_canonical")
            mask_names = task[2].get("roi_subset") or {"lung_vessels": ["lung_arteries", "lung_veins", "lung_airways"], "lung_nodules": ["lung_nodules"]}.get(stage["stage_key"], [])
            expected = {f"prediction.{stage['stage_key']}.{name}" for name in mask_names} | {f"inference.{stage['stage_key']}"}
            if roles != expected or kinds.count("inference_json") != 1 or kinds.count("nifti_mask") != len(mask_names):
                raise ProtocolError(422, "inference_result_roles_invalid")
        elif action == "mesh_stage":
            if roles != {"surface_export"} or kinds != ["mesh_json"]:
                raise ProtocolError(422, "mesh_result_roles_invalid")
        elif action == "candidate_stage":
            if "candidate_review" not in roles or kinds.count("candidates_json") != 1 or any(kind not in {"candidates_json", "nifti_mask"} for kind in kinds):
                raise ProtocolError(422, "candidate_result_roles_invalid")

    def _validate_metrics(self, value):
        if not isinstance(value, dict) or set(value) - {"wall_seconds", "cpu_seconds", "peak_rss_bytes", "transfer_seconds"}:
            raise ProtocolError(400, "invalid_metrics")
        result = {}
        for key in ("wall_seconds", "cpu_seconds", "transfer_seconds"):
            item = value.get(key)
            if item is not None and (isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item) or item < 0 or item > 7 * 24 * 3600):
                raise ProtocolError(400, "invalid_metrics")
            result[key] = item
        item = value.get("peak_rss_bytes")
        if item is not None:
            result["peak_rss_bytes"] = require_int(item, "peak_rss_bytes", 0, 2**63 - 1)
        else:
            result["peak_rss_bytes"] = None
        return result

    def put_result(self, cert_der: bytes, lease_id: str, upload_id: str, stream: bytes | BinaryIO, content_length: int) -> dict:
        node = self._authenticate(cert_der)
        require_id(lease_id, "lease_id")
        require_id(upload_id, "upload_id")
        if isinstance(content_length, bool) or not isinstance(content_length, int) or content_length < 0 or content_length > self.limits.max_result_bytes:
            raise ProtocolError(413, "body_too_large")
        with self._upload_guard(upload_id):
            now = time.time()
            with self.store.read() as db:
                upload = db.execute("SELECT u.upload_id,u.lease_id,u.artifact_id,u.type,u.role,u.bytes,u.sha256,u.geometry_json,u.labels_json,u.state AS upload_state,u.path,l.job_id,l.job_revision,l.stage_key,l.stage_attempt,l.fencing_generation,l.node_id,l.instance_id,l.engine_id,l.engine_sha256,l.model_set_sha256,l.input_sha256,l.geometry_sha256,l.state AS lease_state,l.expires_at,l.grace_until,s.action FROM uploads u JOIN leases l USING(lease_id) JOIN stages s ON s.job_id=l.job_id AND s.revision=l.job_revision AND s.stage_key=l.stage_key WHERE u.upload_id=? AND u.lease_id=?", (upload_id, lease_id)).fetchone()
            if not upload or upload["node_id"] != node["node_id"]:
                raise ProtocolError(404, "upload_not_found")
            if content_length != upload["bytes"]:
                raise ProtocolError(413, "upload_length_mismatch")
            if upload["upload_state"] == "validated":
                path = Path(upload["path"])
                if path.is_file() and path.stat().st_size == upload["bytes"] and _sha_file(path) == upload["sha256"]:
                    if self._consume_body(stream, content_length) != upload["sha256"]:
                        raise ProtocolError(409, "upload_replay_conflict")
                    return {"received": True, "duplicate": True}
                raise ProtocolError(409, "validated_artifact_missing")
            self._check_live_lease_snapshot(upload, node, now)
            self._ensure_storage_capacity(content_length)
            temp_path = self.staging_root / f"{upload_id}.part"
            if temp_path.exists():
                if _is_linklike(temp_path) or not temp_path.resolve().is_relative_to(self.staging_root):
                    raise ProtocolError(409, "upload_staging_invalid")
                temp_path.unlink()
            digest = hashlib.sha256()
            count = 0
            final_path = None
            created_final = False
            try:
                with temp_path.open("xb") as output:
                    for chunk in self._iter_body(stream, content_length):
                        count += len(chunk)
                        if count > upload["bytes"]:
                            raise ProtocolError(413, "body_too_large")
                        digest.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if count != upload["bytes"] or digest.hexdigest() != upload["sha256"]:
                    raise ProtocolError(422, "artifact_hash_mismatch")
                descriptor = {"id": upload["artifact_id"], "type": upload["type"], "role": upload["role"], "bytes": upload["bytes"], "sha256": upload["sha256"], "geometry": json.loads(upload["geometry_json"]), "labels": json.loads(upload["labels_json"])}
                with self.store.transaction() as db:
                    self._live_lease(db, node, self._binding_from_lease(upload), time.time())
                    db.execute("UPDATE stages SET status='validating',updated_at=? WHERE job_id=? AND revision=? AND stage_key=?", (time.time(), upload["job_id"], upload["job_revision"], upload["stage_key"]))
                job = self._job_geometry(upload["job_id"], upload["job_revision"])
                validate_output_file(temp_path, descriptor, job, upload["action"], upload["stage_key"])
                with self.store.transaction() as db:
                    self._live_lease(db, node, self._binding_from_lease(upload), time.time())
                    stage_dir = self._stage_artifact_dir(upload["job_id"], upload["job_revision"], upload["stage_key"], upload["stage_attempt"])
                    final_path = stage_dir / f"{upload['artifact_id']}{'.nii.gz' if upload['type']=='nifti_mask' else '.json'}"
                    if final_path.exists():
                        raise ProtocolError(409, "artifact_path_conflict")
                    os.replace(temp_path, final_path)
                    created_final = True
                    if os.name != "nt":
                        os.chmod(final_path, 0o400)
                    db.execute("UPDATE uploads SET state='validated',path=?,received_at=? WHERE upload_id=? AND state='pending'", (str(final_path), time.time(), upload_id))
                    db.execute("INSERT INTO stage_artifacts(job_id,revision,stage_key,stage_attempt,artifact_id,type,role,bytes,sha256,path,geometry_json,labels_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (upload["job_id"], upload["job_revision"], upload["stage_key"], upload["stage_attempt"], upload["artifact_id"], upload["type"], upload["role"], upload["bytes"], upload["sha256"], str(final_path), upload["geometry_json"], upload["labels_json"], time.time()))
            except BaseException as error:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
                if created_final and final_path and final_path.is_file() and not _is_linklike(final_path):
                    try:
                        if os.name != "nt":
                            os.chmod(final_path, 0o600)
                        final_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                if isinstance(error, OSError):
                    raise ProtocolError(507, "storage_unavailable") from None
                raise
            return {"received": True, "duplicate": False}

    def _consume_body(self, stream, length, discard=False):
        count = 0
        digest = hashlib.sha256()
        for chunk in self._iter_body(stream, length):
            count += len(chunk)
            digest.update(chunk)
        if count != length:
            raise ProtocolError(400, "body_length_mismatch")
        return digest.hexdigest()

    def _iter_body(self, stream, length):
        source = io.BytesIO(stream) if isinstance(stream, bytes) else stream
        if isinstance(stream, bytes) and len(stream) != length:
            raise ProtocolError(400, "body_length_mismatch")
        remaining = length
        while remaining:
            chunk = source.read(min(1024 * 1024, remaining))
            if not chunk:
                raise ProtocolError(400, "body_length_mismatch")
            remaining -= len(chunk)
            yield chunk

    def _upload_lock(self, upload_id):
        with self._upload_locks_guard:
            return self._upload_locks.setdefault(upload_id, threading.Lock())

    @contextmanager
    def _upload_guard(self, upload_id: str) -> Iterator[None]:
        """Serialize an upload across threads and coordinator processes."""
        require_id(upload_id, "upload_id")
        with self._upload_lock(upload_id):
            with self._cross_process_upload_lock(upload_id):
                yield

    @contextmanager
    def _cross_process_upload_lock(self, upload_id: str) -> Iterator[None]:
        lock_path = self.staging_root / f".{upload_id}.lock"
        if _is_linklike(lock_path):
            raise ProtocolError(409, "upload_lock_invalid")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(lock_path, flags, 0o600)
            lock_file = os.fdopen(descriptor, "r+b", buffering=0)
        except OSError:
            raise ProtocolError(507, "storage_unavailable") from None
        locked = False
        try:
            if not lock_path.resolve().is_relative_to(self.staging_root.resolve()) or _is_linklike(lock_path):
                raise ProtocolError(409, "upload_lock_invalid")
            if os.name == "nt":
                import msvcrt
                if os.fstat(lock_file.fileno()).st_size == 0:
                    lock_file.write(b"\0")
                    lock_file.flush()
                while True:
                    try:
                        lock_file.seek(0)
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                        locked = True
                        break
                    except OSError:
                        time.sleep(0.05)
            else:
                import fcntl
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                locked = True
            yield
        finally:
            if locked:
                if os.name == "nt":
                    import msvcrt
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()

    def _ensure_storage_capacity(self, required_bytes: int) -> None:
        try:
            free_bytes = shutil.disk_usage(self.staging_root).free
        except OSError:
            raise ProtocolError(507, "storage_unavailable") from None
        if free_bytes < required_bytes + self.limits.storage_reserve_bytes:
            raise ProtocolError(507, "storage_unavailable")

    def _stage_artifact_dir(self, job_id, revision, stage_key, attempt):
        if not STAGE_RE.fullmatch(stage_key):
            raise ProtocolError(400, "invalid_stage_key")
        path = self.results_root
        for component in (job_id, str(revision), stage_key, str(attempt)):
            path = path / component
            if path.exists() and _is_linklike(path):
                raise ProtocolError(409, "artifact_path_invalid")
            path.mkdir(exist_ok=True)
        resolved = path.resolve()
        if _is_linklike(path) or not resolved.is_relative_to(self.results_root):
            raise ProtocolError(409, "artifact_path_invalid")
        if os.name != "nt":
            for directory in (self.results_root / job_id, self.results_root / job_id / str(revision), self.results_root / job_id / str(revision) / stage_key, path):
                os.chmod(directory, 0o700)
        return resolved

    def _job_geometry(self, job_id, revision):
        with self.store.read() as db:
            row = db.execute("SELECT geometry_json FROM jobs WHERE job_id=? AND revision=? AND state='active'", (job_id, revision)).fetchone()
        if not row:
            raise ProtocolError(409, "job_stale")
        return json.loads(row["geometry_json"])

    def _binding(self, body):
        if not isinstance(body, dict) or set(body) != {"binding"} or not isinstance(body["binding"], dict):
            raise ProtocolError(400, "invalid_binding")
        value = body["binding"]
        keys = {"protocol_version", "job_id", "job_revision", "stage_key", "stage_attempt", "lease_id", "fencing_generation", "node_id", "engine_id", "engine_sha256", "model_set_sha256", "input_sha256", "geometry_sha256"}
        if set(value) != keys or value["protocol_version"] != 1:
            raise ProtocolError(400, "invalid_binding")
        for name in ("job_id", "lease_id", "node_id"):
            require_id(value[name], name)
        for name in ("engine_sha256", "model_set_sha256", "input_sha256", "geometry_sha256"):
            require_sha256(value[name], name)
        if not isinstance(value["stage_key"], str) or not STAGE_RE.fullmatch(value["stage_key"]) or not isinstance(value["engine_id"], str) or not 1 <= len(value["engine_id"]) <= 80:
            raise ProtocolError(400, "invalid_binding")
        require_int(value["job_revision"], "job_revision", 1, 2**31 - 1)
        require_int(value["stage_attempt"], "stage_attempt", 1, 10)
        require_int(value["fencing_generation"], "fencing_generation", 1, 2**31 - 1)
        return value

    def _binding_from_lease(self, row):
        return {"protocol_version": 1, "job_id": row["job_id"], "job_revision": row["job_revision"], "stage_key": row["stage_key"], "stage_attempt": row["stage_attempt"], "lease_id": row["lease_id"], "fencing_generation": row["fencing_generation"], "node_id": row["node_id"], "engine_id": row["engine_id"], "engine_sha256": row["engine_sha256"], "model_set_sha256": row["model_set_sha256"], "input_sha256": row["input_sha256"], "geometry_sha256": row["geometry_sha256"]}

    def _live_lease(self, db, node, binding, now, *, allow_completed=False):
        if "lan_principal" in node:
            self.lan_sessions.check_locked(db, node["lan_principal"], now)
        lease = db.execute("SELECT * FROM leases WHERE lease_id=?", (binding["lease_id"],)).fetchone()
        if not lease or lease["state"] != "active" and not (allow_completed and lease["state"] == "completed"):
            raise ProtocolError(409, "lease_invalid")
        if lease["node_id"] != node["node_id"] or lease["node_id"] != binding["node_id"] or lease["instance_id"] == "":
            raise ProtocolError(403, "lease_owner_mismatch")
        if "lan_principal" in node and lease["instance_id"] != node["lan_principal"].instance_id:
            raise ProtocolError(403, "lease_owner_mismatch")
        if self._binding_from_lease(lease) != binding:
            raise ProtocolError(409, "lease_binding_mismatch")
        job = db.execute("SELECT revision,state FROM jobs WHERE job_id=?", (lease["job_id"],)).fetchone()
        if not job or job["revision"] != lease["job_revision"] or job["state"] != "active":
            raise ProtocolError(409, "job_stale")
        stage = db.execute("SELECT attempt,fencing_generation,status FROM stages WHERE job_id=? AND revision=? AND stage_key=?", (lease["job_id"], lease["job_revision"], lease["stage_key"])).fetchone()
        if not stage or stage["attempt"] != lease["stage_attempt"] or stage["fencing_generation"] != lease["fencing_generation"]:
            raise ProtocolError(409, "lease_fenced")
        if lease["grace_until"] < now:
            raise ProtocolError(409, "lease_expired")
        node_row = db.execute("SELECT status,admin_enabled,certificate_not_after FROM nodes WHERE node_id=?", (node["node_id"],)).fetchone()
        if not node_row or node_row["status"] == "revoked" or not node_row["admin_enabled"] or node_row["certificate_not_after"] <= now:
            raise ProtocolError(403, "node_revoked")
        return lease

    def _check_live_lease_snapshot(self, upload, node, now):
        if upload["upload_state"] != "pending" or upload["lease_state"] != "active" or upload["grace_until"] < now:
            raise ProtocolError(409, "lease_invalid")
        with self.store.transaction() as db:
            self._live_lease(db, node, self._binding_from_lease(upload), now)

    def _authenticate(self, cert_der: bytes):
        from .lan_sessions import LanPrincipal
        if isinstance(cert_der, LanPrincipal):
            sessions = getattr(self, "lan_sessions", None)
            if sessions is None:
                raise ProtocolError(401, "session_invalid")
            now = time.time()
            with self.store.read() as db:
                sessions.check_locked(db, cert_der, now)
                row = db.execute("SELECT * FROM nodes WHERE node_id=?", (cert_der.node_id,)).fetchone()
            fingerprint = "lan:" + cert_der.node_id
            if (not row or row["certificate_fingerprint"] != fingerprint or
                    row["status"] == "revoked" or row["revoked_at"] is not None or not row["admin_enabled"]):
                raise ProtocolError(401, "session_invalid")
            return {"node_id": cert_der.node_id, "fingerprint": fingerprint, "lan_principal": cert_der}
        if not isinstance(cert_der, bytes) or not cert_der:
            raise ProtocolError(401, "client_certificate_required")
        fingerprint = certificate_fingerprint(cert_der)
        now = time.time()
        with self.store.read() as db:
            row = db.execute("SELECT * FROM nodes WHERE certificate_fingerprint=?", (fingerprint,)).fetchone()
        if not row or row["certificate_der"] != cert_der or row["status"] == "revoked" or row["revoked_at"] is not None or not row["admin_enabled"] or row["certificate_not_after"] <= now:
            raise ProtocolError(401, "node_certificate_invalid")
        return {"node_id": row["node_id"], "fingerprint": fingerprint}

    def enable_lan_sessions(self):
        """Enable the separate LAN identity adapter; does not start a listener."""
        from .lan_sessions import LanSessions
        if getattr(self, "lan_sessions", None) is None:
            self.lan_sessions = LanSessions(self.store, self._retire_lan_leases,
                                            prepare_node=self._prepare_lan_node)
        return self.lan_sessions

    def _prepare_lan_node(self, db, node_id, now):
        row = db.execute("SELECT * FROM nodes WHERE node_id=?", (node_id,)).fetchone()
        fingerprint = "lan:" + node_id
        if row:
            if row["certificate_fingerprint"] != fingerprint:
                raise ProtocolError(409, "node_identity_conflict")
            if row["status"] == "revoked" or not row["admin_enabled"]:
                raise ProtocolError(403, "node_disabled")
            return
        # The sentinel is an internal mode tag, never a DER certificate.
        db.execute("INSERT INTO nodes(node_id,certificate_fingerprint,certificate_der,certificate_pem,"
                   "display_name,capabilities_json,status,admin_enabled,created_at,approved_at,certificate_not_after) "
                   "VALUES(?,?,?,?,?,?,'ready',1,?,?,?)",
                   (node_id, fingerprint, b"", "", "OpenCT 节点", "{}", now, now, 253402300799))

    def _retire_lan_leases(self, db, node_id, now, reason):
        leases = db.execute("SELECT * FROM leases WHERE node_id=? AND state='active'", (node_id,)).fetchall()
        for lease in leases:
            db.execute("UPDATE leases SET state='released' WHERE lease_id=?", (lease["lease_id"],))
            stage = db.execute("SELECT attempt,max_attempts FROM stages WHERE job_id=? AND revision=? AND stage_key=?",
                               (lease["job_id"], lease["job_revision"], lease["stage_key"])).fetchone()
            if stage:
                retryable = stage["attempt"] < stage["max_attempts"]
                db.execute("UPDATE stages SET status=?,error_code=?,updated_at=? WHERE job_id=? AND revision=? "
                           "AND stage_key=? AND status IN ('leased','running','uploading','validating')",
                           ("retryable" if retryable else "failed", reason, now, lease["job_id"],
                            lease["job_revision"], lease["stage_key"]))
                if not retryable:
                    self._fail_job_locked(db, lease["job_id"], lease["job_revision"], reason, now)
            db.execute("UPDATE uploads SET state='rejected',error_code=? WHERE lease_id=? AND state!='validated'",
                       (reason, lease["lease_id"]))
        db.execute("UPDATE nodes SET active_instance=NULL WHERE node_id=?", (node_id,))

    def _reap_expired_locked(self, db, now):
        rows = db.execute("SELECT * FROM leases WHERE state='active' AND grace_until<=?", (now,)).fetchall()
        for lease in rows:
            job = db.execute("SELECT revision,state FROM jobs WHERE job_id=?", (lease["job_id"],)).fetchone()
            stage = db.execute("SELECT attempt,max_attempts,fencing_generation FROM stages WHERE job_id=? AND revision=? AND stage_key=?", (lease["job_id"], lease["job_revision"], lease["stage_key"])).fetchone()
            if not job or job["state"] != "active" or job["revision"] != lease["job_revision"] or not stage or stage["fencing_generation"] != lease["fencing_generation"]:
                state = "stale"
            else:
                state = "expired"
                status = "retryable" if stage["attempt"] < stage["max_attempts"] else "failed"
                db.execute("UPDATE stages SET status=?,error_code='lease_expired',updated_at=? WHERE job_id=? AND revision=? AND stage_key=?", (status, now, lease["job_id"], lease["job_revision"], lease["stage_key"]))
                if status == "failed":
                    self._fail_job_locked(db, lease["job_id"], lease["job_revision"], "lease_expired", now)
            db.execute("UPDATE leases SET state=? WHERE lease_id=?", (state, lease["lease_id"]))
            db.execute("UPDATE uploads SET state='rejected',error_code='lease_expired' WHERE lease_id=? AND state!='validated'", (lease["lease_id"],))
            db.execute("UPDATE nodes SET active_instance=NULL WHERE node_id=? AND active_instance=?", (lease["node_id"], lease["instance_id"]))
        return rows

    def _reap_locked(self, db, now):
        self._reap_expired_locked(db, now)

    def reap_expired(self) -> int:
        now = time.time()
        with self.store.transaction() as db:
            rows = self._reap_expired_locked(db, now)
        self._remove_pending_for_leases([row["lease_id"] for row in rows])
        return len(rows)

    def _recover_on_start(self):
        # First make lease state durable, then remove only stale partial uploads.
        # A second coordinator may be starting beside a live gateway process;
        # active upload parts are retained and never touched without their
        # cross-process upload lock.
        with self.store.transaction() as db:
            db.execute("UPDATE uploads SET state='pending',path=NULL WHERE state='received'")
            db.execute("UPDATE stages SET status='retryable',error_code='server_restart',updated_at=? WHERE status IN ('leased','running','uploading','validating') AND NOT EXISTS(SELECT 1 FROM leases l WHERE l.job_id=stages.job_id AND l.job_revision=stages.revision AND l.stage_key=stages.stage_key AND l.state='active' AND l.grace_until>?)", (time.time(), time.time()))
            self._reap_expired_locked(db, time.time())
        for path in self.staging_root.glob("*.part"):
            if not path.is_file() or _is_linklike(path):
                continue
            upload_id = path.stem
            try:
                require_id(upload_id, "upload_id")
            except ProtocolError:
                continue
            if self._upload_is_live_pending(upload_id):
                continue
            with self._upload_guard(upload_id):
                if not self._upload_is_live_pending(upload_id) and path.is_file() and not _is_linklike(path):
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass

    def _upload_is_live_pending(self, upload_id: str) -> bool:
        now = time.time()
        with self.store.read() as db:
            row = db.execute("SELECT 1 FROM uploads u JOIN leases l USING(lease_id) JOIN jobs j ON j.job_id=l.job_id JOIN stages s ON s.job_id=l.job_id AND s.revision=l.job_revision AND s.stage_key=l.stage_key WHERE u.upload_id=? AND u.state='pending' AND l.state='active' AND l.grace_until>? AND j.state='active' AND j.revision=l.job_revision AND s.attempt=l.stage_attempt AND s.fencing_generation=l.fencing_generation", (upload_id, now)).fetchone()
        return row is not None

    def _remove_pending_for_leases(self, leases):
        for lease_id in leases:
            with self.store.read() as db:
                upload_ids = [row[0] for row in db.execute("SELECT upload_id FROM uploads WHERE lease_id=?", (lease_id,))]
            for upload_id in upload_ids:
                path = self.staging_root / f"{upload_id}.part"
                with self._upload_guard(upload_id):
                    if not self._upload_is_live_pending(upload_id) and path.is_file() and not _is_linklike(path):
                        try:
                            path.unlink(missing_ok=True)
                        except OSError:
                            pass

    def _remove_pending_for_node(self, leases):
        self._remove_pending_for_leases([row["lease_id"] for row in leases])

    def claim_publish_event(self, publisher_id: str | None = None, lease_seconds: int = 300) -> dict | None:
        publisher_id = publisher_id or self.publisher_id
        if not isinstance(publisher_id, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", publisher_id):
            raise ProtocolError(400, "invalid_publisher_id")
        lease_seconds = require_int(lease_seconds, "publisher lease seconds", 5, 300)
        now = time.time()
        with self.store.transaction() as db:
            owner_row = db.execute("SELECT value_json FROM settings WHERE key='publisher_owner'").fetchone()
            owner = json.loads(owner_row["value_json"]) if owner_row else None
            if owner and owner["publisher_id"] != publisher_id and owner["expires_at"] > now:
                return None
            self._publisher_ids.add(publisher_id)
            db.execute("INSERT INTO settings(key,value_json,updated_at) VALUES('publisher_owner',?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at", (json.dumps({"publisher_id": publisher_id, "expires_at": now + lease_seconds}), now))
            row = db.execute("SELECT * FROM outbox WHERE state='pending' OR (state='claimed' AND claim_expires<=?) ORDER BY created_at LIMIT 1", (now,)).fetchone()
            if not row:
                return None
            job = db.execute("SELECT revision,state FROM jobs WHERE job_id=?", (row["job_id"],)).fetchone()
            if not job or job["revision"] != row["job_revision"] or job["state"] in {"cancelled", "tombstoned"}:
                db.execute("UPDATE outbox SET state='discarded' WHERE event_id=?", (row["event_id"],))
                return None
            changed = db.execute("UPDATE outbox SET state='claimed',claimed_by=?,claim_expires=? WHERE event_id=? AND (state='pending' OR (state='claimed' AND claim_expires<=?))", (publisher_id, now + lease_seconds, row["event_id"], now)).rowcount
            if not changed:
                return None
            payload = json.loads(row["payload_json"])
            payload["event_id"] = row["event_id"]
            return payload

    def ack_publish_event(self, event_id: str, publisher_id: str | None = None) -> bool:
        publisher_id = publisher_id or self.publisher_id
        require_id(event_id, "event_id")
        now = time.time()
        with self.store.transaction() as db:
            owner_row = db.execute("SELECT value_json FROM settings WHERE key='publisher_owner'").fetchone()
            owner = json.loads(owner_row["value_json"]) if owner_row else None
            row = db.execute("SELECT * FROM outbox WHERE event_id=?", (event_id,)).fetchone()
            if row and row["state"] == "acked":
                return True
            if not owner or owner["publisher_id"] != publisher_id or owner["expires_at"] <= now or not row or row["state"] != "claimed" or row["claimed_by"] != publisher_id:
                return False
            job = db.execute("SELECT revision,state FROM jobs WHERE job_id=?", (row["job_id"],)).fetchone()
            if not job or job["revision"] != row["job_revision"] or job["state"] in {"cancelled", "tombstoned"}:
                db.execute("UPDATE outbox SET state='discarded' WHERE event_id=?", (event_id,))
                return False
            db.execute("UPDATE outbox SET state='acked',acked_at=?,claim_expires=NULL WHERE event_id=?", (now, event_id))
            return True

    def release_publish_event(self, event_id: str, publisher_id: str | None = None) -> bool:
        publisher_id = publisher_id or self.publisher_id
        require_id(event_id, "event_id")
        with self.store.transaction() as db:
            row = db.execute("SELECT state,claimed_by FROM outbox WHERE event_id=?", (event_id,)).fetchone()
            if not row or row["state"] != "claimed" or row["claimed_by"] != publisher_id:
                return False
            db.execute("UPDATE outbox SET state='pending',claimed_by=NULL,claim_expires=NULL WHERE event_id=?", (event_id,))
            return True

    def release_publisher(self, publisher_id: str | None = None):
        owners = {publisher_id} if publisher_id is not None and publisher_id in self._publisher_ids else set(self._publisher_ids) if publisher_id is None else set()
        for owner_id in owners:
            with self.store.transaction() as db:
                row = db.execute("SELECT value_json FROM settings WHERE key='publisher_owner'").fetchone()
                if row and json.loads(row["value_json"]).get("publisher_id") == owner_id:
                    db.execute("DELETE FROM settings WHERE key='publisher_owner'")
            self._publisher_ids.discard(owner_id)

    def _validate_final_candidate_review(self, lease_id, descriptors):
        review = next((row for row in descriptors if row["role"] == "candidate_review"), None)
        if not review:
            return
        with self.store.read() as db:
            upload = db.execute("SELECT path FROM uploads WHERE lease_id=? AND artifact_id=? AND state='validated'", (lease_id, review["id"])).fetchone()
        if not upload:
            return
        data = Path(upload["path"]).read_bytes()
        parsed = strict_json_loads(data, ARTIFACT_LIMITS["candidates_json"])
        roles = {row.get("mask_role") for row in parsed.get("candidates", []) if isinstance(row, dict) and row.get("mask_role")}
        actual = {row["role"] for row in descriptors if row["role"].startswith("candidate_mask.")}
        if roles != actual:
            raise ProtocolError(422, "candidate_mask_manifest_mismatch")

    def finalize_result(self, cert_der: bytes, body: dict) -> dict:
        node = self._authenticate(cert_der)
        binding = self._binding(body)
        now = time.time()
        with self.store.transaction() as db:
            lease = db.execute("SELECT * FROM leases WHERE lease_id=?", (binding["lease_id"],)).fetchone()
            if lease and lease["state"] == "completed":
                job = db.execute("SELECT revision,state FROM jobs WHERE job_id=?", (lease["job_id"],)).fetchone()
                if job and job["revision"] == lease["job_revision"] and job["state"] in {"active", "completed", "failed"} and lease["node_id"] == node["node_id"] and self._binding_from_lease(lease) == binding:
                    return {"state": "completed", "duplicate": True}
            lease = self._live_lease(db, node, binding, now)
            uploads = db.execute("SELECT * FROM uploads WHERE lease_id=? ORDER BY artifact_id", (lease["lease_id"],)).fetchall()
            if not uploads or any(row["state"] != "validated" for row in uploads):
                raise ProtocolError(409, "uploads_incomplete")
            stage = db.execute("SELECT * FROM stages WHERE job_id=? AND revision=? AND stage_key=?", (lease["job_id"], lease["job_revision"], lease["stage_key"])).fetchone()
            if stage["action"] == "candidate_stage":
                review = next((row for row in uploads if row["role"] == "candidate_review"), None)
                if review:
                    parsed = strict_json_loads(Path(review["path"]).read_bytes(), ARTIFACT_LIMITS["candidates_json"])
                    candidate_rows = parsed.get("candidates", parsed.get("listed_largest_candidates", []))
                    listed_roles = {row.get("mask_role") for row in candidate_rows if isinstance(row, dict) and row.get("mask_role")}
                    actual_roles = {row["role"] for row in uploads if row["role"].startswith("candidate_mask.")}
                    if listed_roles != actual_roles:
                        raise ProtocolError(422, "candidate_mask_manifest_mismatch")
            db.execute("UPDATE leases SET state='completed',progress_percent=99,last_progress=? WHERE lease_id=?", (now, lease["lease_id"]))
            db.execute("UPDATE stages SET status='completed',error_code=NULL,updated_at=? WHERE job_id=? AND revision=? AND stage_key=?", (now, lease["job_id"], lease["job_revision"], lease["stage_key"]))
            stage_artifacts = db.execute("SELECT * FROM stage_artifacts WHERE job_id=? AND revision=? AND stage_key=? AND stage_attempt=? ORDER BY artifact_id", (lease["job_id"], lease["job_revision"], lease["stage_key"], lease["stage_attempt"])).fetchall()
            all_stages = db.execute("SELECT stage_key,status FROM stages WHERE job_id=? AND revision=? ORDER BY stage_key", (lease["job_id"], lease["job_revision"])).fetchall()
            completed_keys = [row["stage_key"] for row in all_stages if row["status"] == "completed"]
            is_complete = bool(all_stages) and len(completed_keys) == len(all_stages)
            if is_complete:
                db.execute("UPDATE jobs SET state='completed',updated_at=? WHERE job_id=? AND revision=? AND state='active'", (now, lease["job_id"], lease["job_revision"]))
            completed_artifacts = db.execute("SELECT a.* FROM stage_artifacts a JOIN stages s ON s.job_id=a.job_id AND s.revision=a.revision AND s.stage_key=a.stage_key WHERE a.job_id=? AND a.revision=? AND a.stage_attempt=s.attempt AND s.status='completed' ORDER BY a.stage_key,a.artifact_id", (lease["job_id"], lease["job_revision"])).fetchall()
            metric_row = db.execute("SELECT value_json FROM settings WHERE key=?", (f"lease_metrics:{lease['lease_id']}",)).fetchone()
            metrics = json.loads(metric_row["value_json"]) if metric_row else {"wall_seconds": None, "cpu_seconds": None, "peak_rss_bytes": None, "transfer_seconds": None}
            payload = {"job_id": lease["job_id"], "job_revision": lease["job_revision"], "binding": self._binding_from_lease(lease), "stage_key": lease["stage_key"], "action": stage["action"], "structures": json.loads(stage["structures_json"]), "parameters": json.loads(stage["parameters_json"]), "metrics": metrics, "artifacts": [{"id": row["artifact_id"], "type": row["type"], "role": row["role"], "bytes": row["bytes"], "sha256": row["sha256"], "path": row["path"], "geometry": json.loads(row["geometry_json"]), "labels": json.loads(row["labels_json"])} for row in stage_artifacts], "completed_stages": [{"stage_key": key, "artifacts": [self._artifact_public(row) for row in completed_artifacts if row["stage_key"] == key]} for key in completed_keys], "job_complete": is_complete}
            event_id = uuid.uuid4().hex
            db.execute("INSERT INTO outbox(event_id,job_id,job_revision,stage_key,stage_attempt,payload_json,state,created_at) VALUES(?,?,?,?,?,?,'pending',?)",
                       (event_id, lease["job_id"], lease["job_revision"], lease["stage_key"], lease["stage_attempt"], json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")), now))
        return {"state": "completed", "duplicate": False}

    def input_response(self, cert_der: bytes, lease_id: str, input_id: str, range_header: str | None = None) -> Response:
        node = self._authenticate(cert_der)
        require_id(lease_id, "lease_id")
        require_id(input_id, "input_id")
        now = time.time()
        with self.store.transaction() as db:
            lease = db.execute("SELECT * FROM leases WHERE lease_id=?", (lease_id,)).fetchone()
            if not lease:
                raise ProtocolError(404, "input_not_found")
            binding = self._binding_from_lease(lease)
            lease = self._live_lease(db, node, binding, now)
            stage = db.execute("SELECT * FROM stages WHERE job_id=? AND revision=? AND stage_key=?", (lease["job_id"], lease["job_revision"], lease["stage_key"])).fetchone()
            allowed = {row["id"] for row in self._stage_input_rows(db, stage)}
            if input_id not in allowed:
                raise ProtocolError(404, "input_not_found")
            input_row = db.execute("SELECT * FROM job_inputs WHERE job_id=? AND revision=? AND input_id=?", (lease["job_id"], lease["job_revision"], input_id)).fetchone()
            if input_row:
                path, digest, size = Path(input_row["source_path"]), input_row["sha256"], input_row["bytes"]
            else:
                artifact = db.execute("SELECT a.* FROM stage_artifacts a JOIN stages s ON s.job_id=a.job_id AND s.revision=a.revision AND s.stage_key=a.stage_key WHERE a.job_id=? AND a.revision=? AND a.artifact_id=? AND a.stage_attempt=s.attempt AND s.status='completed'", (lease["job_id"], lease["job_revision"], input_id)).fetchone()
                if not artifact:
                    raise ProtocolError(404, "input_not_found")
                path, digest, size = Path(artifact["path"]), artifact["sha256"], artifact["bytes"]
        if _is_linklike(path) or not path.is_file() or path.stat().st_size != size or _sha_file(path) != digest:
            raise ProtocolError(409, "input_changed")
        start, end, status = 0, size - 1, 200
        headers = {"Content-Type": "application/octet-stream", "Content-Length": str(size), "Cache-Control": "no-store", "Accept-Ranges": "bytes", "ETag": f'"{digest}"'}
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if not match or "," in range_header or not any(match.groups()):
                return Response(416, {"Content-Length": "0", "Content-Range": f"bytes */{size}"}, b"")
            first, last = match.groups()
            if not first:
                suffix = int(last)
                if suffix <= 0:
                    return Response(416, {"Content-Length": "0", "Content-Range": f"bytes */{size}"}, b"")
                start = max(0, size - suffix)
            else:
                start = int(first)
                end = min(size - 1, int(last)) if last else size - 1
            if start >= size or end < start:
                return Response(416, {"Content-Length": "0", "Content-Range": f"bytes */{size}"}, b"")
            status = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            headers["Content-Length"] = str(end - start + 1)
        return Response(status, headers, FileBody(str(path), start, end - start + 1))

    def handle_device_request(self, method: str, path: str, body: bytes | dict | BinaryIO | None, peer_cert_der: bytes, headers: dict | None = None) -> Response:
        try:
            route = normalize_path(path)
            self._authenticate(peer_cert_der)
            method = method.upper()
            headers = {str(key).casefold(): str(value) for key, value in (headers or {}).items()}
            if route.startswith("/node/resource/") and method == "GET":
                parts = route.split("/")
                if parts[2] != "resource" or parts[3] not in {"engine", "model"}:
                    raise ProtocolError(404, "not_found")
                if len(parts) == 5:
                    asset = parts[4]
                elif len(parts) == 6 and parts[4] == "file":
                    asset = f"file/{parts[5]}"
                else:
                    raise ProtocolError(404, "not_found")
                return self.resources.response(parts[3], asset, headers.get("range"))
            if route.startswith("/node/input/") and method == "GET":
                parts = route.split("/")
                if len(parts) != 5:
                    raise ProtocolError(404, "not_found")
                return self.input_response(peer_cert_der, parts[3], parts[4], headers.get("range"))
            if route.startswith("/node/result/") and method == "PUT":
                parts = route.split("/")
                if len(parts) != 5:
                    raise ProtocolError(404, "not_found")
                content_length = int(headers.get("content-length", "-1"))
                result = self.put_result(peer_cert_der, parts[3], parts[4], body or b"", content_length)
                return Response.json(200, result)
            if method != "POST":
                raise ProtocolError(405, "method_not_allowed")
            if isinstance(body, dict):
                value = body
            else:
                raw = body.read(JSON_BODY_LIMIT + 1) if hasattr(body, "read") else body or b""
                value = strict_json_loads(raw)
            if route == "/node/heartbeat":
                result = self.heartbeat(peer_cert_der, value)
            elif route == "/node/claim":
                result = self.claim(peer_cert_der, value)
            elif route == "/node/rotate":
                result = self.request_certificate_rotation(peer_cert_der, value)
            elif route == "/node/renew":
                result = self.renew(peer_cert_der, value)
            elif route == "/node/progress":
                result = self.progress(peer_cert_der, value)
            elif route == "/node/release":
                result = self.release(peer_cert_der, value)
            elif route == "/node/result/begin":
                result = self.begin_result(peer_cert_der, value)
            elif route == "/node/result/finalize":
                result = self.finalize_result(peer_cert_der, value)
            else:
                raise ProtocolError(404, "not_found")
            return Response.json(200, result)
        except ProtocolError as error:
            return Response.json(error.status, {"error": error.code})
        except (ValueError, TypeError):
            return Response.json(400, {"error": "invalid_request"})
