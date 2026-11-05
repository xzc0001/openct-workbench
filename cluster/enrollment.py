"""Single-use pairing, approved RSA CSR enrollment, and client certificate issue."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone

from .protocol import ProtocolError, Response, normalize_path, strict_json_loads, validate_capabilities
from .store import Store


def _crypto():
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa, padding
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
        return x509, hashes, serialization, rsa, padding, ExtendedKeyUsageOID, NameOID
    except ImportError:
        raise RuntimeError("cryptography is required for approved node enrollment") from None


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _clean_display_name(value) -> str:
    if not isinstance(value, str):
        raise ProtocolError(400, "invalid_display_name")
    value = "".join(ch for ch in value.strip() if unicodedata.category(ch)[0] != "C")
    if not value or len(value) > 80:
        raise ProtocolError(400, "invalid_display_name")
    return value


def _datetime(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc)


class EnrollmentService:
    def __init__(self, store: Store, *, ca_cert_pem: bytes | str | None = None, ca_key_pem: bytes | str | None = None,
                 pairing_ttl: int = 600, enrollment_ttl: int = 600, certificate_days: int = 90):
        self.store = store
        self.ca_cert_pem = ca_cert_pem.encode("ascii") if isinstance(ca_cert_pem, str) else ca_cert_pem
        self.ca_key_pem = ca_key_pem.encode("ascii") if isinstance(ca_key_pem, str) else ca_key_pem
        self.pairing_ttl = pairing_ttl
        self.enrollment_ttl = enrollment_ttl
        if isinstance(certificate_days, bool) or not isinstance(certificate_days, int) or not 1 <= certificate_days <= 365:
            raise ValueError("node client certificate lifetime must be between 1 and 365 days")
        self.certificate_days = certificate_days

    def create_pairing_token(self) -> dict:
        now = time.time()
        token = secrets.token_hex(32)
        expires = now + self.pairing_ttl
        with self.store.transaction() as db:
            db.execute("DELETE FROM pairing_tokens WHERE expires_at<? OR consumed_at IS NOT NULL", (now,))
            db.execute("INSERT INTO pairing_tokens(token_hash,created_at,expires_at) VALUES(?,?,?)", (_token_hash(token), now, expires))
        return {"pairing_token": token, "expires_in": self.pairing_ttl}

    def submit(self, body: dict) -> dict:
        if not isinstance(body, dict) or set(body) != {"pairing_token", "csr_pem", "display_name", "capabilities"}:
            raise ProtocolError(400, "invalid_enrollment_request")
        token, csr_pem = body["pairing_token"], body["csr_pem"]
        if not isinstance(token, str) or len(token) != 64 or any(ch not in "0123456789abcdef" for ch in token):
            raise ProtocolError(401, "pairing_invalid")
        if not isinstance(csr_pem, str) or len(csr_pem.encode("utf-8")) > 64 * 1024:
            raise ProtocolError(400, "csr_invalid")
        display_name = _clean_display_name(body["display_name"])
        capabilities = validate_capabilities(body["capabilities"])
        self.validate_csr(csr_pem)
        now = time.time()
        enrollment_id, poll_token = uuid.uuid4().hex, secrets.token_hex(32)
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM pairing_tokens WHERE token_hash=?", (_token_hash(token),)).fetchone()
            if not row or row["consumed_at"] is not None or row["expires_at"] <= now:
                raise ProtocolError(401, "pairing_invalid")
            db.execute("UPDATE pairing_tokens SET consumed_at=? WHERE token_hash=? AND consumed_at IS NULL", (now, row["token_hash"]))
            db.execute("INSERT INTO enrollments(enrollment_id,token_hash,poll_hash,csr_pem,display_name,capabilities_json,state,created_at,expires_at) VALUES(?,?,?,?,?,?,'pending',?,?)",
                       (enrollment_id, row["token_hash"], _token_hash(poll_token), csr_pem, display_name, json.dumps(capabilities, separators=(",", ":")), now, now + self.enrollment_ttl))
        return {"enrollment_id": enrollment_id, "poll_token": poll_token, "expires_in": self.enrollment_ttl}

    def validate_csr(self, csr_pem: str):
        x509, _, serialization, rsa, *_ = _crypto()
        try:
            csr = x509.load_pem_x509_csr(csr_pem.encode("ascii"))
            public = csr.public_key()
            if not csr.is_signature_valid or not isinstance(public, rsa.RSAPublicKey) or public.key_size < 3072:
                raise ValueError
        except Exception:
            raise ProtocolError(400, "csr_invalid") from None
        return csr

    def submit_rotation(self, node_id: str, body: dict) -> dict:
        if not isinstance(node_id, str) or len(node_id) != 32 or any(ch not in "0123456789abcdef" for ch in node_id):
            raise ProtocolError(400, "invalid_node_id")
        if not isinstance(body, dict) or set(body) != {"instance_id", "csr_pem", "capabilities"}:
            raise ProtocolError(400, "invalid_rotation_request")
        instance_id, csr_pem = body["instance_id"], body["csr_pem"]
        if not isinstance(instance_id, str) or len(instance_id) != 32 or any(ch not in "0123456789abcdef" for ch in instance_id):
            raise ProtocolError(400, "invalid_instance_id")
        if not isinstance(csr_pem, str) or len(csr_pem.encode("utf-8")) > 64 * 1024:
            raise ProtocolError(400, "csr_invalid")
        self.validate_csr(csr_pem)
        capabilities = validate_capabilities(body["capabilities"])
        if capabilities["state"] not in {"ready", "paused"}:
            raise ProtocolError(409, "node_not_ready_for_rotation")
        now = time.time()
        enrollment_id, poll_token = uuid.uuid4().hex, secrets.token_hex(32)
        with self.store.transaction() as db:
            node = db.execute("SELECT * FROM nodes WHERE node_id=?", (node_id,)).fetchone()
            if not node or node["status"] == "revoked" or not node["admin_enabled"] or node["active_instance"] not in (None, instance_id):
                raise ProtocolError(403, "node_rotation_forbidden")
            active = db.execute("SELECT lease_id FROM leases WHERE node_id=? AND state='active'", (node_id,)).fetchone()
            if active:
                raise ProtocolError(409, "node_busy")
            pending = db.execute("SELECT enrollment_id FROM enrollments WHERE node_id=? AND state='pending' AND expires_at>? LIMIT 1", (node_id, now)).fetchone()
            if pending:
                raise ProtocolError(409, "rotation_pending")
            # No pairing token is involved. Authentication came from the
            # registered certificate on the mTLS socket above this service.
            token_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
            db.execute("INSERT INTO enrollments(enrollment_id,token_hash,poll_hash,csr_pem,display_name,capabilities_json,state,created_at,expires_at,node_id,purpose) VALUES(?,?,?,?,?,?,'pending',?,?,?,'rotation')",
                       (enrollment_id, token_hash, _token_hash(poll_token), csr_pem, node["display_name"], json.dumps(capabilities, sort_keys=True, separators=(",", ":")), now, now + self.enrollment_ttl, node_id))
        return {"enrollment_id": enrollment_id, "poll_token": poll_token, "expires_in": self.enrollment_ttl}

    def request_approval(self, enrollment_id: str, approved_by: str = "admin") -> dict:
        if not isinstance(enrollment_id, str) or len(enrollment_id) != 32 or any(ch not in "0123456789abcdef" for ch in enrollment_id):
            raise ProtocolError(400, "invalid_enrollment_id")
        now = time.time()
        if not isinstance(approved_by, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", approved_by):
            raise ProtocolError(400, "invalid_approver")
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM enrollments WHERE enrollment_id=?", (enrollment_id,)).fetchone()
            if not row or row["state"] != "pending" or row["expires_at"] <= now:
                raise ProtocolError(409, "enrollment_not_pending")
            existing = db.execute("SELECT node_id,state FROM approval_requests WHERE enrollment_id=?", (enrollment_id,)).fetchone()
            if existing and existing["state"] == "pending":
                return {"state": "pending_signing", "node_id": existing["node_id"]}
            node_id = row["node_id"] or uuid.uuid4().hex
            rotating = db.execute("SELECT * FROM nodes WHERE node_id=?", (node_id,)).fetchone() if row["node_id"] else None
            if rotating:
                if rotating["status"] == "revoked" or rotating["revoked_at"] is not None or not rotating["admin_enabled"]:
                    raise ProtocolError(409, "rotation_node_unavailable")
                leases = db.execute("SELECT lease_id,job_id,job_revision,stage_key FROM leases WHERE node_id=? AND state='active'", (node_id,)).fetchall()
                for lease in leases:
                    db.execute("UPDATE leases SET state='revoked' WHERE lease_id=?", (lease["lease_id"],))
                    stage = db.execute("SELECT attempt,max_attempts FROM stages WHERE job_id=? AND revision=? AND stage_key=?", (lease["job_id"], lease["job_revision"], lease["stage_key"])).fetchone()
                    retryable = bool(stage and stage["attempt"] < stage["max_attempts"])
                    db.execute("UPDATE stages SET status=?,error_code='certificate_rotation',updated_at=? WHERE job_id=? AND revision=? AND stage_key=? AND status IN ('leased','running','uploading','validating')",
                               ("retryable" if retryable else "failed", now, lease["job_id"], lease["job_revision"], lease["stage_key"]))
                    db.execute("UPDATE uploads SET state='rejected',error_code='certificate_rotation' WHERE lease_id=? AND state='pending'", (lease["lease_id"],))
                    if not retryable:
                        db.execute("UPDATE jobs SET state='failed',updated_at=? WHERE job_id=? AND revision=? AND state='active'", (now, lease["job_id"], lease["job_revision"]))
                db.execute("UPDATE nodes SET admin_enabled=0,active_instance=NULL WHERE node_id=?", (node_id,))
            db.execute("INSERT INTO approval_requests(enrollment_id,node_id,approved_by,requested_at,state) VALUES(?,?,?,?,'pending')",
                       (enrollment_id, node_id, approved_by, now))
            db.execute("UPDATE enrollments SET node_id=? WHERE enrollment_id=? AND state='pending'", (node_id, enrollment_id))
        return {"state": "pending_signing", "node_id": node_id}

    def approve(self, enrollment_id: str, approved_by: str = "admin") -> dict:
        """Persist an admin approval; only the separate signer listener issues a cert."""
        result = self.request_approval(enrollment_id, approved_by)
        if self.ca_cert_pem and self.ca_key_pem:
            self.process_approvals()
            with self.store.read() as db:
                row = db.execute("SELECT certificate_fingerprint FROM nodes WHERE node_id=?", (result["node_id"],)).fetchone()
            if row:
                result["state"] = "approved"
                result["certificate_fingerprint"] = row["certificate_fingerprint"]
        return result

    def process_approvals(self, limit: int = 100) -> int:
        """Sign only rows explicitly approved by the browser-side admin adapter."""
        if not self.ca_cert_pem or not self.ca_key_pem:
            raise RuntimeError("the isolated enrollment signer requires an external CA certificate and private signer")
        now = time.time()
        with self.store.transaction() as db:
            rows = db.execute("SELECT a.enrollment_id,a.node_id,e.* FROM approval_requests a JOIN enrollments e USING(enrollment_id) WHERE a.state='pending' AND e.state='pending' AND e.expires_at>? ORDER BY a.requested_at LIMIT ?", (now, limit)).fetchall()
            expired = db.execute("SELECT a.enrollment_id FROM approval_requests a JOIN enrollments e USING(enrollment_id) WHERE a.state='pending' AND e.expires_at<=?", (now,)).fetchall()
            for item in expired:
                db.execute("UPDATE approval_requests SET state='expired' WHERE enrollment_id=?", (item["enrollment_id"],))
                db.execute("UPDATE enrollments SET state='expired' WHERE enrollment_id=? AND state='pending'", (item["enrollment_id"],))
                db.execute("UPDATE nodes SET admin_enabled=1 WHERE node_id=(SELECT node_id FROM approval_requests WHERE enrollment_id=?) AND status!='revoked'", (item["enrollment_id"],))
        completed = 0
        for row in rows:
            issued = self._issue_certificate(row, row["node_id"])
            capabilities = json.loads(row["capabilities_json"])
            with self.store.transaction() as db:
                current = db.execute("SELECT state FROM approval_requests WHERE enrollment_id=?", (row["enrollment_id"],)).fetchone()
                enrollment = db.execute("SELECT state,expires_at FROM enrollments WHERE enrollment_id=?", (row["enrollment_id"],)).fetchone()
                if not current or current["state"] != "pending" or not enrollment or enrollment["state"] != "pending" or enrollment["expires_at"] <= time.time():
                    continue
                existing_node = db.execute("SELECT * FROM nodes WHERE node_id=?", (row["node_id"],)).fetchone()
                if existing_node and existing_node["status"] == "revoked":
                    db.execute("UPDATE approval_requests SET state='expired' WHERE enrollment_id=?", (row["enrollment_id"],))
                    db.execute("UPDATE enrollments SET state='rejected' WHERE enrollment_id=?", (row["enrollment_id"],))
                    continue
                if existing_node:
                    db.execute("UPDATE nodes SET certificate_fingerprint=?,certificate_der=?,certificate_pem=?,display_name=?,capabilities_json=?,status=?,admin_enabled=1,approved_at=?,last_seen=NULL,certificate_not_after=?,revoked_at=NULL,active_instance=NULL WHERE node_id=?",
                               (issued["fingerprint"], issued["der"], issued["pem"], row["display_name"], row["capabilities_json"], capabilities.get("state", "ready"), now, issued["not_after"], row["node_id"]))
                else:
                    db.execute("INSERT INTO nodes(node_id,certificate_fingerprint,certificate_der,certificate_pem,display_name,capabilities_json,status,admin_enabled,created_at,approved_at,certificate_not_after) VALUES(?,?,?,?,?,?,?,1,?,?,?)",
                               (row["node_id"], issued["fingerprint"], issued["der"], issued["pem"], row["display_name"], row["capabilities_json"], capabilities.get("state", "ready"), now, now, issued["not_after"]))
                db.execute("UPDATE enrollments SET state='approved',certificate_pem=?,certificate_der=?,certificate_fingerprint=? WHERE enrollment_id=?",
                           (issued["pem"], issued["der"], issued["fingerprint"], row["enrollment_id"]))
                db.execute("UPDATE approval_requests SET state='processed' WHERE enrollment_id=?", (row["enrollment_id"],))
            completed += 1
        return completed

    def _issue_certificate(self, row, node_id: str) -> dict:
        x509, hashes, serialization, rsa, padding, eku_oid, name_oid = _crypto()
        try:
            ca_cert = x509.load_pem_x509_certificate(self.ca_cert_pem)
            ca_key = serialization.load_pem_private_key(self.ca_key_pem, password=None)
            if ca_cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo) != ca_key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo):
                raise ValueError("CA signer mismatch")
            if not isinstance(ca_key, rsa.RSAPrivateKey) or ca_key.key_size < 3072:
                raise ValueError("node CA must use RSA-3072 or stronger")
            if ca_cert.not_valid_before_utc.timestamp() > time.time() or ca_cert.not_valid_after_utc.timestamp() <= time.time():
                raise ValueError("node CA certificate is outside its validity period")
            constraints = ca_cert.extensions.get_extension_for_class(x509.BasicConstraints).value
            if not constraints.ca:
                raise ValueError("not a CA")
            try:
                usage = ca_cert.extensions.get_extension_for_class(x509.KeyUsage).value
                if not usage.key_cert_sign:
                    raise ValueError("CA key usage does not permit certificate signing")
            except x509.ExtensionNotFound:
                pass
            csr = self.validate_csr(row["csr_pem"])
            now_dt = datetime.now(timezone.utc)
            builder = (x509.CertificateBuilder()
                       .subject_name(x509.Name([x509.NameAttribute(name_oid.COMMON_NAME, node_id)]))
                       .issuer_name(ca_cert.subject)
                       .public_key(csr.public_key())
                       .serial_number(x509.random_serial_number())
                       .not_valid_before(now_dt - timedelta(minutes=1))
                       .not_valid_after(now_dt + timedelta(days=self.certificate_days))
                       .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                       .add_extension(x509.ExtendedKeyUsage([eku_oid.CLIENT_AUTH]), critical=True)
                       .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=True, data_encipherment=False, key_agreement=False, key_cert_sign=False, crl_sign=False, encipher_only=None, decipher_only=None), critical=True)
                       .add_extension(x509.SubjectAlternativeName([x509.UniformResourceIdentifier(f"urn:openct:node:{node_id}")]), critical=False))
            certificate = builder.sign(private_key=ca_key, algorithm=hashes.SHA256())
            cert_der = certificate.public_bytes(serialization.Encoding.DER)
            cert_pem = certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")
            fingerprint = hashlib.sha256(cert_der).hexdigest()
            cert_not_after = certificate.not_valid_after_utc.timestamp()
        except ProtocolError:
            raise
        except Exception:
            raise RuntimeError("configured node CA could not issue the approved certificate") from None
        return {"fingerprint": fingerprint, "der": cert_der, "pem": cert_pem, "not_after": cert_not_after}

    def reject(self, enrollment_id: str) -> None:
        now = time.time()
        with self.store.transaction() as db:
            changed = db.execute("UPDATE enrollments SET state='rejected' WHERE enrollment_id=? AND state='pending' AND expires_at>?", (enrollment_id, now)).rowcount
            if not changed:
                raise ProtocolError(409, "enrollment_not_pending")
            db.execute("UPDATE approval_requests SET state='expired' WHERE enrollment_id=? AND state='pending'", (enrollment_id,))
            db.execute("UPDATE nodes SET admin_enabled=1 WHERE node_id=(SELECT node_id FROM approval_requests WHERE enrollment_id=?) AND status!='revoked'", (enrollment_id,))

    def poll(self, body: dict) -> dict:
        if not isinstance(body, dict) or set(body) != {"enrollment_id", "poll_token"}:
            raise ProtocolError(400, "invalid_enrollment_poll")
        enrollment_id, poll_token = body["enrollment_id"], body["poll_token"]
        if not isinstance(enrollment_id, str) or len(enrollment_id) != 32 or any(ch not in "0123456789abcdef" for ch in enrollment_id) or not isinstance(poll_token, str) or len(poll_token) != 64 or any(ch not in "0123456789abcdef" for ch in poll_token):
            raise ProtocolError(400, "invalid_enrollment_poll")
        now = time.time()
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM enrollments WHERE enrollment_id=? AND poll_hash=?", (enrollment_id, _token_hash(poll_token))).fetchone()
            if not row:
                raise ProtocolError(404, "enrollment_not_found")
            if row["state"] == "pending" and row["expires_at"] <= now:
                db.execute("UPDATE enrollments SET state='expired' WHERE enrollment_id=?", (enrollment_id,))
                db.execute("UPDATE approval_requests SET state='expired' WHERE enrollment_id=? AND state='pending'", (enrollment_id,))
                db.execute("UPDATE nodes SET admin_enabled=1 WHERE node_id=(SELECT node_id FROM approval_requests WHERE enrollment_id=?) AND status!='revoked'", (enrollment_id,))
                state = "expired"
            else:
                state = row["state"]
        if state == "approved":
            return {"state": "approved", "node_id": row["node_id"], "certificate_pem": row["certificate_pem"], "expires_in": 0}
        with self.store.read() as db:
            approval = db.execute("SELECT state FROM approval_requests WHERE enrollment_id=?", (enrollment_id,)).fetchone()
        mapped = "provisioning" if state == "pending" and approval and approval["state"] == "pending" else "pending_approval" if state == "pending" else state
        return {"state": mapped, "node_id": row["node_id"], "expires_in": max(0, int(row["expires_at"] - now))}

    def handle_request(self, method: str, path: str, body: bytes | dict | None = None) -> Response:
        try:
            route = normalize_path(path)
            if method != "POST":
                raise ProtocolError(405, "method_not_allowed")
            value = body if isinstance(body, dict) else strict_json_loads(body or b"")
            if route == "/enroll":
                return Response.json(200, self.submit(value))
            if route == "/enroll/status":
                return Response.json(200, self.poll(value))
            raise ProtocolError(404, "not_found")
        except ProtocolError as error:
            return Response.json(error.status, {"error": error.code})


class EnrollmentSigner:
    """Minimal database-only process boundary for approved CSR signing.

    Unlike Coordinator, this service never opens artifact directories, reaps
    leases, schedules work, or claims publication events. The private CA key is
    held only by the process that instantiates this class.
    """

    def __init__(self, db_path, *, ca_cert_pem: bytes | str, ca_key_pem: bytes | str):
        self.store = Store(db_path)
        self.enrollment = EnrollmentService(self.store, ca_cert_pem=ca_cert_pem,
                                            ca_key_pem=ca_key_pem)

    def process_approvals(self, limit: int = 100) -> int:
        return self.enrollment.process_approvals(limit=limit)

    def close(self):
        self.store.close()
