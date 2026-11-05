"""Explicit, separate HTTP listener for an operator-selected LAN test network."""
from __future__ import annotations

import argparse
import ipaddress
import signal
import threading
from http.server import ThreadingHTTPServer

from .coordinator import Coordinator
from .protocol import API_PREFIX, JSON_BODY_LIMIT, ProtocolError, Response, strict_json_loads
from .resources import ResourceCatalog
from .server import MAX_UPLOAD_BYTES, _Handler

LAN_PREFIX = "/api/cluster/lan/v1"


class LanServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = True

    def __init__(self, address, coordinator, allowed_networks, resources=None):
        self.coordinator = coordinator
        self.sessions = coordinator.enable_lan_sessions()
        self.allowed_networks = tuple(ipaddress.ip_network(n, strict=False) for n in allowed_networks)
        if not self.allowed_networks:
            raise ValueError("at least one source network must be explicitly configured")
        self.resources = resources or ResourceCatalog(None)
        self._connection_slots = threading.BoundedSemaphore(32)
        super().__init__(address, LanHandler)

    def verify_request(self, request, address):
        try:
            peer = ipaddress.ip_address(address[0])
            return any(peer in network for network in self.allowed_networks)
        except ValueError:
            return False

    def get_request(self):
        sock, address = super().get_request()
        sock.settimeout(20)
        return sock, address

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


class LanHandler(_Handler):
    def _json_body(self):
        if not self.headers.get("Content-Type", "").casefold().startswith("application/json"):
            raise ProtocolError(415, "json_required")
        size = self._read_length(JSON_BODY_LIMIT)
        body = self.rfile.read(size)
        if len(body) != size:
            raise ProtocolError(400, "body_length_mismatch")
        return body

    def _principal(self):
        authorization = self.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            raise ProtocolError(401, "session_invalid")
        return self.server.sessions.authenticate(authorization[7:])

    def _dispatch(self):
        try:
            path = self.path
            if "?" in path or "#" in path or not path.startswith(LAN_PREFIX + "/"):
                raise ProtocolError(404, "not_found")
            route = path[len(LAN_PREFIX):]
            if route == "/session" and self.command == "POST":
                self._respond(Response.json(200, self.server.sessions.register(strict_json_loads(self._json_body()))))
                return
            if route == "/session/close" and self.command == "POST":
                if strict_json_loads(self._json_body()) != {}:
                    raise ProtocolError(400, "invalid_request")
                try:
                    principal = self._principal()
                    self.server.sessions.close(principal)
                except ProtocolError as error:
                    if error.status != 401:
                        raise
                self._respond(Response.json(200, {"closed": True}))
                return
            principal = self._principal()
            self.server.coordinator._authenticate(principal)
            # Activity on an authenticated session extends its lifetime; lease
            # validity remains separately enforced by the coordinator.
            self.server.sessions.heartbeat(principal)
            if self.command == "GET" and route.startswith("/node/resource/"):
                asset = route[len("/node/resource/"):]
                self._respond(self.server.resources.response("engine", asset, self.headers.get("Range")))
                return
            if self.command == "POST":
                body = self._json_body()
            elif self.command == "PUT":
                if not self.headers.get("Content-Type", "").casefold().startswith("application/octet-stream"):
                    raise ProtocolError(415, "binary_required")
                self._read_length(MAX_UPLOAD_BYTES)
                body = self.rfile
            elif self.command == "GET":
                body = None
            else:
                raise ProtocolError(405, "method_not_allowed")
            result = self.server.coordinator.handle_device_request(
                self.command, API_PREFIX + route, body, principal, dict(self.headers.items()))
            self._respond(result)
        except ProtocolError as error:
            self._respond(Response.json(error.status, {"error": error.code}))
        except (ValueError, TypeError):
            self._respond(Response.json(400, {"error": "invalid_request"}))
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True

    do_POST = _dispatch
    do_GET = _dispatch
    do_PUT = _dispatch


def main():
    parser = argparse.ArgumentParser(description="Explicit LAN HTTP reconstruction node listener")
    parser.add_argument("--db", required=True)
    parser.add_argument("--artifacts", required=True)
    parser.add_argument("--host", required=True, help="explicit bind address")
    parser.add_argument("--port", type=int, default=28448)
    parser.add_argument("--allow-cidr", action="append", required=True)
    parser.add_argument("--resource-root")
    parser.add_argument("--resource-manifest")
    args = parser.parse_args()
    if bool(args.resource_root) != bool(args.resource_manifest):
        parser.error("resource root and manifest must be supplied together")
    resources = ResourceCatalog({"engine": {"root": args.resource_root, "manifest": args.resource_manifest}},
                                allow_unsigned=True) if args.resource_root else ResourceCatalog(None)
    coordinator = Coordinator(args.db, args.artifacts)
    server = LanServer((args.host, args.port), coordinator, args.allow_cidr, resources)
    stopping = threading.Event()

    def reap():
        while not stopping.wait(5):
            server.sessions.reap()
            coordinator.reap_expired()

    worker = threading.Thread(target=reap, daemon=True)
    worker.start()
    def stop(_signum, _frame):
        stopping.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        stopping.set()
        server.server_close()
        worker.join()
        coordinator.close()


if __name__ == "__main__":
    main()
