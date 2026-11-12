#!/usr/bin/env python3
"""Validate a DepthWizard AR project folder and its USDZ package."""
from __future__ import annotations
import json
import re
import sys
import struct
import zipfile
import math
from pathlib import Path, PurePosixPath

MAX_BYTES = 500 * 1024 * 1024
USD_EXTENSIONS = {".usd", ".usda", ".usdc"}

def inspect_usdz(asset: Path) -> tuple[bool, str]:
    if asset.is_symlink() or not asset.is_file():
        return False, "model asset is missing or is a symlink"
    size = asset.stat().st_size
    if size < 64 or size > MAX_BYTES:
        return False, "USDZ size must be between 64 bytes and 500 MB"
    if not zipfile.is_zipfile(asset):
        return False, "file is not a USDZ ZIP package"
    try:
        with zipfile.ZipFile(asset, "r") as archive:
            entries = archive.infolist()
            if not entries or len(entries) > 10000:
                return False, "USDZ is empty or contains too many entries"
            first = entries[0]
            if Path(first.filename).suffix.lower() not in USD_EXTENSIONS or first.file_size == 0:
                return False, "first USDZ package entry must be a non-empty USD scene file"
            root_signature = archive.read(first)[:8]
            if not (root_signature.startswith(b"#usda") or root_signature.startswith(b"PXR-USDC")):
                return False, "USDZ root scene has an unknown USD file signature"
            expanded = 0
            with asset.open("rb") as raw:
                for entry in entries:
                    name = PurePosixPath(entry.filename.replace("\\", "/"))
                    if name.is_absolute() or ".." in name.parts or entry.is_dir():
                        return False, "USDZ contains an unsafe path or directory entry"
                    if entry.flag_bits & 1:
                        return False, "encrypted USDZ entries are unsupported"
                    if entry.compress_type != zipfile.ZIP_STORED:
                        return False, "USDZ entries must be stored without ZIP compression"
                    raw.seek(entry.header_offset)
                    local_header = raw.read(30)
                    if len(local_header) != 30:
                        return False, "USDZ contains a truncated local ZIP header"
                    fields = struct.unpack("<IHHHHHIIIHH", local_header)
                    signature, name_length, extra_length = fields[0], fields[-2], fields[-1]
                    if signature != 0x04034B50:
                        return False, "USDZ contains an invalid local ZIP header"
                    offset = entry.header_offset + 30 + name_length + extra_length
                    if offset % 64:
                        return False, "USDZ entry is not 64-byte aligned"
                    expanded += entry.file_size
                    if expanded > MAX_BYTES:
                        return False, "USDZ expands beyond the 500 MB safety limit"
            if archive.testzip() is not None:
                return False, "USDZ contains a corrupt ZIP entry"
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        return False, f"could not read USDZ package: {exc}"
    return True, f"{size:,} bytes, aligned USDZ archive"

def validate_measurements(data: dict) -> tuple[bool, str]:
    measurements = data.get("measurements")
    if measurements is None:
        return True, "legacy project without AR measurements"
    if not isinstance(measurements, list) or not 1 <= len(measurements) <= 500:
        return False, "measurements must contain between 1 and 500 entries"
    if type(data.get("measurementSchemaVersion")) is not int or data.get("measurementSchemaVersion") != 1:
        return False, "unsupported measurement schema version"
    if data.get("measurementCoordinateSpace") != "terrain_glb_local_bounds_v1":
        return False, "unsupported measurement coordinate-space contract"
    if data.get("measurementPixelOrigin") != "top_left_zero_based":
        return False, "unsupported measurement pixel origin"
    expected_axes = {
        "x": "raster_columns_positive",
        "y": "mesh_elevation_up",
        "z": "negative_raster_rows",
    }
    if data.get("measurementAxes") != expected_axes:
        return False, "measurement axes do not match the terrain mesh coordinate contract"
    seen = set()
    raster_width = data.get("measurementRasterWidth")
    raster_height = data.get("measurementRasterHeight")
    if (isinstance(raster_width, bool) or not isinstance(raster_width, int) or raster_width < 2 or
            isinstance(raster_height, bool) or not isinstance(raster_height, int) or raster_height < 2):
        return False, "measurement raster dimensions are missing or invalid"
    stride = data.get("measurementMeshStride")
    if isinstance(stride, bool) or not isinstance(stride, int) or stride < 1:
        return False, "measurement mesh stride is missing or invalid"
    product = data.get("surfaceProduct")
    units = data.get("verticalUnits", data.get("units"))
    for key in (
        "measurementSourceSurfaceSha256",
        "measurementMeshSha256",
        "measurementBuildConfigSha256",
    ):
        digest = data.get(key)
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            return False, f"{key} must be a lowercase SHA-256 digest"
    if data.get("measurementMeshSha256") != data.get("sourceMeshSha256"):
        return False, "measurement metadata does not identify the exported source mesh"
    expected_units = "m" if product == "dsm" else "relative" if product == "rdsm" else None
    if expected_units is None or units != expected_units:
        return False, "measurement surface product and vertical units are inconsistent"
    value_minimum = data.get("measurementValueMinimum")
    value_maximum = data.get("measurementValueMaximum")
    if (isinstance(value_minimum, bool) or not isinstance(value_minimum, (int, float)) or
            not math.isfinite(value_minimum) or isinstance(value_maximum, bool) or
            not isinstance(value_maximum, (int, float)) or not math.isfinite(value_maximum) or
            value_maximum < value_minimum):
        return False, "measurement elevation value range is missing or invalid"
    for point in measurements:
        if not isinstance(point, dict):
            return False, "measurement entries must be objects"
        identifier = point.get("id")
        if not isinstance(identifier, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,80}", identifier) is None or identifier in seen:
            return False, "measurement IDs must be unique safe identifiers"
        if point.get("type") != product or point.get("units") != expected_units:
            return False, "measurement point surface type or units do not match the project"
        value = point.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return False, "measurement values must be finite numbers"
        for key in ("label", "source"):
            if not isinstance(point.get(key), str) or not point[key].strip() or len(point[key]) > 120:
                return False, f"measurement {key} must be a non-empty string of at most 120 characters"
        if type(point.get("featured")) is not bool:
            return False, "measurement featured flag must be boolean"
        for key in ("pixelX", "pixelY"):
            coordinate = point.get(key)
            if isinstance(coordinate, bool) or not isinstance(coordinate, int) or coordinate < 0:
                return False, f"measurement {key} must be a non-negative integer"
        if point["pixelX"] >= raster_width or point["pixelY"] >= raster_height:
            return False, "measurement pixel coordinate is outside the source raster"
        position = point.get("normalizedPosition")
        if not isinstance(position, dict):
            return False, "measurement normalizedPosition must be an object"
        for axis in ("x", "y", "z"):
            coordinate = position.get(axis)
            if isinstance(coordinate, bool) or not isinstance(coordinate, (int, float)) or not math.isfinite(coordinate) or not 0 <= coordinate <= 1:
                return False, f"measurement normalizedPosition.{axis} must be within [0, 1]"
        expected_x = point["pixelX"] / (raster_width - 1)
        expected_z = 1.0 - point["pixelY"] / (raster_height - 1)
        if not math.isclose(position["x"], expected_x, rel_tol=0.0, abs_tol=1e-6):
            return False, "measurement normalized X does not match its source raster column"
        if not math.isclose(position["z"], expected_z, rel_tol=0.0, abs_tol=1e-6):
            return False, "measurement normalized Z does not match its source raster row"
        if not value_minimum <= point["value"] <= value_maximum:
            return False, "measurement value is outside the sampled elevation range"
        value_range = value_maximum - value_minimum
        expected_y = 0.5 if value_range <= 1e-12 else (point["value"] - value_minimum) / value_range
        if not math.isclose(position["y"], expected_y, rel_tol=0.0, abs_tol=1e-6):
            return False, "measurement normalized Y does not match its source elevation value"
        if ((point["pixelX"] % stride != 0 and point["pixelX"] != raster_width - 1) or
                (point["pixelY"] % stride != 0 and point["pixelY"] != raster_height - 1)):
            return False, "measurement pixel is not a vertex in the exported LOD0 grid"
        seen.add(identifier)
    return True, f"{len(measurements)} validated terrain measurements"


def validate(folder: Path) -> tuple[bool, str]:
    if folder.is_symlink() or not folder.is_dir():
        return False, "project folder must be a real directory"
    manifest_path = folder / "project.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return False, "missing project.json or manifest symlink"
    if manifest_path.stat().st_size > 1024 * 1024:
        return False, "project.json exceeds the 1 MB limit"
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        return False, f"invalid JSON: {exc}"
    if not isinstance(data, dict):
        return False, "manifest must be a JSON object"
    required = ("id", "name", "modelFile", "modelFormat")
    if any(not isinstance(data.get(k), str) or not data[k].strip() for k in required):
        return False, "required fields: id, name, modelFile and modelFormat"
    project_id = data["id"]
    if project_id != folder.name or re.fullmatch(r"[A-Za-z0-9_-]{1,80}", project_id) is None:
        return False, "project id must be a simple ID matching the folder name"
    name = data["modelFile"]
    if name != Path(name).name or name in (".", "..") or "/" in name or "\\" in name:
        return False, "modelFile must be a filename within the project folder"
    if not isinstance(data["modelFormat"], str) or Path(name).suffix.lower() != ".usdz" or data["modelFormat"].lower().lstrip(".") != "usdz":
        return False, "the iPad v1 project must reference a USDZ companion asset"
    asset = folder / name
    try:
        if asset.resolve().parent != folder.resolve():
            return False, "model path escapes the project folder"
    except OSError:
        return False, "could not resolve model path"
    ok, details = inspect_usdz(asset)
    if not ok:
        return False, details
    measurement_ok, measurement_details = validate_measurements(data)
    if not measurement_ok:
        return False, measurement_details
    return True, f"{data['name']} -> {name} ({details}; {measurement_details})"

def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python validate_project.py <project-folder>")
        return 2
    ok, message = validate(Path(sys.argv[1]).expanduser().resolve())
    print(("PASS: " if ok else "FAIL: ") + message)
    return 0 if ok else 1

if __name__ == "__main__":
    raise SystemExit(main())
