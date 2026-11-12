from __future__ import annotations

from pathlib import Path
from typing import Literal

import numpy as np
import rasterio
from pydantic import BaseModel, Field

from depthwizard.mesh.project_mesh import _surface_artifact, load_project_mesh
from depthwizard.pipeline.project import ProjectManifest
from depthwizard.provenance.manifest import sha256_file
from depthwizard.visualization.relative_scale import relative_display_vertical_scale


class TerrainMeasurementPosition(BaseModel):
    """Fractions within the exported GLB's local XYZ bounds (not geographic coordinates)."""

    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    z: float = Field(ge=0.0, le=1.0)


class TerrainMeasurementPoint(BaseModel):
    id: str
    label: str
    value: float
    units: Literal["m", "relative"]
    source: str
    type: Literal["dsm", "rdsm"]
    pixel_x: int = Field(ge=0)
    pixel_y: int = Field(ge=0)
    normalized_position: TerrainMeasurementPosition
    featured: bool = False


class ProjectMeasurementReport(BaseModel):
    schema_version: int = 1
    project_id: str
    coordinate_space: Literal["terrain_glb_local_bounds_v1"] = "terrain_glb_local_bounds_v1"
    axes: dict[str, str] = Field(default_factory=lambda: {
        "x": "raster_columns_positive",
        "y": "mesh_elevation_up",
        "z": "negative_raster_rows",
    })
    pixel_origin: Literal["top_left_zero_based"] = "top_left_zero_based"
    surface_product: Literal["dsm", "rdsm"]
    vertical_units: Literal["m", "relative"]
    raster_width: int = Field(ge=2)
    raster_height: int = Field(ge=2)
    mesh_stride: int = Field(ge=1)
    surface_sha256: str
    mesh_sha256: str
    build_config_sha256: str
    points: list[TerrainMeasurementPoint] = Field(max_length=500)
    semantics: str = "sampled_from_persisted_primary_surface_at_exported_lod0_vertices"


def _sample_indices(length: int, stride: int) -> np.ndarray:
    values = np.arange(0, length, stride, dtype=np.int64)
    if values[-1] != length - 1:
        values = np.append(values, length - 1)
    return values


def _vertices_used_by_lod0_faces(valid: np.ndarray) -> np.ndarray:
    """Match terrain_mesh_from_dsm's valid-triangle rule so every pin lands on a real mesh vertex."""
    used = np.zeros(valid.shape, dtype=bool)
    if valid.shape[0] < 2 or valid.shape[1] < 2:
        return used

    triangle_a = valid[:-1, :-1] & valid[1:, :-1] & valid[:-1, 1:]
    used[:-1, :-1] |= triangle_a
    used[1:, :-1] |= triangle_a
    used[:-1, 1:] |= triangle_a

    triangle_b = valid[:-1, 1:] & valid[1:, :-1] & valid[1:, 1:]
    used[:-1, 1:] |= triangle_b
    used[1:, :-1] |= triangle_b
    used[1:, 1:] |= triangle_b
    return used


def build_project_measurements(project_dir: str | Path, *, grid_size: int = 10) -> ProjectMeasurementReport:
    """Create up to grid_size² well-spread measurements aligned to actual LOD0 mesh vertices.

    Elevation values remain the persisted DSM/rDSM values. The normalized Y coordinate is derived
    separately from the display-only mesh elevation, so a relative DSM is never converted to metres.
    """
    if not 2 <= grid_size <= 20:
        raise ValueError("measurement grid_size must be between 2 and 20")

    directory = Path(project_dir).expanduser().resolve()
    manifest = ProjectManifest.load(directory)
    product, surface_path, surface_sha = _surface_artifact(manifest)
    report = load_project_mesh(directory)
    if report.surface_product != product or report.surface_sha256 != surface_sha:
        raise RuntimeError("terrain mesh and primary surface identities do not match")
    if not report.lods:
        raise ValueError("terrain mesh has no LOD artifacts")
    stride = next((lod.stride for lod in report.lods if lod.level == 0), None)
    if stride is None:
        raise ValueError("terrain mesh has no LOD0 stride")
    lod0 = next((lod for lod in report.lods if lod.level == 0), None)
    if lod0 is None:
        raise ValueError("terrain mesh has no LOD0 artifact")

    with rasterio.open(surface_path) as src:
        if src.width != report.raster_width or src.height != report.raster_height:
            raise RuntimeError("primary surface dimensions no longer match the terrain mesh")
        raw = src.read(1).astype(np.float32)
        valid = src.read_masks(1) > 0
        valid &= np.isfinite(raw)
        if src.nodata is not None and np.isfinite(src.nodata):
            valid &= raw != np.float32(src.nodata)
    if sha256_file(surface_path) != surface_sha:
        raise RuntimeError("primary surface changed while AR measurements were being prepared")

    rows = _sample_indices(raw.shape[0], stride)
    cols = _sample_indices(raw.shape[1], stride)
    sampled_valid = valid[np.ix_(rows, cols)]
    used_vertices = _vertices_used_by_lod0_faces(sampled_valid)
    if not np.any(used_vertices):
        raise ValueError("LOD0 terrain contains no valid vertices for AR measurements")

    gsd_x = float(report.gsd_x)
    gsd_y = float(report.gsd_y)
    span_x = max(raw.shape[1] - 1, 1) * gsd_x
    span_y = max(raw.shape[0] - 1, 1) * gsd_y
    if product == "rdsm":
        scale = relative_display_vertical_scale(raw, valid, span_x=span_x, span_y=span_y)
        center = float(np.median(raw[valid].astype(np.float64)))
        mesh_elevation = (raw.astype(np.float64) - center) * scale
    else:
        mesh_elevation = raw.astype(np.float64)

    # RealityKit positions are derived from the sampled LOD0 vertex buffer, not every
    # source-raster pixel. Use the same sampled grid for the vertical bounds so pin Y fractions
    # match the actual exported geometry even when LOD0 has stride > 1.
    sampled_mesh_elevation = mesh_elevation[np.ix_(rows, cols)]
    mesh_values = sampled_mesh_elevation[sampled_valid]
    if mesh_values.size == 0:
        raise ValueError("LOD0 terrain has no valid elevation vertices for measurement placement")
    mesh_min = float(np.min(mesh_values))
    mesh_max = float(np.max(mesh_values))
    mesh_range = mesh_max - mesh_min

    row_groups = np.array_split(np.arange(len(rows)), grid_size)
    col_groups = np.array_split(np.arange(len(cols)), grid_size)
    points: list[TerrainMeasurementPoint] = []
    feature_cells = {0, grid_size // 2, grid_size - 1}
    source_label = "Persisted DSM raster" if product == "dsm" else "Persisted relative DSM raster"
    units: Literal["m", "relative"] = "m" if product == "dsm" else "relative"

    for grid_row, row_group in enumerate(row_groups):
        if row_group.size == 0:
            continue
        for grid_col, col_group in enumerate(col_groups):
            if col_group.size == 0:
                continue
            cell_used = used_vertices[np.ix_(row_group, col_group)]
            candidates = np.argwhere(cell_used)
            if candidates.size == 0:
                continue

            local_rows = row_group[candidates[:, 0]]
            local_cols = col_group[candidates[:, 1]]
            source_rows = rows[local_rows]
            source_cols = cols[local_cols]
            candidate_values = raw[source_rows, source_cols].astype(np.float64)
            is_featured = grid_row in feature_cells and grid_col in feature_cells
            if is_featured and (grid_row, grid_col) == (grid_size // 2, grid_size // 2):
                target_quantile = 0.5
            elif is_featured:
                # Alternate robust low/high picks across the 3x3 featured grid. This preserves
                # spatial coverage while surfacing local terrain variation in the initial view.
                target_quantile = 0.95 if (grid_row + grid_col) % 2 == 0 else 0.05
            else:
                target_quantile = 0.5
            target_value = float(np.quantile(candidate_values, target_quantile))
            center_row = (grid_row + 0.5) * raw.shape[0] / grid_size
            center_col = (grid_col + 0.5) * raw.shape[1] / grid_size
            spatial_distance = (
                (source_rows.astype(np.float64) - center_row) ** 2
                + (source_cols.astype(np.float64) - center_col) ** 2
            )
            chosen = int(np.lexsort((spatial_distance, np.abs(candidate_values - target_value)))[0])
            row = int(source_rows[chosen])
            col = int(source_cols[chosen])
            value = float(raw[row, col])
            display_height = float(mesh_elevation[row, col])
            normalized_y = (
                0.5 if mesh_range <= 1e-12
                else min(1.0, max(0.0, (display_height - mesh_min) / mesh_range))
            )
            featured = grid_row in feature_cells and grid_col in feature_cells
            point_id = f"p{grid_row:02d}_{grid_col:02d}"
            points.append(TerrainMeasurementPoint(
                id=point_id,
                label=f"Point {len(points) + 1:02d}",
                value=value,
                units=units,
                source=source_label,
                type=product,
                pixel_x=col,
                pixel_y=row,
                normalized_position=TerrainMeasurementPosition(
                    x=col / max(raw.shape[1] - 1, 1),
                    y=normalized_y,
                    z=1.0 - row / max(raw.shape[0] - 1, 1),
                ),
                featured=featured,
            ))

    if not points:
        raise ValueError("no valid terrain measurements could be sampled")
    return ProjectMeasurementReport(
        project_id=manifest.project_id,
        surface_product=product,
        vertical_units=units,
        raster_width=int(raw.shape[1]),
        raster_height=int(raw.shape[0]),
        mesh_stride=int(stride),
        surface_sha256=surface_sha,
        mesh_sha256=lod0.sha256,
        build_config_sha256=report.build_config_sha256,
        points=points,
    )
