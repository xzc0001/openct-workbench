"""Verify a proposed device/CSR-bound management enrollment grant.

This is a verification primitive, not an enabled enrollment endpoint. Callers
must additionally authenticate proof of possession, atomically consume the
grant, check current device authorization, and bind issuing/rotation to the
coordinator lifecycle. No verification failure may be retried as manual pairing.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import time

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .protocol import ProtocolError, canonical_json, strict_json_loads

_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_FIELDS = {"version", "issuer", "audience", "action", "grant_id", "device_hash", "node_id",
           "csr_sha256", "poll_sha256", "policy_revision", "issued_at", "expires_at"}
_MAX_GRANT_BYTES = 4096
_MAX_LIFETIME_SECONDS = 300
_PROOF_DOMAIN = b"openct-reconstruction-managed-v1\0"


def verify_managed_grant(payload: bytes, signature: bytes, public_key_pem: bytes,
                         *, issuer: str, audience: str, csr_pem: bytes,
                         now: int | None = None) -> dict:
    """Authenticate a single canonical grant; does not consume or enroll it."""
    def invalid():
        raise ProtocolError(401, "managed_grant_invalid")

    if (not isinstance(payload, bytes) or not 1 <= len(payload) <= _MAX_GRANT_BYTES
            or not isinstance(signature, bytes) or len(signature) != 384
            or not isinstance(public_key_pem, bytes) or not 1 <= len(public_key_pem) <= 16384
            or not isinstance(csr_pem, bytes)
            or not 1 <= len(csr_pem) <= 16384
            or not isinstance(issuer, str) or not issuer or len(issuer) > 128
            or not isinstance(audience, str) or not audience or len(audience) > 128):
        invalid()
    try:
        key = serialization.load_pem_public_key(public_key_pem)
        if not isinstance(key, rsa.RSAPublicKey) or key.key_size != 3072:
            invalid()
        key.verify(signature, payload, padding.PKCS1v15(), hashes.SHA256())
        claims = strict_json_loads(payload, max_bytes=_MAX_GRANT_BYTES)
    except ProtocolError:
        invalid()
    except Exception:
        invalid()
    if not isinstance(claims, dict) or set(claims) != _FIELDS or canonical_json(claims) != payload:
        invalid()
    if (type(claims["version"]) is not int or claims["version"] != 1
            or claims["issuer"] != issuer or claims["audience"] != audience
            or claims["action"] not in ("enroll", "rotate")
            or (claims["action"] == "enroll" and claims["node_id"] is not None)
            or (claims["action"] == "rotate" and
                (not isinstance(claims["node_id"], str) or not _HEX32.fullmatch(claims["node_id"])))
            or not isinstance(claims["grant_id"], str) or not _HEX32.fullmatch(claims["grant_id"])
            or not isinstance(claims["device_hash"], str) or not _HEX64.fullmatch(claims["device_hash"])
            or not isinstance(claims["csr_sha256"], str) or not _HEX64.fullmatch(claims["csr_sha256"])
            or not isinstance(claims["poll_sha256"], str) or not _HEX64.fullmatch(claims["poll_sha256"])
            or not hmac.compare_digest(claims["csr_sha256"], hashlib.sha256(csr_pem).hexdigest())
            or type(claims["policy_revision"]) is not int or not 1 <= claims["policy_revision"] <= 2**63 - 1
            or type(claims["issued_at"]) is not int or type(claims["expires_at"]) is not int):
        invalid()
    current = int(time.time()) if now is None else now
    if (type(current) is not int or claims["issued_at"] > current
            or claims["expires_at"] <= current
            or not 0 < claims["expires_at"] - claims["issued_at"] <= _MAX_LIFETIME_SECONDS):
        invalid()
    return claims


def verify_managed_proof(csr_pem: bytes, payload: bytes, proof: bytes) -> None:
    """Check possession of the CSR key for this exact grant; never consumes it."""
    if (not isinstance(csr_pem, bytes) or not 1 <= len(csr_pem) <= 16384
            or not isinstance(payload, bytes) or not 1 <= len(payload) <= _MAX_GRANT_BYTES
            or not isinstance(proof, bytes) or len(proof) != 384):
        raise ProtocolError(401, "managed_proof_invalid")
    try:
        csr = x509.load_pem_x509_csr(csr_pem)
        key = csr.public_key()
        if not csr.is_signature_valid or not isinstance(key, rsa.RSAPublicKey) or key.key_size != 3072:
            raise ValueError("invalid CSR")
        message = _PROOF_DOMAIN + hashlib.sha256(payload).digest()
        key.verify(proof, message, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                               salt_length=32), hashes.SHA256())
    except Exception:
        raise ProtocolError(401, "managed_proof_invalid") from None
