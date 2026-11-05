"""Bounded validation of node-supplied reconstruction artifacts."""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import math
import os
import re
import struct
import zlib
from pathlib import Path
from typing import Any

from .protocol import (
    ARTIFACT_LIMITS,
    JSON_OUTPUT_TYPES,
    ProtocolError,
    canonical_json,
    require_id,
    require_int,
    require_sha256,
    strict_json_loads,
    validate_role,
)


CORE_GEOMETRY = {"dimensions_xyz", "spacing_xyz_mm", "origin_lps_mm", "direction"}
GEOMETRY_EXTRA = {"coordinate_system", "dtype", "units", "original_geometry", "slice_count", "resampled_for_preview"}
SENSITIVE_KEYS = {"patient", "patient_name", "patient_id", "studyinstanceuid", "seriesinstanceuid", "sopinstanceuid", "accessionnumber", "mask_file", "thumbnail_file", "path", "filepath", "url", "script", "command", "argv", "executable"}
INTEGER_NIFTI = {2: ("B", 1), 256: ("b", 1), 4: ("h", 2), 512: ("H", 2), 8: ("i", 4), 768: ("I", 4), 1024: ("q", 8), 1280: ("Q", 8)}


def _json_safe(value: Any, *, max_nodes: int = 4_000_000, max_depth: int = 64) -> None:
    stack = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > max_nodes or depth > max_depth:
            raise ProtocolError(422, "artifact_complexity_limit")
        if isinstance(current, dict):
            for key, item in current.items():
                if not isinstance(key, str) or key.casefold() in SENSITIVE_KEYS:
                    raise ProtocolError(422, "artifact_forbidden_field")
                stack.append((item, depth + 1))
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
        elif isinstance(current, float):
            if not math.isfinite(current):
                raise ProtocolError(422, "artifact_non_finite")
        elif isinstance(current, int):
            if current < -(2**63) or current > 2**64 - 1:
                raise ProtocolError(422, "artifact_integer_range")
        elif isinstance(current, str):
            if len(current) > 1024 * 1024 or any(ord(char) < 32 and char not in "\t\n\r" for char in current):
                raise ProtocolError(422, "artifact_string_limit")
            folded = current.casefold()
            if folded.startswith(("http://", "https://", "file://", "javascript:")) or re.match(r"^(?:[a-z]:[\\/]|/|\\\\)", current):
                raise ProtocolError(422, "artifact_path_forbidden")
        elif current is None or isinstance(current, bool):
            continue
        else:
            raise ProtocolError(422, "artifact_value_type")


def _numeric_array(value: Any, name: str, *, integer: bool = False, max_items: int = 40_000_000) -> list:
    if not isinstance(value, list) or len(value) > max_items:
        raise ProtocolError(422, "artifact_array_limit")
    for item in value:
        if integer:
            if isinstance(item, bool) or not isinstance(item, int) or item < 0 or item >= 2**32:
                raise ProtocolError(422, f"artifact_invalid_{name}")
        elif isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item):
            raise ProtocolError(422, f"artifact_invalid_{name}")
    return value


def _mesh_json_safe(value: Any, *, key: str = "", depth: int = 0, nodes: list[int] | None = None) -> None:
    """Validate metadata while allowing only the typed base64 mesh/mask arrays."""
    nodes = [0] if nodes is None else nodes
    nodes[0] += 1
    if nodes[0] > 4_000_000 or depth > 64:
        raise ProtocolError(422, "artifact_complexity_limit")
    if key in {"positions", "indices", "normals", "data"}:
        if not isinstance(value, str):
            raise ProtocolError(422, "mesh_binary_field_invalid")
        return
    if isinstance(value, dict):
        for field, child in value.items():
            if not isinstance(field, str) or field.casefold() in SENSITIVE_KEYS:
                raise ProtocolError(422, "artifact_forbidden_field")
            _mesh_json_safe(child, key=field, depth=depth + 1, nodes=nodes)
    elif isinstance(value, list):
        for child in value:
            _mesh_json_safe(child, key=key, depth=depth + 1, nodes=nodes)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ProtocolError(422, "artifact_non_finite")
    elif isinstance(value, int):
        if value < -(2**63) or value > 2**64 - 1:
            raise ProtocolError(422, "artifact_integer_range")
    elif isinstance(value, str):
        if len(value) > 1024 * 1024 or any(ord(char) < 32 and char not in "\t\n\r" for char in value):
            raise ProtocolError(422, "artifact_string_limit")
        folded = value.casefold()
        if folded.startswith(("http://", "https://", "file://", "javascript:")) or re.match(r"^(?:[a-z]:[\\/]|/|\\\\)", value):
            raise ProtocolError(422, "artifact_path_forbidden")
    elif value is None or isinstance(value, bool):
        return
    else:
        raise ProtocolError(422, "artifact_value_type")


def _decode_mesh_array(value: str, expected_bytes: int, encoding: str | None) -> bytes:
    if expected_bytes <= 0 or expected_bytes > 256 * 1024 * 1024:
        raise ProtocolError(422, "mesh_array_limit")
    try:
        encoded = base64.b64decode(value, validate=True)
        if encoding is None:
            raw = encoded
        elif encoding == "gzip":
            inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
            raw = inflater.decompress(encoded, expected_bytes + 1)
            raw += inflater.flush(max(1, expected_bytes + 1 - len(raw)))
            if inflater.unused_data or inflater.unconsumed_tail:
                raise ValueError
        else:
            raise ValueError
    except (ValueError, zlib.error):
        raise ProtocolError(422, "mesh_array_invalid") from None
    if len(raw) != expected_bytes:
        raise ProtocolError(422, "mesh_array_length_invalid")
    return raw


def _validate_geometry(value: Any, expected: dict, *, require_ct: bool = True) -> dict:
    if not isinstance(value, dict) or set(value) - (CORE_GEOMETRY | GEOMETRY_EXTRA) or not CORE_GEOMETRY <= set(value):
        raise ProtocolError(422, "artifact_invalid_geometry")
    dimensions = value["dimensions_xyz"]
    spacing = value["spacing_xyz_mm"]
    origin = value["origin_lps_mm"]
    direction = value["direction"]
    if not isinstance(dimensions, list) or len(dimensions) != 3 or any(isinstance(x, bool) or not isinstance(x, int) or not 1 <= x <= 65536 for x in dimensions):
        raise ProtocolError(422, "artifact_invalid_geometry")
    if not isinstance(spacing, list) or len(spacing) != 3 or not isinstance(origin, list) or len(origin) != 3 or not isinstance(direction, list) or len(direction) != 9:
        raise ProtocolError(422, "artifact_invalid_geometry")
    for number in spacing + origin + direction:
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
            raise ProtocolError(422, "artifact_invalid_geometry")
    if any(float(x) <= 0 for x in spacing) or max(dimensions) > 65536:
        raise ProtocolError(422, "artifact_invalid_geometry")
    if require_ct:
        if value.get("coordinate_system") not in (None, "LPS"):
            raise ProtocolError(422, "artifact_geometry_mismatch")
        if not isinstance(expected, dict) or not CORE_GEOMETRY <= set(expected):
            raise ProtocolError(500, "server_geometry_invalid")
        if dimensions != expected["dimensions_xyz"]:
            raise ProtocolError(422, "artifact_geometry_mismatch")
        for key in ("spacing_xyz_mm", "origin_lps_mm", "direction"):
            left, right = value[key], expected[key]
            if len(left) != len(right) or any(abs(float(a) - float(b)) > 1e-6 for a, b in zip(left, right)):
                raise ProtocolError(422, "artifact_geometry_mismatch")
    return value


def _nifti_float32_tolerance(expected: float) -> float:
    """Bound NIfTI-1 field drift to its actual float32 serialization error."""
    try:
        encoded = struct.unpack("<f", struct.pack("<f", float(expected)))[0]
    except (OverflowError, struct.error):
        raise ProtocolError(422, "nifti_geometry_invalid") from None
    return max(1e-6, abs(float(expected) - encoded) + 1e-6)


def _validate_nifti_geometry(value: Any, expected: dict) -> dict:
    geometry = _validate_geometry(value, expected, require_ct=False)
    if not isinstance(expected, dict) or not CORE_GEOMETRY <= set(expected):
        raise ProtocolError(500, "server_geometry_invalid")
    if geometry["dimensions_xyz"] != expected["dimensions_xyz"]:
        raise ProtocolError(422, "artifact_geometry_mismatch")
    for field in ("spacing_xyz_mm", "origin_lps_mm"):
        if any(abs(float(actual) - float(wanted)) > _nifti_float32_tolerance(wanted)
               for actual, wanted in zip(geometry[field], expected[field])):
            raise ProtocolError(422, "artifact_geometry_mismatch")
    if any(abs(float(actual) - float(wanted)) > 1e-6
           for actual, wanted in zip(geometry["direction"], expected["direction"])):
        raise ProtocolError(422, "artifact_geometry_mismatch")
    return geometry


def _validate_cropped_geometry(value: Any, canonical: dict) -> dict:
    geometry = _validate_geometry(value, canonical, require_ct=False)
    if geometry.get("coordinate_system") not in (None, "LPS"):
        raise ProtocolError(422, "artifact_geometry_mismatch")
    if any(abs(float(a) - float(b)) > 1e-6 for a, b in zip(geometry["direction"], canonical["direction"])):
        raise ProtocolError(422, "artifact_geometry_mismatch")
    if any(float(step) > 10 for step in geometry["spacing_xyz_mm"]):
        raise ProtocolError(422, "artifact_geometry_mismatch")
    # Candidate masks may be cropped and resampled. Their physical corners must
    # still lie inside the source CT's oriented physical field of view.
    dims = geometry["dimensions_xyz"]
    spacing = geometry["spacing_xyz_mm"]
    direction = geometry["direction"]
    c_dims = canonical["dimensions_xyz"]
    c_spacing = canonical["spacing_xyz_mm"]
    c_direction = canonical["direction"]
    c_origin = canonical["origin_lps_mm"]
    origin = geometry["origin_lps_mm"]
    for ix in (0, dims[0] - 1):
        for iy in (0, dims[1] - 1):
            for iz in (0, dims[2] - 1):
                index = (ix * spacing[0], iy * spacing[1], iz * spacing[2])
                point = [origin[row] + sum(direction[row * 3 + col] * index[col] for col in range(3)) for row in range(3)]
                delta = [point[row] - c_origin[row] for row in range(3)]
                canonical_index = [sum(c_direction[row * 3 + axis] * delta[row] for row in range(3)) / c_spacing[axis] for axis in range(3)]
                if any(canonical_index[axis] < -0.51 or canonical_index[axis] > c_dims[axis] - 0.49 for axis in range(3)):
                    raise ProtocolError(422, "artifact_geometry_outside_input")
    return geometry


def _iter_json_objects(value: Any):
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            yield item
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)


def _validate_mesh(data: bytes, expected_geometry: dict) -> dict:
    value = strict_json_loads(data, ARTIFACT_LIMITS["mesh_json"])
    _mesh_json_safe(value)
    if not isinstance(value, dict) or value.get("schema") != 1:
        raise ProtocolError(422, "mesh_schema_invalid")
    required = {"schema", "selected", "models", "masks", "nodules", "missing", "canonical_geometry", "native_geometry"}
    if set(value) != required:
        raise ProtocolError(422, "mesh_schema_invalid")
    if not isinstance(value["selected"], list) or not isinstance(value["models"], list) or not isinstance(value["masks"], list) or not isinstance(value["nodules"], list) or not isinstance(value["missing"], list):
        raise ProtocolError(422, "mesh_schema_invalid")
    _validate_geometry(value["canonical_geometry"], expected_geometry, require_ct=True)
    if canonical_json(value["canonical_geometry"]) != canonical_json(expected_geometry):
        raise ProtocolError(422, "mesh_geometry_mismatch")
    if not isinstance(value["native_geometry"], dict):
        raise ProtocolError(422, "mesh_schema_invalid")
    native_expected = expected_geometry.get("original_geometry")
    if native_expected is not None and canonical_json(value["native_geometry"]) != canonical_json(native_expected):
        raise ProtocolError(422, "mesh_native_geometry_mismatch")
    if len(value["models"]) > 10000:
        raise ProtocolError(422, "mesh_model_limit")
    for model in value["models"]:
        if not isinstance(model, dict) or "mask_file" in model or not {"id", "positions", "indices", "normals", "vertices", "triangles"} <= set(model):
            raise ProtocolError(422, "mesh_model_schema_invalid")
        vertices, triangles = model["vertices"], model["triangles"]
        if isinstance(vertices, bool) or not isinstance(vertices, int) or not 1 <= vertices <= 5_000_000 or isinstance(triangles, bool) or not isinstance(triangles, int) or not 1 <= triangles <= 10_000_000:
            raise ProtocolError(422, "mesh_array_limit")
        encoding = model.get("encoding")
        positions = _decode_mesh_array(model["positions"], vertices * 12, encoding)
        normals = _decode_mesh_array(model["normals"], vertices * 12, encoding)
        indices = _decode_mesh_array(model["indices"], triangles * 12, encoding)
        for (number,) in struct.iter_unpack("<f", positions):
            if not math.isfinite(number):
                raise ProtocolError(422, "mesh_non_finite")
        for (number,) in struct.iter_unpack("<f", normals):
            if not math.isfinite(number):
                raise ProtocolError(422, "mesh_non_finite")
        for (index,) in struct.iter_unpack("<I", indices):
            if index >= vertices:
                raise ProtocolError(422, "mesh_indices_invalid")
        if "bounds_ras" in model:
            bounds = model["bounds_ras"]
            if not isinstance(bounds, list) or len(bounds) != 6 or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in bounds):
                raise ProtocolError(422, "mesh_bounds_invalid")
    for mask in value["masks"]:
        if not isinstance(mask, dict) or set(mask) != {"id", "start", "size", "data"}:
            raise ProtocolError(422, "mesh_mask_invalid")
        if not isinstance(mask["id"], str) or not isinstance(mask["start"], list) or len(mask["start"]) != 3 or not isinstance(mask["size"], list) or len(mask["size"]) != 3 or any(isinstance(x, bool) or not isinstance(x, int) or x < 0 for x in mask["start"] + mask["size"]):
            raise ProtocolError(422, "mesh_mask_invalid")
        if not isinstance(mask["data"], str) or len(mask["data"]) > 32 * 1024 * 1024:
            raise ProtocolError(422, "mesh_mask_invalid")
        expected_bits = (math.prod(mask["size"]) + 7) // 8
        if not 0 < expected_bits <= 64 * 1024 * 1024:
            raise ProtocolError(422, "mesh_mask_invalid")
        try:
            packed = base64.b64decode(mask["data"], validate=True)
            inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
            raw = inflater.decompress(packed, expected_bits + 1)
            raw += inflater.flush(max(0, expected_bits + 1 - len(raw)))
        except (ValueError, zlib.error):
            raise ProtocolError(422, "mesh_mask_invalid") from None
        if len(raw) != expected_bits or inflater.unused_data or inflater.unconsumed_tail:
            raise ProtocolError(422, "mesh_mask_invalid")
        if any(mask["start"][axis] + mask["size"][axis] > expected_geometry["dimensions_xyz"][axis] for axis in range(3)):
            raise ProtocolError(422, "mesh_mask_bounds_invalid")
    return value


def _validate_candidates(data: bytes) -> dict:
    value = strict_json_loads(data, ARTIFACT_LIMITS["candidates_json"])
    _json_safe(value)
    if not isinstance(value, dict) or set(value) - {"version", "source_ct_sha256", "parameters", "candidates", "seconds", "method", "limitations", "identity", "total_components_at_least_4_voxels", "listed_largest_candidates"}:
        raise ProtocolError(422, "candidate_schema_invalid")
    if "source_ct_sha256" in value:
        require_sha256(value["source_ct_sha256"], "source_ct_sha256")
    rows = value.get("candidates", value.get("listed_largest_candidates", []))
    if not isinstance(rows, list) or len(rows) > 1000:
        raise ProtocolError(422, "candidate_schema_invalid")
    seen_ids = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or len(row["id"]) > 128:
            raise ProtocolError(422, "candidate_schema_invalid")
        if row["id"] in seen_ids:
            raise ProtocolError(422, "candidate_schema_invalid")
        seen_ids.add(row["id"])
        role = row.get("mask_role")
        if role is not None:
            if not isinstance(role, str) or not role.startswith("candidate_mask."):
                raise ProtocolError(422, "candidate_mask_role_invalid")
            require_id(role.removeprefix("candidate_mask."), "candidate mask artifact ID")
    identity = value.get("identity")
    if identity is not None:
        if not isinstance(identity, dict) or len(identity) > 10000:
            raise ProtocolError(422, "candidate_identity_invalid")
        for fingerprint, candidate_id in identity.items():
            require_sha256(fingerprint, "candidate identity fingerprint")
            if not isinstance(candidate_id, str) or not re.fullmatch(r"suspect_[0-9]{3,6}", candidate_id):
                raise ProtocolError(422, "candidate_identity_invalid")
    return value


def _validate_inference(data: bytes, task_key: str) -> dict:
    value = strict_json_loads(data, ARTIFACT_LIMITS["inference_json"])
    _json_safe(value)
    if not isinstance(value, dict) or set(value) != {"device", "offline_guard", "tasks"} or value["device"] != "cpu" or value["offline_guard"] is not True or not isinstance(value["tasks"], list) or len(value["tasks"]) != 1:
        raise ProtocolError(422, "inference_schema_invalid")
    row = value["tasks"][0]
    if not isinstance(row, dict) or set(row) - {"task", "key", "seconds", "cache", "resolution_mm"} or row.get("key") != task_key:
        raise ProtocolError(422, "inference_schema_invalid")
    if "seconds" in row and (isinstance(row["seconds"], bool) or not isinstance(row["seconds"], (int, float)) or not math.isfinite(row["seconds"]) or row["seconds"] < 0):
        raise ProtocolError(422, "inference_schema_invalid")
    if "resolution_mm" in row and (isinstance(row["resolution_mm"], bool) or not isinstance(row["resolution_mm"], (int, float)) or not math.isfinite(row["resolution_mm"]) or row["resolution_mm"] <= 0):
        raise ProtocolError(422, "inference_schema_invalid")
    return value


def _nifti_stream(path: Path, expected_geometry: dict, labels: list[int]) -> None:
    if not labels or len(labels) > 256 or any(isinstance(item, bool) or not isinstance(item, int) or not 0 <= item <= 65535 for item in labels):
        raise ProtocolError(422, "mask_labels_invalid")
    label_set = set(labels) | {0}
    raw = None
    try:
        raw = open(path, "rb")
        prefix = raw.read(2)
        raw.seek(0)
        stream = gzip.GzipFile(fileobj=raw, mode="rb") if prefix == b"\x1f\x8b" else raw
        header = stream.read(348)
        if len(header) != 348:
            raise ProtocolError(422, "nifti_header_invalid")
        if struct.unpack("<i", header[:4])[0] == 348:
            endian = "<"
        elif struct.unpack(">i", header[:4])[0] == 348:
            endian = ">"
        else:
            raise ProtocolError(422, "nifti_header_invalid")
        if header[344:348] != b"n+1\x00":
            raise ProtocolError(422, "nifti_single_file_required")
        dim = struct.unpack(endian + "8h", header[40:56])
        if dim[0] != 3 or any(d <= 0 for d in dim[1:4]) or list(dim[1:4]) != expected_geometry["dimensions_xyz"]:
            raise ProtocolError(422, "nifti_dimensions_mismatch")
        datatype, bitpix = struct.unpack(endian + "2h", header[70:74])
        if datatype not in INTEGER_NIFTI or INTEGER_NIFTI[datatype][1] * 8 != bitpix:
            raise ProtocolError(422, "nifti_integer_labels_required")
        count = math.prod(dim[1:4])
        byte_count = count * INTEGER_NIFTI[datatype][1]
        if byte_count > 512 * 1024 * 1024:
            raise ProtocolError(422, "nifti_voxel_limit")
        pixdim = struct.unpack(endian + "8f", header[76:108])
        vox_offset = struct.unpack(endian + "f", header[108:112])[0]
        slope, intercept = struct.unpack(endian + "2f", header[112:120])
        if not math.isfinite(vox_offset) or int(vox_offset) != vox_offset or not 352 <= vox_offset <= 4096 or slope not in (0, 1) or intercept != 0:
            raise ProtocolError(422, "nifti_scaling_invalid")
        sform_code = struct.unpack(endian + "h", header[254:256])[0]
        if sform_code <= 0:
            raise ProtocolError(422, "nifti_geometry_missing")
        matrix = [struct.unpack(endian + "4f", header[offset:offset + 16]) for offset in (280, 296, 312)]
        if not all(math.isfinite(item) for row in matrix for item in row):
            raise ProtocolError(422, "nifti_geometry_invalid")
        # NIfTI stores RAS; the canonical workbench geometry is LPS.
        got_origin = [-matrix[0][3], -matrix[1][3], matrix[2][3]]
        got_direction = [
            -matrix[0][0] / float(expected_geometry["spacing_xyz_mm"][0]),
            -matrix[0][1] / float(expected_geometry["spacing_xyz_mm"][1]),
            -matrix[0][2] / float(expected_geometry["spacing_xyz_mm"][2]),
            -matrix[1][0] / float(expected_geometry["spacing_xyz_mm"][0]),
            -matrix[1][1] / float(expected_geometry["spacing_xyz_mm"][1]),
            -matrix[1][2] / float(expected_geometry["spacing_xyz_mm"][2]),
            matrix[2][0] / float(expected_geometry["spacing_xyz_mm"][0]),
            matrix[2][1] / float(expected_geometry["spacing_xyz_mm"][1]),
            matrix[2][2] / float(expected_geometry["spacing_xyz_mm"][2]),
        ]
        got_spacing = [math.sqrt(sum(matrix[row][axis] ** 2 for row in range(3))) for axis in range(3)]
        if (any(abs(got_spacing[i] - float(expected_geometry["spacing_xyz_mm"][i])) > _nifti_float32_tolerance(expected_geometry["spacing_xyz_mm"][i]) for i in range(3)) or
                any(abs(got_origin[i] - float(expected_geometry["origin_lps_mm"][i])) > _nifti_float32_tolerance(expected_geometry["origin_lps_mm"][i]) for i in range(3)) or
                any(abs(got_direction[i] - float(expected_geometry["direction"][i])) > 1e-6 for i in range(9))):
            raise ProtocolError(422, "nifti_geometry_mismatch")
        padding = int(vox_offset) - 348
        if len(stream.read(padding)) != padding:
            raise ProtocolError(422, "nifti_payload_short")
        fmt, width = INTEGER_NIFTI[datatype]
        unpacker = struct.Struct(endian + fmt)
        remaining = byte_count
        while remaining:
            block = stream.read(min(1024 * 1024, remaining))
            if not block:
                raise ProtocolError(422, "nifti_payload_short")
            remaining -= len(block)
            aligned = len(block) - len(block) % width
            if aligned != len(block):
                raise ProtocolError(422, "nifti_payload_invalid")
            for (value,) in struct.iter_unpack(unpacker.format, block):
                if value not in label_set:
                    raise ProtocolError(422, "nifti_label_not_allowed")
        if stream.read(1):
            raise ProtocolError(422, "nifti_trailing_payload")
    except ProtocolError:
        raise
    except (OSError, EOFError, gzip.BadGzipFile, zlib.error, struct.error, OverflowError):
        raise ProtocolError(422, "nifti_decode_failed") from None
    finally:
        if raw is not None:
            raw.close()


def validate_output_file(path: str | Path, descriptor: dict, expected_geometry: dict, action: str, task_key: str) -> dict:
    path = Path(path)
    size = path.stat().st_size
    kind = descriptor["type"]
    if size != descriptor["bytes"] or size <= 0 or size > ARTIFACT_LIMITS[kind]:
        raise ProtocolError(422, "artifact_size_mismatch")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != descriptor["sha256"]:
        raise ProtocolError(422, "artifact_hash_mismatch")
    if kind == "nifti_mask":
        geometry = _validate_cropped_geometry(descriptor.get("geometry"), expected_geometry) if descriptor["role"].startswith("candidate_mask.") else _validate_nifti_geometry(descriptor.get("geometry"), expected_geometry)
        _nifti_stream(path, geometry, descriptor.get("labels"))
    elif kind in JSON_OUTPUT_TYPES:
        geometry = _validate_geometry(descriptor.get("geometry"), expected_geometry, require_ct=True)
        data = path.read_bytes()
        if kind == "mesh_json":
            value = _validate_mesh(data, expected_geometry)
            if action != "mesh_stage" or descriptor["role"] != "surface_export":
                raise ProtocolError(422, "artifact_role_stage_mismatch")
        elif kind == "candidates_json":
            value = _validate_candidates(data)
            if action != "candidate_stage" or descriptor["role"] != "candidate_review":
                raise ProtocolError(422, "artifact_role_stage_mismatch")
        else:
            if action != "infer_stage" or descriptor["role"] != f"inference.{task_key}":
                raise ProtocolError(422, "artifact_role_stage_mismatch")
            value = _validate_inference(data, task_key)
    else:
        raise ProtocolError(422, "artifact_type_forbidden")
    return {"geometry": geometry}


def validate_artifact_descriptor(value: Any, action: str, task_key: str, expected_geometry: dict) -> dict:
    allowed = {"id", "type", "role", "bytes", "sha256", "geometry", "labels"}
    if not isinstance(value, dict) or set(value) - allowed or not {"id", "type", "role", "bytes", "sha256", "geometry"} <= set(value):
        raise ProtocolError(400, "invalid_artifact_descriptor")
    artifact_id = require_id(value["id"], "artifact id")
    kind = value["type"]
    if kind not in ARTIFACT_LIMITS:
        raise ProtocolError(400, "artifact_type_forbidden")
    role = validate_role(value["role"], kind, output=True)
    if action == "infer_stage":
        prefix = f"prediction.{task_key}."
        if kind == "nifti_mask" and not role.startswith(prefix):
            raise ProtocolError(400, "artifact_role_stage_mismatch")
        if kind not in {"nifti_mask", "inference_json"}:
            raise ProtocolError(400, "artifact_role_stage_mismatch")
    elif action == "mesh_stage" and (kind != "mesh_json" or role != "surface_export"):
        raise ProtocolError(400, "artifact_role_stage_mismatch")
    elif action == "candidate_stage" and kind not in {"nifti_mask", "candidates_json"}:
        raise ProtocolError(400, "artifact_role_stage_mismatch")
    size = require_int(value["bytes"], "artifact bytes", 1, ARTIFACT_LIMITS[kind])
    digest = require_sha256(value["sha256"], "artifact sha256")
    if value["role"].startswith("candidate_mask."):
        geometry = _validate_cropped_geometry(value["geometry"], expected_geometry)
    elif kind == "nifti_mask":
        geometry = _validate_nifti_geometry(value["geometry"], expected_geometry)
    else:
        geometry = _validate_geometry(value["geometry"], expected_geometry, require_ct=True)
    labels = value.get("labels", [])
    if kind == "nifti_mask":
        if not isinstance(labels, list) or not labels or len(labels) > 256 or any(isinstance(x, bool) or not isinstance(x, int) or not 0 <= x <= 65535 for x in labels) or len(set(labels)) != len(labels):
            raise ProtocolError(400, "mask_labels_invalid")
    elif "labels" in value:
        raise ProtocolError(400, "artifact_labels_not_allowed")
    if kind == "nifti_mask":
        if action == "candidate_stage":
            if not role.startswith("candidate_mask."):
                raise ProtocolError(400, "artifact_role_stage_mismatch")
        elif action != "infer_stage":
            raise ProtocolError(400, "artifact_role_stage_mismatch")
    return {"id": artifact_id, "type": kind, "role": role, "bytes": size, "sha256": digest, "geometry": geometry, "labels": labels}
