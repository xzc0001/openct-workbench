"""Closed, bounded wire-contract helpers for reconstruction cluster v1."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any


API_PREFIX = "/api/cluster/v1"
PROTOCOL_VERSION = 1
JSON_BODY_LIMIT = 1024 * 1024
ID_RE = re.compile(r"^[0-9a-f]{32}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
STAGE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
LABEL_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

OUTPUT_TYPES = {"nifti_mask", "mesh_json", "candidates_json", "inference_json"}
INPUT_TYPES = OUTPUT_TYPES | {"nifti_ct", "geometry_json", "instances_json"}
JSON_OUTPUT_TYPES = {"mesh_json", "candidates_json", "inference_json"}
ARTIFACT_LIMITS = {
    "nifti_mask": 512 * 1024 * 1024,
    "mesh_json": 256 * 1024 * 1024,
    "candidates_json": 16 * 1024 * 1024,
    "inference_json": 4 * 1024 * 1024,
}
INPUT_LIMITS = {
    "nifti_ct": 512 * 1024 * 1024,
    "nifti_mask": 512 * 1024 * 1024,
    "geometry_json": 256 * 1024,
    "instances_json": 4 * 1024 * 1024,
    "mesh_json": 256 * 1024 * 1024,
    "candidates_json": 16 * 1024 * 1024,
    "inference_json": 4 * 1024 * 1024,
}

BUILTIN_ROLES = {"ct", "native_ct", "geometry", "native_instances"}
RESOURCE_KINDS = {"engine", "model"}


class ProtocolError(ValueError):
    """A safe error suitable for an API response (contains no patient data)."""

    def __init__(self, status: int, code: str, message: str | None = None):
        super().__init__(message or code)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class FileBody:
    path: str
    offset: int
    length: int
    chunk_size: int = 1024 * 1024

    def iter_chunks(self):
        with open(self.path, "rb") as stream:
            stream.seek(self.offset)
            remaining = self.length
            while remaining:
                chunk = stream.read(min(self.chunk_size, remaining))
                if not chunk:
                    raise OSError("stream file shortened during response")
                remaining -= len(chunk)
                yield chunk


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes | FileBody

    @classmethod
    def json(cls, status: int, value: Any, headers: dict[str, str] | None = None) -> "Response":
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        base = {"Content-Type": "application/json; charset=utf-8", "Content-Length": str(len(payload)), "Cache-Control": "no-store"}
        if headers:
            base.update(headers)
        return cls(status, base, payload)


def require_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ProtocolError(400, "invalid_id", f"{field} must be 32 lowercase hexadecimal characters")
    return value


def require_sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ProtocolError(400, "invalid_sha256", f"{field} must be a lowercase SHA-256 digest")
    return value


def strict_json_loads(data: bytes | str, max_bytes: int = JSON_BODY_LIMIT) -> Any:
    if isinstance(data, bytes):
        if len(data) > max_bytes:
            raise ProtocolError(413, "body_too_large")
        try:
            data = data.decode("utf-8")
        except UnicodeDecodeError:
            raise ProtocolError(400, "invalid_json") from None
    elif len(data.encode("utf-8")) > max_bytes:
        raise ProtocolError(413, "body_too_large")

    def pairs(rows):
        out = {}
        for key, value in rows:
            if key in out:
                raise ProtocolError(400, "duplicate_json_key")
            out[key] = value
        return out

    def bad_constant(_):
        raise ProtocolError(400, "non_finite_number")

    try:
        return json.loads(data, object_pairs_hook=pairs, parse_constant=bad_constant)
    except ProtocolError:
        raise
    except (ValueError, TypeError, RecursionError):
        raise ProtocolError(400, "invalid_json") from None


def canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError):
        raise ProtocolError(400, "invalid_json_value") from None


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def validate_role(role: Any, artifact_type: str, *, output: bool = False) -> str:
    if not isinstance(role, str) or len(role) > 192:
        raise ProtocolError(400, "invalid_role")
    accepted = False
    if role in BUILTIN_ROLES:
        accepted = artifact_type == {"ct": "nifti_ct", "native_ct": "nifti_ct", "geometry": "geometry_json", "native_instances": "instances_json"}[role]
    elif role == "surface_export":
        accepted = artifact_type == "mesh_json"
    elif role == "candidate_review":
        accepted = artifact_type == "candidates_json"
    elif role.startswith("prediction."):
        parts = role.split(".")
        accepted = len(parts) == 3 and STAGE_RE.fullmatch(parts[1]) is not None and LABEL_RE.fullmatch(parts[2]) is not None and artifact_type == "nifti_mask"
    elif role.startswith("inference."):
        parts = role.split(".")
        accepted = len(parts) == 2 and STAGE_RE.fullmatch(parts[1]) is not None and artifact_type == "inference_json"
    elif role.startswith("candidate_mask."):
        candidate_id = role.removeprefix("candidate_mask.")
        accepted = ID_RE.fullmatch(candidate_id) is not None and artifact_type == "nifti_mask"
    if output and artifact_type not in OUTPUT_TYPES:
        accepted = False
    if not accepted:
        raise ProtocolError(400, "role_type_mismatch")
    return role


def require_mapping(value: Any, field: str, allowed: set[str], required: set[str] = frozenset()) -> dict:
    if not isinstance(value, dict) or set(value) - allowed or required - set(value):
        raise ProtocolError(400, f"invalid_{field}")
    return value


def require_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ProtocolError(400, f"invalid_{field}")
    return value


def require_finite(value: Any, field: str, minimum: float | None = None, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ProtocolError(400, f"invalid_{field}")
    result = float(value)
    if minimum is not None and result < minimum or maximum is not None and result > maximum:
        raise ProtocolError(400, f"invalid_{field}")
    return result


def normalize_path(path: str) -> str:
    if not isinstance(path, str) or "?" in path or "#" in path or "\\" in path or "%" in path:
        raise ProtocolError(404, "not_found")
    if not path.startswith(API_PREFIX + "/"):
        raise ProtocolError(404, "not_found")
    return path[len(API_PREFIX):]


def validate_capabilities(value: Any) -> dict:
    if not isinstance(value, dict) or set(value) != {"engine", "models", "resources", "platform", "state"}:
        raise ProtocolError(400, "invalid_capabilities")
    engine = value.get("engine")
    models = value.get("models")
    resources = value.get("resources")
    platform = value.get("platform")
    state = value.get("state")
    if state not in {"ready", "paused", "draining", "unsupported", "error"}:
        raise ProtocolError(400, "invalid_capabilities")
    if not isinstance(engine, dict) or set(engine) != {"id", "sha256"} or not isinstance(engine["id"], str) or not 1 <= len(engine["id"]) <= 80:
        raise ProtocolError(400, "invalid_capabilities")
    require_sha256(engine["sha256"], "engine.sha256")
    if not isinstance(models, list) or len(models) > 512:
        raise ProtocolError(400, "invalid_capabilities")
    normalized_models = []
    seen = set()
    for model in models:
        if not isinstance(model, dict) or set(model) not in ({"id", "sha256"}, {"id", "name", "sha256", "files"}) or not isinstance(model["id"], str) or not 1 <= len(model["id"]) <= 128:
            raise ProtocolError(400, "invalid_capabilities")
        require_sha256(model["sha256"], "model.sha256")
        if model["id"] in seen:
            raise ProtocolError(400, "invalid_capabilities")
        if "name" in model:
            name = model["name"]
            files = model["files"]
            if (not isinstance(name, str) or not re.fullmatch(r"Dataset\d+_[A-Za-z0-9_.-]+", name) or
                    model["id"] != hashlib.sha256(("totalsegmentator-model-v1:" + name).encode("utf-8")).hexdigest()[:32] or
                    not isinstance(files, list) or not 1 <= len(files) <= 100_000):
                raise ProtocolError(400, "invalid_capabilities")
            previous = ""
            for relative in files:
                if not isinstance(relative, str) or not relative or len(relative) > 512 or "\\" in relative or "\x00" in relative:
                    raise ProtocolError(400, "invalid_capabilities")
                path = PurePosixPath(relative)
                if (path.is_absolute() or path.as_posix() != relative or
                        any(part in {"", ".", ".."} for part in path.parts) or
                        ":" in path.parts[0] or relative <= previous):
                    raise ProtocolError(400, "invalid_capabilities")
                previous = relative
        seen.add(model["id"])
        normalized_models.append({"id": model["id"], "sha256": model["sha256"]})
    if not isinstance(resources, dict) or set(resources) - {"threads", "memory_bytes", "cpu_percent", "free_disk_bytes"} or not {"threads", "memory_bytes", "cpu_percent"} <= set(resources):
        raise ProtocolError(400, "invalid_capabilities")
    result_resources = {
        "threads": require_int(resources["threads"], "threads", 1, 1024),
        "memory_bytes": require_int(resources["memory_bytes"], "memory_bytes", 1 if state == "ready" else 0, 2**63 - 1),
        "cpu_percent": require_int(resources["cpu_percent"], "cpu_percent", 1, 100),
    }
    if "free_disk_bytes" in resources:
        result_resources["free_disk_bytes"] = require_int(resources["free_disk_bytes"], "free_disk_bytes", 0, 2**63 - 1)
    if not isinstance(platform, dict) or set(platform) - {"os", "arch", "version"}:
        raise ProtocolError(400, "invalid_capabilities")
    for key, val in platform.items():
        if not isinstance(val, str) or len(val) > 80 or any(ord(ch) < 32 for ch in val):
            raise ProtocolError(400, "invalid_capabilities")
    return {"engine": {"id": engine["id"], "sha256": engine["sha256"]}, "models": sorted(normalized_models, key=lambda item: item["id"]), "resources": result_resources, "platform": platform, "state": state}
