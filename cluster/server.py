"""Direct-TLS cluster gateway, separate from the human workbench listener.

Example (the private key files should be systemd LoadCredential files):
  python -m cluster.server \
    --db /var/lib/reconstruction-cluster/cluster.sqlite3 \
    --artifacts /var/lib/reconstruction-cluster/artifacts \
    --server-chain /etc/reconstruction/tls/server-fullchain.pem \
    --server-key "$CREDENTIALS_DIRECTORY/server.key" \
    --client-ca /etc/reconstruction/pki/node-ca.pem \
    --node-ca-cert /etc/reconstruction/pki/node-ca.pem \
    --resource-manifest /opt/reconstruction/engine/manifest.json \
    --resource-signature /opt/reconstruction/engine/manifest.sig \
    --resource-root /opt/reconstruction/engine \
    --resource-public-key /etc/reconstruction/pki/engine-signing-public.pem

Device and enrollment endpoints use the same configured server certificate,
with mTLS required only on the device port. The gateway receives only the
public node CA certificate. Run `python -m cluster.signer` as a
separate restricted process with the private CA credential. Human sessions,
CSRF, admin actions, and job publication remain in the offline workbench.
"""

from __future__ import annotations

import argparse
import os
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .api import Coordinator, create_device_server_context, create_enrollment_server_context, peer_certificate_der
from .protocol import JSON_BODY_LIMIT, ProtocolError, Response


MAX_UPLOAD_BYTES = 1024 * 1024 * 1024


class _GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = True

    def __init__(self, address, handler, coordinator, listener_kind, tls_context):
        self.tls_context = tls_context
        self._connection_slots = threading.BoundedSemaphore(32)
        super().__init__(address, handler)
        self.coordinator = coordinator
        self.listener_kind = listener_kind

    def get_request(self):
        raw_socket, address = self.socket.accept()
        tls_socket = None
        try:
            raw_socket.settimeout(5)
            tls_socket = self.tls_context.wrap_socket(raw_socket, server_side=True, do_handshake_on_connect=False)
            tls_socket.settimeout(5)
            tls_socket.do_handshake()
            tls_socket.settimeout(20)
            return tls_socket, address
        except BaseException:
            if tls_socket is not None:
                tls_socket.close()
            else:
                raw_socket.close()
            raise

    def process_request(self, request, client_address):
        if not self._connection_slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._connection_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connection_slots.release()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        # Request paths and peer addresses are not written to shared service logs.
        return

    def _respond(self, response: Response):
        self.send_response(response.status)
        for key, value in response.headers.items():
            self.send_header(key, value)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        body = response.body
        if isinstance(body, bytes):
            if body:
                self.wfile.write(body)
        else:
            for chunk in body.iter_chunks():
                self.wfile.write(chunk)

    def _read_length(self, maximum):
        if self.headers.get("Transfer-Encoding"):
            raise ProtocolError(400, "transfer_encoding_forbidden")
        raw = self.headers.get("Content-Length")
        if raw is None or not raw.isdecimal():
            raise ProtocolError(411, "content_length_required")
        size = int(raw)
        if size > maximum:
            raise ProtocolError(413, "body_too_large")
        return size

    def _dispatch(self):
        try:
            if self.server.listener_kind == "enrollment":
                if self.command != "POST":
                    raise ProtocolError(405, "method_not_allowed")
                if not self.headers.get("Content-Type", "").casefold().startswith("application/json"):
                    raise ProtocolError(415, "json_required")
                size = self._read_length(JSON_BODY_LIMIT)
                body = self.rfile.read(size)
                if len(body) != size:
                    raise ProtocolError(400, "body_length_mismatch")
                response = self.server.coordinator.enrollment.handle_request(self.command, self.path, body)
                self._respond(response)
                return
            path = self.path
            if "?" in path or "#" in path:
                raise ProtocolError(404, "not_found")
            cert_der = peer_certificate_der(self.connection)
            if self.command == "POST":
                if not self.headers.get("Content-Type", "").casefold().startswith("application/json"):
                    raise ProtocolError(415, "json_required")
                size = self._read_length(JSON_BODY_LIMIT)
                body = self.rfile.read(size)
                if len(body) != size:
                    raise ProtocolError(400, "body_length_mismatch")
            elif self.command == "PUT":
                if not self.headers.get("Content-Type", "").casefold().startswith("application/octet-stream"):
                    raise ProtocolError(415, "binary_required")
                size = self._read_length(MAX_UPLOAD_BYTES)
                body = self.rfile
            elif self.command == "GET":
                body = None
            else:
                raise ProtocolError(405, "method_not_allowed")
            response = self.server.coordinator.handle_device_request(self.command, path, body, cert_der, dict(self.headers.items()))
            self._respond(response)
        except PermissionError:
            self._respond(Response.json(401, {"error": "client_certificate_required"}))
        except ProtocolError as error:
            self._respond(Response.json(error.status, {"error": error.code}))
        except (ValueError, TypeError):
            self._respond(Response.json(400, {"error": "invalid_request"}))
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True

    do_POST = _dispatch
    do_GET = _dispatch
    do_PUT = _dispatch


def _parser():
    parser = argparse.ArgumentParser(description="Direct TLS gateway for reconstruction compute nodes")
    parser.add_argument("--db", required=True)
    parser.add_argument("--artifacts", required=True)
    parser.add_argument("--device-host", default="0.0.0.0")
    parser.add_argument("--device-port", type=int, default=28446)
    parser.add_argument("--enrollment-host", default="0.0.0.0")
    parser.add_argument("--enrollment-port", type=int, default=28447)
    parser.add_argument("--server-chain", required=True)
    parser.add_argument("--server-key", required=True)
    parser.add_argument("--client-ca", required=True)
    parser.add_argument("--node-ca-cert", required=True)
    parser.add_argument("--resource-manifest")
    parser.add_argument("--resource-signature")
    parser.add_argument("--resource-root")
    parser.add_argument("--resource-public-key")
    parser.add_argument("--lease-seconds", type=int, default=60)
    parser.add_argument("--expiry-grace-seconds", type=int, default=15)
    parser.add_argument("--heartbeat-seconds", type=int, default=10)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    resource_values = (args.resource_manifest, args.resource_signature, args.resource_root, args.resource_public_key)
    resource_catalog = None
    if any(resource_values):
        if not all(resource_values):
            raise SystemExit("all four resource arguments are required together")
        public_key = Path(args.resource_public_key).read_bytes()
        entry = {"manifest": args.resource_manifest, "signature": args.resource_signature, "root": args.resource_root, "public_key_pem": public_key}
        resource_catalog = {"engine": entry, "model": entry}
    coordinator = Coordinator(
        args.db,
        args.artifacts,
        ca_cert_pem=Path(args.node_ca_cert).read_bytes(),
        lease_seconds=args.lease_seconds,
        expiry_grace_seconds=args.expiry_grace_seconds,
        heartbeat_seconds=args.heartbeat_seconds,
        resource_catalog=resource_catalog,
    )
    device = _GatewayServer((args.device_host, args.device_port), _Handler, coordinator, "device", create_device_server_context(args.server_chain, args.server_key, args.client_ca))
    enrollment = _GatewayServer((args.enrollment_host, args.enrollment_port), _Handler, coordinator, "enrollment", create_enrollment_server_context(args.server_chain, args.server_key))
    stopping = threading.Event()

    servers = [threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.25}, daemon=True) for server in (device, enrollment)]
    for thread in servers:
        thread.start()
    def request_stop(_signum, _frame):
        stopping.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    try:
        while not stopping.wait(0.5):
            if not all(thread.is_alive() for thread in servers):
                stopping.set()
    finally:
        stopping.set()
        for server in (device, enrollment):
            server.shutdown()
            server.server_close()
        for thread in servers:
            thread.join(timeout=5)
        coordinator.release_publisher()
        coordinator.close()


if __name__ == "__main__":
    main()
