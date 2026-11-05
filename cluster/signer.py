"""Isolated approval signer for cluster enrollment certificates.

The gateway/workbench coordinator records admin approvals without a private CA
key. This process alone receives the key credential and signs only queued,
explicitly approved requests from the shared cluster database.

Example (run under the restricted signer service identity):
  python -m cluster.signer \
    --db /var/lib/reconstruction-cluster/cluster.sqlite3 \
    --node-ca-cert /etc/reconstruction/pki/node-ca.pem \
    --node-ca-key "$CREDENTIALS_DIRECTORY/node-ca.key"
"""

from __future__ import annotations

import argparse
import json
import signal
import threading
import sys
from pathlib import Path

from .enrollment import EnrollmentSigner


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Separate certificate signer for approved reconstruction nodes")
    parser.add_argument("--db", required=True)
    parser.add_argument("--node-ca-cert", required=True)
    parser.add_argument("--node-ca-key", required=True,
                        help="private key supplied only through the restricted signer credential mount")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--once", action="store_true",
                        help="process one approval batch and exit (test/maintenance mode)")
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    if not 0.1 <= args.poll_seconds <= 3600:
        raise SystemExit("--poll-seconds must be between 0.1 and 3600")
    signer = EnrollmentSigner(
        args.db,
        ca_cert_pem=Path(args.node_ca_cert).read_bytes(),
        ca_key_pem=Path(args.node_ca_key).read_bytes(),
    )
    if args.once:
        try:
            print(json.dumps({"processed": signer.process_approvals()}, separators=(",", ":")), flush=True)
            return 0
        except Exception:
            sys.stderr.write("approval signer failed to process a batch\n")
            return 1
        finally:
            signer.close()

    stopping = threading.Event()

    def request_stop(_signum, _frame):
        stopping.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    try:
        while not stopping.wait(args.poll_seconds):
            try:
                signer.process_approvals()
            except Exception:
                # Do not write CSR, certificate, database, or patient values to logs.
                sys.stderr.write("approval signer batch failed; will retry\n")
    finally:
        signer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
