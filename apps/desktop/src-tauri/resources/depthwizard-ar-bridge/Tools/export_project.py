
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
import ctypes

MAX_MODEL_BYTES = 500 * 1024 * 1024

blender_env = os.environ.copy()

def request_bytes(url: str, token: str) -> tuple[bytes, dict[str, str]]:
    request = urllib.request.Request(
        url,
        headers={
            "x-depthwizard-token": token,
            "Accept": "application/json, model/gltf-binary",
            "Connection": "close",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            data = response.read(MAX_MODEL_BYTES + 1)
            if len(data) > MAX_MODEL_BYTES:
                raise RuntimeError("Downloaded data exceeds the 500 MB limit.")
            return data, {
                key.lower(): value for key, value in response.headers.items()
            }
    except urllib.error.HTTPError as exc:
        details = exc.read(2000).decode("utf-8", errors="replace")
        raise RuntimeError(
            f"DepthWizard API returned HTTP {exc.code}: {details}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Could not connect to the DepthWizard sidecar: {exc}"
        ) from exc


def api_url(base: str, endpoint: str, project_dir: Path) -> str:
    query = urllib.parse.urlencode({"project_dir": str(project_dir)})
    return f"{base.rstrip('/')}{endpoint}?{query}"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_project_name(name: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_-]+", "-", name.strip()).strip("-_")
    return value[:48] or "depthwizard-project"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a validated DepthWizard iPad AR project."
    )
    parser.add_argument("--api-base", required=True)
    parser.add_argument("--project-dir", required=True, type=Path)
    parser.add_argument("--export-root", required=True, type=Path)
    parser.add_argument("--blender", required=True, type=Path)
    parser.add_argument("--converter", required=True, type=Path)
    parser.add_argument("--name", required=True)
    parser.add_argument("--validator", type=Path)
    args = parser.parse_args()

    token = os.environ.get("DEPTHWIZARD_SESSION_TOKEN", "")
    if not token:
        raise RuntimeError("The DepthWizard session token is unavailable.")

    project_dir = args.project_dir.expanduser().resolve()
    export_root = args.export_root.expanduser().resolve()
    blender = args.blender.expanduser().resolve()
    converter = args.converter.expanduser().resolve()

    # Normalize Windows extended-length paths for Blender/OpenUSD.
    if os.name == "nt":
        def normalize_windows_path(path: Path) -> Path:
            value = str(path)

            if value.startswith("\\\\?\\UNC\\"):
                value = "\\\\" + value[8:]
            elif value.startswith("\\\\?\\"):
                value = value[4:]

            return Path(value)

        blender = normalize_windows_path(blender)
    converter = normalize_windows_path(converter)

    if not project_dir.is_dir():
        raise RuntimeError(f"Scientific project folder not found: {project_dir}")
    if not blender.is_file():
        raise RuntimeError(f"Blender executable not found: {blender}")
    if not converter.is_file():
        raise RuntimeError(f"USDZ converter not found: {converter}")

    export_root.mkdir(parents=True, exist_ok=True)

    report_data, _ = request_bytes(
        api_url(args.api_base, "/v1/projects/mesh", project_dir),
        token,
    )
    report = json.loads(report_data.decode("utf-8"))

    measurement_data, _ = request_bytes(
        api_url(args.api_base, "/v1/projects/measurements", project_dir),
        token,
    )
    measurement_report = json.loads(measurement_data.decode("utf-8"))
    if measurement_report.get("project_id") != report.get("project_id"):
        raise RuntimeError("Measurement report belongs to a different scientific project.")
    if measurement_report.get("surface_product") != report.get("surface_product"):
        raise RuntimeError("Measurement report surface does not match the exported terrain.")
    if measurement_report.get("vertical_units") != report.get("vertical_units"):
        raise RuntimeError("Measurement report units do not match the exported terrain.")
    if measurement_report.get("coordinate_space") != "terrain_glb_local_bounds_v1":
        raise RuntimeError("Unsupported AR measurement coordinate-space contract.")

    measurements = []
    for point in measurement_report.get("points", []):
        position = point.get("normalized_position")
        if not isinstance(position, dict):
            raise RuntimeError("Measurement point is missing its normalized terrain position.")
        measurements.append({
            "id": point["id"],
            "label": point["label"],
            "value": point["value"],
            "units": point["units"],
            "source": point["source"],
            "type": point["type"],
            "pixelX": point["pixel_x"],
            "pixelY": point["pixel_y"],
            "normalizedPosition": {
                "x": position["x"],
                "y": position["y"],
                "z": position["z"],
            },
            "featured": point["featured"],
        })
    if not measurements or len(measurements) > 500:
        raise RuntimeError("AR measurement report must contain between 1 and 500 points.")

    lod0 = next(
        (item for item in report.get("lods", []) if item.get("level") == 0),
        None,
    )
    if lod0 is None:
        raise RuntimeError("The project has no level-0 terrain mesh.")
    if measurement_report.get("surface_sha256") != report.get("surface_sha256"):
        raise RuntimeError("Measurement report was generated from a different source surface.")
    if measurement_report.get("mesh_sha256") != lod0.get("sha256"):
        raise RuntimeError("Measurement report was generated for a different LOD0 mesh.")
    if measurement_report.get("build_config_sha256") != report.get("build_config_sha256"):
        raise RuntimeError("Measurement report build configuration does not match the terrain mesh.")

    expected_sha = str(lod0.get("sha256", "")).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
        raise RuntimeError("The mesh report contains an invalid SHA-256.")

    model_data, headers = request_bytes(
        api_url(args.api_base, "/v1/projects/mesh/lod/0", project_dir),
        token,
    )

    actual_sha = sha256_bytes(model_data)
    header_sha = headers.get("x-depthwizard-mesh-sha256", "").lower()

    if actual_sha != expected_sha:
        raise RuntimeError("Downloaded terrain mesh failed SHA-256 verification.")
    if header_sha and header_sha != actual_sha:
        raise RuntimeError("API mesh SHA-256 header does not match the downloaded file.")

    project_id = f"{safe_project_name(args.name)}-{uuid.uuid4().hex[:8]}"
    final_dir = export_root / project_id

    # The inner staging folder has the final project ID, so the validator
    # sees the same folder name as project.json's ID.
    staging_parent = export_root / f".staging-{uuid.uuid4().hex}"
    staging_dir = staging_parent / project_id

    if final_dir.exists():
        raise RuntimeError(f"Export destination already exists: {final_dir}")

    staging_dir.mkdir(parents=True)

    try:
        source_glb = staging_dir / "source.glb"
        usdz_file = staging_dir / "model.usdz"
        source_glb.write_bytes(model_data)

        command = [
            str(blender),
            "--background",
            "--factory-startup",
            "--python",
            str(converter),
            "--",
            "--input",
            str(source_glb),
            "--output",
            str(usdz_file),
        ]

        blender_env = os.environ.copy()

        # Prevent the PyInstaller one-file environment from leaking
        # into the independently installed Blender process.
        for key in (
            "_MEIPASS2",
            "PYTHONHOME",
            "PYTHONPATH",
            "PYTHONEXECUTABLE",
        ):
            blender_env.pop(key, None)

        if os.name == "nt":
            # Restore Windows default DLL search behavior before
            # launching an independent native application.
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.SetDllDirectoryW.argtypes = [ctypes.c_wchar_p]
            kernel32.SetDllDirectoryW.restype = ctypes.c_int

            if not kernel32.SetDllDirectoryW(None):
                raise ctypes.WinError(ctypes.get_last_error())

        print("BLENDER COMMAND:", command, file=sys.stderr, flush=True)
        print("BLENDER PATH:", blender, file=sys.stderr, flush=True)
        print("CONVERTER EXISTS:", converter.is_file(), file=sys.stderr, flush=True)
        print("SOURCE EXISTS:", source_glb.is_file(), file=sys.stderr, flush=True)
        print("OUTPUT DIRECTORY:", str(usdz_file.parent), file=sys.stderr, flush=True)


        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=900,
                env=blender_env,
            )
        except Exception as exc:
            raise RuntimeError(
                "BLENDER PROCESS LAUNCH FAILED\n"
                f"Command: {command}\n"
                f"Executable exists: {blender.is_file()}\n"
                f"Converter exists: {converter.is_file()}\n"
                f"Source exists: {source_glb.is_file()}\n"
                f"Output directory: {usdz_file.parent}\n"
                f"Exception: {repr(exc)}"
            ) from exc

        if result.returncode != 0:
            raise RuntimeError(
                "BLENDER USDZ CONVERSION FAILED\n"
                f"Return code: {result.returncode}\n"
                f"Command: {command}\n"
                f"Blender exists: {blender.is_file()}\n"
                f"Converter exists: {converter.is_file()}\n"
                f"Source exists: {source_glb.is_file()}\n"
                f"Source size: {source_glb.stat().st_size if source_glb.is_file() else 'MISSING'}\n"
                f"Output directory exists: {usdz_file.parent.is_dir()}\n"
                f"Output exists: {usdz_file.is_file()}\n"
                f"STDOUT:\n{result.stdout[-6000:]}\n"
                f"STDERR:\n{result.stderr[-6000:]}"
            )

        if result.returncode != 0:
            details = (
                "BLENDER STDOUT:\n"
                + result.stdout
                + "\nBLENDER STDERR:\n"
                + result.stderr
            )[-12000:]

            raise RuntimeError(
                f"Blender USDZ conversion failed:\n{details}"
)

        if not usdz_file.is_file() or usdz_file.stat().st_size == 0:
            raise RuntimeError("Blender did not produce a USDZ file.")

        surface_product = report.get("surface_product", "dsm")
        vertical_units = report.get("vertical_units", "relative")
        horizontal_units = report.get("horizontal_units", "px")

        model_sha = hashlib.sha256(usdz_file.read_bytes()).hexdigest()
        model_size = usdz_file.stat().st_size

        manifest = {
            "id": project_id,
            "name": args.name.strip() or "DepthWizard Project",
            "description": "Terrain exported from DepthWizard",
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "modelFile": "model.usdz",
            "modelFormat": "usdz",
            "sourceModelFile": "source.glb",
            "elevationMode": (
                "DSM" if surface_product == "dsm" else "Relative DSM"
            ),
            "units": vertical_units,
            "surfaceProduct": surface_product,
            "horizontalUnits": horizontal_units,
            "verticalUnits": vertical_units,
            "gsdX": report.get("gsd_x"),
            "gsdY": report.get("gsd_y"),
            "displayOnlyNormalization": surface_product == "rdsm",
            "sourceMeshSha256": actual_sha,
            "modelSha256": model_sha,
            "semantics": report.get("semantics"),
            "measurementSchemaVersion": measurement_report.get("schema_version", 1),
            "measurementCoordinateSpace": measurement_report["coordinate_space"],
            "measurementPixelOrigin": measurement_report.get("pixel_origin", "top_left_zero_based"),
            "measurementAxes": measurement_report.get("axes", {}),
            "measurementRasterWidth": measurement_report.get("raster_width"),
            "measurementRasterHeight": measurement_report.get("raster_height"),
            "measurementMeshStride": measurement_report.get("mesh_stride"),
            "measurementSourceSurfaceSha256": measurement_report.get("surface_sha256"),
            "measurementMeshSha256": measurement_report.get("mesh_sha256"),
            "measurementBuildConfigSha256": measurement_report.get("build_config_sha256"),
            "measurements": measurements,
        }

        (staging_dir / "project.json").write_text(
            json.dumps(manifest, indent=2),
            encoding="utf-8",
        )

        validator = args.validator
        if validator is None:
            validator_name = (
                "depthwizard-ar-validator.exe"
                if sys.platform == "win32"
                else "depthwizard-ar-validator"
            )
            validator = Path(sys.executable).resolve().parent / validator_name

        validator = validator.expanduser().resolve()
        if not validator.is_file():
            raise RuntimeError(f"AR validator not found: {validator}")

        validation = subprocess.run(
            [str(validator), str(staging_dir)],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=120,
        )

        if validation.returncode != 0:
            details = (validation.stdout + "\n" + validation.stderr)[-4000:]
            raise RuntimeError(f"AR project validation failed:\n{details}")

        # Publish only after conversion and validation succeed.
        staging_dir.rename(final_dir)

        print(json.dumps({
            "status": "success",
            "projectId": project_id,
            "projectName": manifest["name"],
            "projectPath": str(final_dir),
            "modelPath": str(final_dir / "model.usdz"),
            "modelSha256": model_sha,
            "modelBytes": model_size,
            "elevationMode": manifest["elevationMode"],
            "units": manifest["units"],
            "horizontalUnits": horizontal_units,
        }), flush=True)

        return 0

    finally:
        shutil.rmtree(staging_parent, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"AR EXPORT FAILED: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)