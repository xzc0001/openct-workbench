"""Ephemeral direct-TLS gateway fixture shared by loopback and native tests.

The optional CLI prints one JSON line containing only public endpoint material,
then accepts JSON control lines on stdin (`approve_next`, `approve`, `revoke`,
`snapshot`, `shutdown`). Private keys remain in the fixture process/tempdir.
"""

from __future__ import annotations

import hashlib
import argparse
import http.client
import json
import os
import socket
import ssl
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cluster.api import Coordinator
from cluster.protocol import API_PREFIX
from cluster.server import _GatewayServer, _Handler
from cluster.tls import create_device_server_context, create_enrollment_server_context


def _private_key_pem(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def _certificate_pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def _new_ca(common_name: str):
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=365))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                         key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, key_cert_sign=True,
                                         crl_sign=True, encipher_only=None,
                                         decipher_only=None), critical=True)
            .sign(key, hashes.SHA256()))
    return key, cert


def _issue_leaf(ca_key, ca_cert, key, common_name: str, *,
                dns_names: tuple[str, ...] = (), ip_addresses: tuple[str, ...] = (),
                eku: tuple[x509.ObjectIdentifier, ...],
                not_before: datetime | None = None,
                not_after: datetime | None = None) -> x509.Certificate:
    now = datetime.now(timezone.utc)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    names: list[x509.GeneralName] = [x509.DNSName(name) for name in dns_names]
    import ipaddress
    names.extend(x509.IPAddress(ipaddress.ip_address(value)) for value in ip_addresses)
    builder = (x509.CertificateBuilder().subject_name(subject).issuer_name(ca_cert.subject)
               .public_key(key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(not_before or now - timedelta(minutes=5))
               .not_valid_after(not_after or now + timedelta(days=30))
               .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
               .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                            key_encipherment=True, data_encipherment=False,
                                            key_agreement=False, key_cert_sign=False,
                                            crl_sign=False, encipher_only=None,
                                            decipher_only=None), critical=True)
               .add_extension(x509.ExtendedKeyUsage(list(eku)), critical=False))
    if names:
        builder = builder.add_extension(x509.SubjectAlternativeName(names), critical=False)
    return builder.sign(ca_key, hashes.SHA256())


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if os.name != "nt":
        os.chmod(path, 0o600)
    return path


def json_http(port: int, method: str, path: str, body: dict | bytes | None,
              context: ssl.SSLContext, *, hostname: str = "localhost",
              headers: dict[str, str] | None = None,
              expected_leaf_sha256: str | None = None,
              timeout: float = 10.0) -> tuple[int, dict[str, str], bytes]:
    """Send one real HTTP/1.1 request over an explicitly verified TLS socket."""
    if isinstance(body, dict):
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        default_type = "application/json"
    elif isinstance(body, bytes):
        payload = body
        default_type = "application/octet-stream" if method.upper() == "PUT" else "application/json"
    else:
        payload = b""
        default_type = "application/json"
    request_headers = {"Host": hostname, "Connection": "close",
                       "Content-Length": str(len(payload)), "Content-Type": default_type}
    if headers:
        request_headers.update(headers)
    raw = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    tls = None
    try:
        raw.settimeout(timeout)
        tls = context.wrap_socket(raw, server_hostname=hostname)
        tls.settimeout(timeout)
        if expected_leaf_sha256:
            leaf = tls.getpeercert(binary_form=True)
            if not leaf or hashlib.sha256(leaf).hexdigest() != expected_leaf_sha256:
                raise ssl.SSLCertVerificationError("server leaf pin mismatch")
        lines = [f"{method.upper()} {path} HTTP/1.1"]
        lines.extend(f"{name}: {value}" for name, value in request_headers.items())
        tls.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + payload)
        response = http.client.HTTPResponse(tls)
        response.begin()
        data = response.read()
        return response.status, {name.casefold(): value for name, value in response.getheaders()}, data
    finally:
        if tls is not None:
            tls.close()
        else:
            raw.close()


class LoopbackGateway:
    """Starts the production gateway classes with an ephemeral localhost PKI."""

    def __init__(self, root: Path, coordinator: Coordinator):
        self.root = root
        self.coordinator = coordinator
        self.server_ca_key, self.server_ca_cert = _new_ca("synthetic loopback TLS CA")
        self.node_ca_key, self.node_ca_cert = _new_ca("synthetic loopback node CA")
        self.server_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        self.server_cert = _issue_leaf(
            self.server_ca_key, self.server_ca_cert, self.server_key,
            "localhost", dns_names=("localhost",),
            eku=(ExtendedKeyUsageOID.SERVER_AUTH,))
        self.server_dir = root / "loopback-pki"
        self.server_chain_path = _write(
            self.server_dir / "server-chain.pem",
            _certificate_pem(self.server_cert) + _certificate_pem(self.server_ca_cert))
        self.server_key_path = _write(self.server_dir / "server.key", _private_key_pem(self.server_key))
        self.server_ca_path = _write(self.server_dir / "server-ca.pem", _certificate_pem(self.server_ca_cert))
        self.node_ca_path = _write(self.server_dir / "node-ca.pem", _certificate_pem(self.node_ca_cert))
        # The gateway/workbench process deliberately has no signer credential.
        # A second Coordinator instance models the separately isolated signer.
        self.signer = Coordinator(
            root / "cluster.sqlite3", root / "cluster-artifacts",
            ca_cert_pem=_certificate_pem(self.node_ca_cert),
            ca_key_pem=_private_key_pem(self.node_ca_key),
            lease_seconds=60, expiry_grace_seconds=15, heartbeat_seconds=10)
        self.device_server = _GatewayServer(
            ("127.0.0.1", 0), _Handler, coordinator, "device",
            create_device_server_context(str(self.server_chain_path), str(self.server_key_path), str(self.node_ca_path)))
        self.enrollment_server = _GatewayServer(
            ("127.0.0.1", 0), _Handler, coordinator, "enrollment",
            create_enrollment_server_context(str(self.server_chain_path), str(self.server_key_path)))
        self.device_port = self.device_server.server_address[1]
        self.enrollment_port = self.enrollment_server.server_address[1]
        self.threads = [
            threading.Thread(target=self.device_server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True),
            threading.Thread(target=self.enrollment_server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True),
        ]
        for thread in self.threads:
            thread.start()

    @property
    def server_cert_sha256(self) -> str:
        return hashlib.sha256(self.server_cert.public_bytes(serialization.Encoding.DER)).hexdigest()

    def client_context(self, *, certificate_path: Path | None = None,
                       key_path: Path | None = None, ca_path: Path | None = None) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = True
        context.load_verify_locations(cafile=str(ca_path or self.server_ca_path))
        if certificate_path or key_path:
            if not certificate_path or not key_path:
                raise ValueError("both client certificate and key are required")
            context.load_cert_chain(str(certificate_path), str(key_path))
        return context

    def issue_unregistered_client(self, name: str = "unregistered synthetic node") -> tuple[Path, Path, bytes]:
        key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        cert = _issue_leaf(self.node_ca_key, self.node_ca_cert, key, name,
                           eku=(ExtendedKeyUsageOID.CLIENT_AUTH,))
        cert_path = _write(self.server_dir / f"{uuid.uuid4().hex}-unregistered.pem", _certificate_pem(cert))
        key_path = _write(self.server_dir / f"{uuid.uuid4().hex}-unregistered.key", _private_key_pem(key))
        return cert_path, key_path, cert.public_bytes(serialization.Encoding.DER)

    def client_identity_from_enrollment(self, key, certificate_pem: str) -> tuple[Path, Path, bytes]:
        cert = x509.load_pem_x509_certificate(certificate_pem.encode("ascii"))
        cert_path = _write(self.server_dir / f"{uuid.uuid4().hex}-node.pem", _certificate_pem(cert))
        key_path = _write(self.server_dir / f"{uuid.uuid4().hex}-node.key", _private_key_pem(key))
        return cert_path, key_path, cert.public_bytes(serialization.Encoding.DER)

    def client_configuration(self, pairing_token: str) -> dict[str, Any]:
        return {
            "endpoint": f"https://localhost:{self.device_port}",
            "enrollment_endpoint": f"https://localhost:{self.enrollment_port}",
            "server_ca_pem": _certificate_pem(self.server_ca_cert).decode("ascii"),
            "server_cert_sha256": self.server_cert_sha256,
            "pairing_token": pairing_token,
        }

    def close(self, *, timeout: float = 7.0):
        for server in (self.device_server, self.enrollment_server):
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass
        for thread in self.threads:
            thread.join(timeout=timeout)
        self.signer.close()


def _default_capabilities() -> dict[str, Any]:
    engine_id = "openct.reconstruction.cpu.windows-x64"
    engine_sha = hashlib.sha256(b"loopback fixture engine").hexdigest()
    return {
        "engine": {"id": engine_id, "sha256": engine_sha},
        "models": [],
        "resources": {"threads": 4, "memory_bytes": 256 * 1024**2,
                      "cpu_percent": 50, "free_disk_bytes": 1024**3},
        "platform": {"os": "windows", "arch": "x86_64", "version": "loopback"},
        "state": "ready",
    }


class NativeGatewayFixture:
    """Self-contained optional process fixture for a native-hosted DLL test."""

    def __init__(self, *, resource_catalog: dict | None = None,
                 resource_signing_key_pem: bytes | str | None = None):
        self._temp = tempfile.TemporaryDirectory(prefix="cluster-native-loopback-")
        self.root = Path(self._temp.name)
        self.coordinator = Coordinator(
            self.root / "cluster.sqlite3", self.root / "artifacts",
            lease_seconds=60, expiry_grace_seconds=15, heartbeat_seconds=10,
            resource_catalog=resource_catalog)
        self.coordinator.update_settings({"enabled": True})
        self.gateway = LoopbackGateway(self.root, self.coordinator)
        self.pairing = self.coordinator.create_pairing_token()
        if isinstance(resource_signing_key_pem, str):
            resource_signing_key_pem = resource_signing_key_pem.encode("ascii")
        self.resource_signing_key_pem = resource_signing_key_pem

    def public_config(self) -> dict[str, Any]:
        result = {**self.gateway.client_configuration(self.pairing["pairing_token"]),
                  "protocol_prefix": API_PREFIX,
                  "capabilities_template": _default_capabilities(),
                  "fixture_only": True}
        if self.resource_signing_key_pem:
            result["resource_signing_key_pem"] = self.resource_signing_key_pem.decode("ascii")
        return result

    def handle_command(self, command: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(command, dict) or not isinstance(command.get("action"), str):
            return {"ok": False, "error": "invalid_command"}
        action = command["action"]
        if action == "approve_next" and set(command) == {"action"}:
            with self.coordinator.store.read() as db:
                row = db.execute("SELECT enrollment_id FROM enrollments WHERE state='pending' AND expires_at>? ORDER BY created_at LIMIT 1", (time.time(),)).fetchone()
            if not row:
                return {"ok": False, "error": "no_pending_enrollment"}
            request = self.coordinator.approve_enrollment(row["enrollment_id"])
            signed = self.gateway.signer.process_approvals()
            return {"ok": signed == 1, "enrollment_id": row["enrollment_id"],
                    "node_id": request.get("node_id"), "signatures_issued": signed}
        if action == "approve" and set(command) == {"action", "enrollment_id"}:
            enrollment_id = command["enrollment_id"]
            request = self.coordinator.approve_enrollment(enrollment_id)
            signed = self.gateway.signer.process_approvals()
            return {"ok": signed == 1, "enrollment_id": enrollment_id,
                    "node_id": request.get("node_id"), "signatures_issued": signed}
        if action == "revoke" and set(command) == {"action", "node_id"}:
            return {"ok": True, **self.coordinator.revoke_node(command["node_id"])}
        if action == "snapshot" and set(command) == {"action"}:
            return {"ok": True, "nodes": self.coordinator.list_nodes(),
                    "enrollments": self.coordinator.list_enrollments()}
        return {"ok": False, "error": "unsupported_command"}

    def close(self):
        self.gateway.close()
        self.coordinator.close()
        self._temp.cleanup()


def _parser():
    parser = argparse.ArgumentParser(description="Synthetic TLS fixture for reconstruction node transport tests")
    parser.add_argument("--resource-root")
    parser.add_argument("--resource-manifest")
    parser.add_argument("--resource-signature")
    parser.add_argument("--resource-public-key")
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    resource_values = (args.resource_root, args.resource_manifest,
                       args.resource_signature, args.resource_public_key)
    resource_catalog = None
    resource_public_key = None
    if any(resource_values):
        if not all(resource_values):
            raise SystemExit("all four resource arguments are required together")
        resource_public_key = Path(args.resource_public_key).read_bytes()
        entry = {"root": args.resource_root,
                 "manifest": args.resource_manifest,
                 "signature": args.resource_signature,
                 "public_key_pem": resource_public_key}
        resource_catalog = {"engine": entry, "model": entry}
    fixture = NativeGatewayFixture(resource_catalog=resource_catalog,
                                   resource_signing_key_pem=resource_public_key)
    print(json.dumps(fixture.public_config(), ensure_ascii=False, separators=(",", ":")), flush=True)
    try:
        for line in sys.stdin:
            try:
                command = json.loads(line)
                if command == {"action": "shutdown"}:
                    print(json.dumps({"ok": True, "state": "stopping"}), flush=True)
                    break
                response = fixture.handle_command(command)
            except Exception:
                response = {"ok": False, "error": "fixture_command_failed"}
            print(json.dumps(response, ensure_ascii=False, separators=(",", ":")), flush=True)
    finally:
        fixture.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
