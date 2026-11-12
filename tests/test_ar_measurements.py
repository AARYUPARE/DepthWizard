from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import rasterio
from fastapi.testclient import TestClient
from rasterio.transform import from_origin

from depthwizard.contracts import ProjectMeshBuildRequest
from depthwizard.mesh.ar_measurements import build_project_measurements
from depthwizard.mesh.project_mesh import build_project_mesh
from depthwizard.pipeline.project import ProjectManifest
from depthwizard.provenance.manifest import sha256_file
from depthwizard.service import app


def _write_rasters(root: Path, product: str) -> tuple[Path, Path, np.ndarray]:
    height, width = 83, 107
    rows, columns = np.mgrid[0:height, 0:width]
    source = root / "source.tif"
    source_rgb = np.stack([
        (40 + columns * 2) % 255,
        (70 + rows * 3) % 255,
        (90 + columns + rows) % 255,
    ]).astype(np.uint8)
    with rasterio.open(
        source,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=3,
        dtype="uint8",
        crs="EPSG:32632",
        transform=from_origin(500000.0, 5400000.0, 0.5, 0.5),
    ) as dataset:
        dataset.write(source_rgb)

    if product == "dsm":
        surface_values = (510.0 + columns * 0.12 + rows * 0.08 + np.sin(columns / 5.0)).astype(np.float32)
    else:
        surface_values = (-2.5 + columns * 0.03 + rows * 0.01 + np.sin(columns / 6.0) * 0.4).astype(np.float32)

    surface = root / f"{product}.tif"
    nodata = -9999.0
    surface_values = surface_values.copy()
    surface_values[30:34, 40:44] = nodata
    with rasterio.open(
        surface,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=1,
        dtype="float32",
        crs="EPSG:32632",
        transform=from_origin(500000.0, 5400000.0, 0.5, 0.5),
        nodata=nodata,
    ) as dataset:
        dataset.write(surface_values, 1)
    # Retain the same nodata semantics used in the persisted artifact.
    return source, surface, surface_values


def _build_project(tmp_path: Path, product: str) -> tuple[Path, np.ndarray]:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    source, surface, values = _write_rasters(project_dir, product)
    manifest = ProjectManifest.create_or_load(project_dir, source)
    manifest.set_identity(
        source_sha256=sha256_file(source),
        input_kind="georeferenced",
        geometry_config_sha256=hashlib.sha256(b"geometry").hexdigest(),
        run_config_sha256=hashlib.sha256(b"run").hexdigest(),
    )
    manifest.register_artifact(
        product,
        surface,
        semantics="absolute_digital_surface_model" if product == "dsm" else "relative_digital_surface_model",
        units="m" if product == "dsm" else "relative",
        sha256=sha256_file(surface),
    )
    build_project_mesh(ProjectMeshBuildRequest(
        project_dir=project_dir,
        max_finest_samples=24,
        lod_levels=2,
    ))
    return project_dir, values


def test_measurements_are_spread_and_map_to_lod0_vertices(tmp_path: Path) -> None:
    project_dir, values = _build_project(tmp_path, "dsm")
    report = build_project_measurements(project_dir)

    assert report.project_id == ProjectManifest.load(project_dir).project_id
    assert report.coordinate_space == "terrain_glb_local_bounds_v1"
    assert report.surface_product == "dsm"
    assert report.vertical_units == "m"
    assert report.raster_width == values.shape[1]
    assert report.raster_height == values.shape[0]
    assert 80 <= len(report.points) <= 100
    assert sum(point.featured for point in report.points) == 9

    rows = np.arange(0, values.shape[0], report.mesh_stride, dtype=np.int64)
    if rows[-1] != values.shape[0] - 1:
        rows = np.append(rows, values.shape[0] - 1)
    columns = np.arange(0, values.shape[1], report.mesh_stride, dtype=np.int64)
    if columns[-1] != values.shape[1] - 1:
        columns = np.append(columns, values.shape[1] - 1)
    sampled_values = values[np.ix_(rows, columns)]
    sampled_valid = sampled_values != -9999.0
    mesh_min = float(sampled_values[sampled_valid].min())
    mesh_max = float(sampled_values[sampled_valid].max())

    for point in report.points:
        assert 0 <= point.pixel_x < values.shape[1]
        assert 0 <= point.pixel_y < values.shape[0]
        assert values[point.pixel_y, point.pixel_x] != -9999.0
        assert point.value == float(values[point.pixel_y, point.pixel_x])
        assert point.units == "m"
        assert point.type == "dsm"
        assert np.isclose(point.normalized_position.x, point.pixel_x / (values.shape[1] - 1))
        assert np.isclose(point.normalized_position.z, 1.0 - point.pixel_y / (values.shape[0] - 1))
        expected_y = (point.value - mesh_min) / (mesh_max - mesh_min)
        assert np.isclose(point.normalized_position.y, expected_y)


def test_relative_measurements_keep_raw_relative_values_and_units(tmp_path: Path) -> None:
    project_dir, values = _build_project(tmp_path, "rdsm")
    report = build_project_measurements(project_dir)

    assert report.surface_product == "rdsm"
    assert report.vertical_units == "relative"
    assert report.points
    for point in report.points:
        assert point.units == "relative"
        assert point.type == "rdsm"
        assert point.value == float(values[point.pixel_y, point.pixel_x])
        assert -10.0 < point.value < 10.0
        assert 0.0 <= point.normalized_position.y <= 1.0


def test_measurement_service_endpoint_returns_contract(tmp_path: Path) -> None:
    project_dir, _ = _build_project(tmp_path, "dsm")
    response = TestClient(app).get(
        "/v1/projects/measurements",
        params={"project_dir": str(project_dir)},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["coordinate_space"] == "terrain_glb_local_bounds_v1"
    assert payload["pixel_origin"] == "top_left_zero_based"
    assert payload["surface_product"] == "dsm"
    assert len(payload["points"]) > 0
    assert "normalized_position" in payload["points"][0]
