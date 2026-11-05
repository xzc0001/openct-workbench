"""Verified, digest-addressed engine/model resources served to approved nodes."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .protocol import FileBody, ProtocolError, Response, canonical_json, require_sha256, strict_json_loads


MANIFEST_LIMIT = 32 * 1024 * 1024
MAX_RESOURCE_FILES = 100_000
MAX_RESOURCE_BYTES = 80 * 1024 * 1024 * 1024
MAX_RESOURCE_FILE_BYTES = 8 * 1024 * 1024 * 1024
FILE_ROLE = {"runtime", "runner", "source", "model"}


def _load_crypto():
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
        return hashes, serialization, padding, rsa
    except ImportError:
        raise RuntimeError("cryptography is required to verify signed resource manifests") from None


def _sha_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _safe_relative(raw: object) -> str:
    if not isinstance(raw, str) or not raw or "\\" in raw or "\x00" in raw or len(raw) > 512:
        raise ValueError("resource manifest path is invalid")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts) or ":" in path.parts[0]:
        raise ValueError("resource manifest path is invalid")
    normalized = path.as_posix()
    if normalized != raw:
        raise ValueError("resource manifest path is not canonical")
    return normalized


class ResourceCatalog:
    """Config values contain manifest/signature/root plus a pinned public_key_pem."""

    def __init__(self, catalog: dict | None, *, allow_unsigned: bool = False):
        self.entries = {}
        self.files = {}
        if not catalog:
            return
        if set(catalog) - {"engine", "model"}:
            raise ValueError("resource catalog has an unknown kind")
        loaded = {}
        for kind, config in catalog.items():
            signed = isinstance(config, dict) and set(config) == {"manifest", "signature", "root", "public_key_pem"}
            unsigned = isinstance(config, dict) and set(config) == {"manifest", "root"} and allow_unsigned
            if not signed and not unsigned:
                raise ValueError(f"invalid {kind} resource catalog")
            public_key = config["public_key_pem"].encode("ascii") if signed and isinstance(config["public_key_pem"], str) else config.get("public_key_pem", b"")
            identity = (str(Path(config["manifest"]).resolve()), str(Path(config["signature"]).resolve()) if signed else "",
                        str(Path(config["root"]).resolve()), hashlib.sha256(public_key).hexdigest())
            if identity in loaded:
                self.entries[kind] = loaded[identity]
            else:
                entry = self._load_entry(kind, config)
                self.entries[kind] = entry
                loaded[identity] = entry

    def _load_entry(self, kind: str, config: dict) -> dict:
        manifest_source = Path(config["manifest"])
        signed = "signature" in config
        signature_source = Path(config["signature"]) if signed else None
        root_source = Path(config["root"])
        paths = (manifest_source, signature_source, root_source) if signed else (manifest_source, root_source)
        if any(path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)()) for path in paths):
            raise ValueError("resource catalog paths must be ordinary files and directories")
        manifest_path = manifest_source.resolve(strict=True)
        signature_path = signature_source.resolve(strict=True) if signed else None
        root = root_source.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("resource catalog paths must be ordinary files and directories")
        raw = manifest_path.read_bytes()
        signature = signature_path.read_bytes() if signed else None
        if len(raw) > MANIFEST_LIMIT or (signed and len(signature) != 384):
            raise ValueError("resource manifest or signature size is invalid")
        manifest = strict_json_loads(raw, MANIFEST_LIMIT)
        if raw not in {canonical_json(manifest), canonical_json(manifest) + b"\n"}:
            raise ValueError("resource manifest is not canonical JSON")
        required = {"schema", "engine_id", "engine_version", "created_utc", "expires_utc", "platform", "runtime", "canonical_source_bundle_sha256", "model_set_sha256", "total_bytes", "max_expanded_bytes", "files", "models"}
        if not isinstance(manifest, dict) or set(manifest) != required or type(manifest["schema"]) is not int or manifest["schema"] != 1:
            raise ValueError("resource manifest schema is invalid")
        if any(not isinstance(manifest[field], str) or not manifest[field] or len(manifest[field]) > 256 for field in ("engine_id", "engine_version", "created_utc", "expires_utc")):
            raise ValueError("resource manifest identity fields are invalid")
        if any(isinstance(manifest[field], bool) or not isinstance(manifest[field], int) for field in ("total_bytes", "max_expanded_bytes")):
            raise ValueError("resource manifest byte totals are invalid")
        try:
            created = datetime.fromisoformat(manifest["created_utc"].replace("Z", "+00:00"))
            expires = datetime.fromisoformat(manifest["expires_utc"].replace("Z", "+00:00"))
            if created.tzinfo is None or expires.tzinfo is None or expires <= datetime.now(timezone.utc) or created > datetime.now(timezone.utc):
                raise ValueError("resource manifest is expired or not yet valid")
        except (AttributeError, TypeError, ValueError):
            raise ValueError("resource manifest validity interval is invalid") from None
        _ = require_sha256(manifest["canonical_source_bundle_sha256"], "canonical source bundle SHA256")
        _ = require_sha256(manifest["model_set_sha256"], "model set SHA256")
        if signed:
            hashes, serialization, padding, rsa = _load_crypto()
            public_key_pem = config["public_key_pem"]
            if isinstance(public_key_pem, str):
                public_key_pem = public_key_pem.encode("ascii")
            public = serialization.load_pem_public_key(public_key_pem)
            if not isinstance(public, rsa.RSAPublicKey) or public.key_size != 3072:
                raise ValueError("resource signer must be a pinned RSA-3072 public key")
            public.verify(signature, raw, padding.PKCS1v15(), hashes.SHA256())
        files = manifest["files"]
        models = manifest["models"]
        if not isinstance(files, list) or not 1 <= len(files) <= MAX_RESOURCE_FILES or not isinstance(models, list) or len(models) > 512:
            raise ValueError("resource manifest file count is invalid")
        previous = ""
        seen_casefold = set()
        by_sha = {}
        total = 0
        for item in files:
            if not isinstance(item, dict) or set(item) != {"path", "bytes", "sha256", "role"}:
                raise ValueError("resource manifest file entry is invalid")
            relative = _safe_relative(item["path"])
            folded = relative.casefold()
            if relative <= previous or folded in seen_casefold:
                raise ValueError("resource manifest files are not uniquely sorted")
            previous = relative
            seen_casefold.add(folded)
            digest = require_sha256(item["sha256"], "resource file SHA256")
            size = item["bytes"]
            if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= MAX_RESOURCE_FILE_BYTES or item["role"] not in FILE_ROLE:
                raise ValueError("resource manifest file metadata is invalid")
            target = root.joinpath(*PurePosixPath(relative).parts)
            resolved = target.resolve(strict=True)
            if not resolved.is_relative_to(root) or not resolved.is_file():
                raise ValueError("resource path escapes its configured root")
            cursor = root
            for part in PurePosixPath(relative).parts:
                cursor = cursor / part
                if cursor.is_symlink() or bool(getattr(cursor, "is_junction", lambda: False)()):
                    raise ValueError("resource manifest may not contain links")
            stat = resolved.stat()
            if stat.st_size != size or _sha_file(resolved) != digest:
                raise ValueError("resource file differs from its signed manifest")
            total += size
            by_sha.setdefault(digest, resolved)
        if total != manifest["total_bytes"] or total > MAX_RESOURCE_BYTES or not total <= manifest["max_expanded_bytes"] <= MAX_RESOURCE_BYTES:
            raise ValueError("resource manifest byte totals are invalid")
        sorted_models = sorted(models, key=lambda row: row.get("id", "") if isinstance(row, dict) else "")
        if models != sorted_models:
            raise ValueError("resource models are not sorted")
        seen_models = set()
        model_paths = {item["path"] for item in files}
        file_by_path = {item["path"]: item for item in files}
        for model in models:
            if not isinstance(model, dict) or set(model) != {"id", "name", "sha256", "files"} or not isinstance(model["id"], str) or not model["id"] or model["id"] in seen_models or not isinstance(model["files"], list) or not isinstance(model["name"], str) or not re.fullmatch(r"Dataset\d+_[A-Za-z0-9_.-]+", model["name"]):
                raise ValueError("resource model entry is invalid")
            require_sha256(model["sha256"], "model SHA256")
            expected_id = hashlib.sha256(("totalsegmentator-model-v1:" + model["name"]).encode("utf-8")).hexdigest()[:32]
            if model["id"] != expected_id or model["files"] != sorted(model["files"]) or any(path not in model_paths for path in model["files"]):
                raise ValueError("resource model file list is invalid")
            hash_rows = [{"path": path, "bytes": file_by_path[path]["bytes"], "sha256": file_by_path[path]["sha256"]} for path in model["files"]]
            if hashlib.sha256(canonical_json(hash_rows)).hexdigest() != model["sha256"]:
                raise ValueError("resource model content digest mismatch")
            seen_models.add(model["id"])
        model_set = [{"id": row["id"], "sha256": row["sha256"], "files": row["files"]} for row in sorted_models]
        if hashlib.sha256(canonical_json(model_set)).hexdigest() != manifest["model_set_sha256"]:
            raise ValueError("resource model set digest mismatch")
        digest = hashlib.sha256(raw).hexdigest()
        for file_digest, file_path in by_sha.items():
            prior = self.files.get(file_digest)
            if prior and prior != file_path:
                if _sha_file(prior) != file_digest:
                    raise ValueError("resource digest collision or changed file")
            self.files[file_digest] = file_path
        return {"kind": kind, "manifest": manifest, "raw": raw, "signature": signature, "sha256": digest, "root": root, "files": by_sha}

    def response(self, kind: str, asset: str, range_header: str | None = None) -> Response:
        entry = self.entries.get(kind)
        if not entry:
            raise ProtocolError(404, "resource_not_found")
        if asset == "manifest":
            data = entry["raw"]
            return Response(200, {"Content-Type": "application/json", "Content-Length": str(len(data)), "Cache-Control": "no-store", "ETag": f'"{entry["sha256"]}"'}, data)
        if asset == "manifest.sig":
            data = entry["signature"]
            if data is None:
                raise ProtocolError(404, "resource_not_found")
            return Response(200, {"Content-Type": "application/octet-stream", "Content-Length": str(len(data)), "Cache-Control": "no-store", "ETag": f'"{hashlib.sha256(data).hexdigest()}"'}, data)
        if not asset.startswith("file/"):
            raise ProtocolError(404, "resource_not_found")
        digest = asset.removeprefix("file/")
        require_sha256(digest, "resource file SHA256")
        path = entry["files"].get(digest)
        if not path:
            raise ProtocolError(404, "resource_not_found")
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_RESOURCE_FILE_BYTES or _sha_file(path) != digest:
            raise ProtocolError(409, "resource_changed")
        length = path.stat().st_size
        status, start, end = 200, 0, length - 1
        headers = {"Content-Type": "application/octet-stream", "Accept-Ranges": "bytes", "Cache-Control": "no-store", "ETag": f'"{digest}"'}
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if not match or "," in range_header or not any(match.groups()):
                return Response(416, {"Content-Length": "0", "Content-Range": f"bytes */{length}"}, b"")
            first, last = match.groups()
            if not first:
                suffix = int(last)
                if suffix <= 0:
                    return Response(416, {"Content-Length": "0", "Content-Range": f"bytes */{length}"}, b"")
                start = max(0, length - suffix)
            else:
                start = int(first)
                end = min(length - 1, int(last)) if last else length - 1
            if start >= length or end < start:
                return Response(416, {"Content-Length": "0", "Content-Range": f"bytes */{length}"}, b"")
            status = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{length}"
        size = end - start + 1
        headers["Content-Length"] = str(size)
        return Response(status, headers, FileBody(str(path), start, size))
