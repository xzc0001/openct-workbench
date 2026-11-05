from __future__ import annotations

import hashlib
import json
import unittest

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from cluster.managed_grant import verify_managed_grant, verify_managed_proof
from cluster.protocol import ProtocolError, canonical_json


class ManagedGrantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        cls.public = cls.key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        cls.csr = b"synthetic CSR bytes"

    def claims(self):
        return {"version": 1, "issuer": "synthetic-manager", "audience": "synthetic-cluster",
                "action": "enroll", "grant_id": "a" * 32, "device_hash": "b" * 64, "node_id": None,
                "csr_sha256": hashlib.sha256(self.csr).hexdigest(), "poll_sha256": "c" * 64, "policy_revision": 7,
                "issued_at": 1000, "expires_at": 1100}

    def check(self, value, *, csr=None, now=1050, key=None):
        payload = value if isinstance(value, bytes) else canonical_json(value)
        sig = self.key.sign(payload, padding.PKCS1v15(), hashes.SHA256())
        return verify_managed_grant(payload, sig, key or self.public, issuer="synthetic-manager",
                                    audience="synthetic-cluster", csr_pem=csr or self.csr, now=now)

    def test_valid_grant_only_verifies_claims(self):
        self.assertEqual(self.check(self.claims())["policy_revision"], 7)

    def test_binding_time_and_closed_schema(self):
        for changes in ({"issuer": "wrong"}, {"audience": "other"}, {"action": "disable"},
                        {"grant_id": "a" * 31}, {"device_hash": "B" * 64},
                        {"node_id": "c" * 32}, {"action": "rotate"}, {"poll_sha256": "x" * 64},
                        {"policy_revision": True}, {"expires_at": 1000},
                        {"expires_at": 1301}, {"issued_at": 1051}, {"extra": "x"}):
            with self.subTest(changes=changes), self.assertRaises(ProtocolError):
                self.check({**self.claims(), **changes})
        with self.assertRaises(ProtocolError):
            self.check(self.claims(), csr=b"another CSR")
        with self.assertRaises(ProtocolError):
            self.check(self.claims(), now=1100)
        self.assertEqual(self.check({**self.claims(), "action": "rotate", "node_id": "c" * 32})["action"], "rotate")

    def test_untrusted_signature_and_noncanonical_or_duplicate_json(self):
        other = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        public = other.public_key().public_bytes(serialization.Encoding.PEM,
                                                 serialization.PublicFormat.SubjectPublicKeyInfo)
        with self.assertRaises(ProtocolError):
            self.check(self.claims(), key=public)
        with self.assertRaises(ProtocolError):
            self.check(json.dumps(self.claims()).encode())
        duplicate = canonical_json(self.claims()).replace(b'"version":1', b'"version":1,"version":1')
        with self.assertRaises(ProtocolError):
            self.check(duplicate)

    def test_signature_must_match_exact_payload_and_strong_key(self):
        payload = canonical_json(self.claims())
        with self.assertRaises(ProtocolError):
            verify_managed_grant(payload, b'\0' * 384, self.public,
                issuer="synthetic-manager", audience="synthetic-cluster", csr_pem=self.csr, now=1050)
        weak = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        weak_public = weak.public_key().public_bytes(serialization.Encoding.PEM,
                                                     serialization.PublicFormat.SubjectPublicKeyInfo)
        with self.assertRaises(ProtocolError):
            self.check(self.claims(), key=weak_public)

    def test_proof_requires_private_key_for_exact_csr_and_grant(self):
        node_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        csr = (x509.CertificateSigningRequestBuilder()
               .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic")]))
               .sign(node_key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM))
        payload = canonical_json({**self.claims(), "csr_sha256": hashlib.sha256(csr).hexdigest()})
        signed = b"openct-reconstruction-managed-v1\0" + hashlib.sha256(payload).digest()
        proof = node_key.sign(signed, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())
        signature = self.key.sign(payload, padding.PKCS1v15(), hashes.SHA256())
        claims = verify_managed_grant(payload, signature, self.public,
            issuer="synthetic-manager", audience="synthetic-cluster", csr_pem=csr, now=1050)
        self.assertEqual(claims["csr_sha256"], hashlib.sha256(csr).hexdigest())
        self.assertIsNone(verify_managed_proof(csr, payload, proof))
        with self.assertRaises(ProtocolError):
            verify_managed_proof(csr, payload + b" ", proof)
        with self.assertRaises(ProtocolError):
            verify_managed_proof(csr, payload, b"\0" * 384)


if __name__ == "__main__":
    unittest.main()
