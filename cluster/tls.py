"""TLS contexts for isolated direct mTLS and enrollment listeners."""

from __future__ import annotations

import hashlib
import ssl


TLS_MINIMUM = ssl.TLSVersion.TLSv1_2


def _server_context(certificate_chain: str, private_key: str) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = TLS_MINIMUM
    context.options |= getattr(ssl, "OP_NO_COMPRESSION", 0)
    context.load_cert_chain(certfile=certificate_chain, keyfile=private_key)
    return context


def create_device_server_context(certificate_chain: str, private_key: str, client_ca_bundle: str) -> ssl.SSLContext:
    """Direct node listener. The CA is local to this listener and peer certs are mandatory."""
    context = _server_context(certificate_chain, private_key)
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cafile=client_ca_bundle)
    return context


def create_enrollment_server_context(certificate_chain: str, private_key: str) -> ssl.SSLContext:
    """Separate HTTPS enrollment listener; one-time pairing gates requests."""
    context = _server_context(certificate_chain, private_key)
    context.verify_mode = ssl.CERT_NONE
    return context


def peer_certificate_der(tls_socket) -> bytes:
    """Return DER bytes from the authenticated TLS session; never read identity headers."""
    certificate = tls_socket.getpeercert(binary_form=True)
    if not certificate:
        raise PermissionError("client certificate required")
    return certificate


def certificate_fingerprint(certificate_der: bytes) -> str:
    if not isinstance(certificate_der, bytes) or not certificate_der:
        raise ValueError("client certificate DER is required")
    return hashlib.sha256(certificate_der).hexdigest()
