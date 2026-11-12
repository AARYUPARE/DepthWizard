# iPad AR terrain measurement integration

## Data path

```text
Persisted DSM / rDSM raster
        |
        +--> LOD0 terrain mesh + mesh report
        |
        +--> GET /v1/projects/measurements
                  |
                  +--> source-backed samples on valid LOD0 vertices
                              |
                              v
Desktop AR exporter
  - fetches LOD0 GLB
  - fetches measurement report
  - verifies project, surface, units, surface SHA, LOD0 SHA, build-config SHA
  - writes one project.json + model.usdz
        |
        v
Project validator + authenticated LAN bridge
  - validates manifest and coordinate contract
  - includes measurements in /api/v1/projects
        |
        v
iPad ProjectSyncClient -> RemoteProject -> local project.json
        |
        v
RealityKit terrain + child measurement pins
        |
        +--> 9 featured pins
        +--> show all sampled pins (up to 100)
        +--> tap pin -> measurement detail card
```

The iPad never samples or reconstructs terrain values. All measurement values and positions are authored by the desktop scientific service from the persisted primary surface and the exact mesh build report.

## Measurement sampling contract

The service samples a 10×10 spatial grid (up to 100 points). It follows the LOD0 mesh stride and the terrain mesh's valid-triangle rules, so each chosen pixel belongs to a vertex used by a rendered triangle. The final source row and column are appended to the LOD0 sampling grid by the mesh builder; measurement sampling follows the same rule.

Nine spatially distributed cells are marked featured for the initial AR view. Their picks alternate robust local low and high quantiles, with a median sample at the center. Remaining cells use a local median sample. This gives the first view spatial coverage and a useful range of terrain heights without selecting arbitrary isolated pixels.

Every point includes:
- stable ID and label;
- raw persisted raster value, source product, and units;
- source raster pixel column and row (zero-based, top-left origin);
- normalized terrain-local XYZ position;
- featured flag.

DSM values retain metre units only when the source surface is a metric DSM. rDSM values stay raw and use `relative` units. The display-only rDSM vertical scaling is used solely to compute where a pin sits on the visible mesh. It is never applied to the reported value.

## Coordinate-space definition

`terrain_glb_local_bounds_v1` defines fractions in the exported LOD0 GLB local bounding box, not geographic coordinates, not world coordinates, and not physical metres:

- `x = column / (raster_width - 1)`
- `y = (value - measurementValueMinimum) / (measurementValueMaximum - measurementValueMinimum)` (0.5 when the range is zero); because the mesh display elevation is a positive affine transform of the same source values, this is the same normalized Y as the visible LOD0 mesh
- `z = 1 - row / (raster_height - 1)`

The mesh uses positive X for raster columns, positive Y for elevation up, and negative Z for raster rows. The vertical fraction uses the sampled LOD0 vertices, not all raster pixels, so it shares the mesh's display bounds when the LOD is decimated. Positions are dimensionless; the iPad maps them into the loaded USDZ entity's local visual bounds after the model is normalized for AR tabletop viewing.

The manifest carries `measurementValueMinimum` and `measurementValueMaximum`, computed over the valid sampled LOD0 vertices. Both bridge validators recompute the expected normalized Y from those raw-value bounds and reject a pin with a mismatched height. No relative-elevation value is interpreted as metres.

Before a project is exported, the exporter verifies:
- project ID, surface product and vertical units match;
- the measurement surface SHA-256 equals the mesh report's surface SHA-256;
- the measurement LOD0 SHA-256 equals the exact GLB LOD0 download digest;
- the measurement build-configuration digest equals the mesh report.

The package validator and both LAN bridge implementations then validate the schema, exact coordinate-space and axes, SHA-256 digest syntax and mesh-digest consistency, dimensions, stride, unique IDs, finite values, source product/units, pixel bounds, pixel-to-normalized X/Z equality, pixel-to-normalized Y equality, and membership of each pixel in the declared LOD0 sample grid. The bridges do not claim to cryptographically re-derive the original raster or GLB from these metadata fields; the exporter is the stage that checks source-surface, mesh-report, and actual GLB bytes against one another. A bad or stale measurement payload fails closed rather than producing misplaced pins.

## Desktop sidecar rebuild requirement

The new `/v1/projects/measurements` route is part of the Python scientific API. The generated `depthwizard-core-runtime` directory is intentionally excluded from Git, so an already-built local `depthwizard-core.exe` will not acquire this route just because the source changed. Rebuild the local runtime before running or packaging the updated desktop application.

From the repository root in PowerShell, using the project's existing pinned DA3 vendor source and verified model snapshot:

```powershell
uv sync --frozen --python 3.12 --extra ml --extra dev --extra standalone
uv run --frozen python -m scripts.build_standalone_sidecar

Push-Location apps/desktop
npm ci --no-audit --no-fund
npm test
npm run build
npm run tauri dev
Pop-Location
```

The sidecar builder performs its frozen-runtime self-check and stages the new runtime into `apps/desktop/src-tauri/resources/depthwizard-core-runtime/`. If the pinned DA3 source or verified model snapshot is absent, setup must be completed using the repository's DA3 setup instructions before building; do not copy an old runtime into the resource folder to bypass the check.

The Windows GitHub workflow rebuilds and refreshes the three separately bundled one-file AR utilities (exporter, LAN bridge, and project validator) from their tracked Python source. It does not replace the scientific sidecar, because that is a much larger onedir runtime with a pinned model payload.

## iPad UI and backwards compatibility

`measurements` is an optional field on the project manifest and Swift project model. Legacy USDZ projects without it still load and show the existing terrain AR experience without pins. Projects with metadata display featured pins first; the control reveals the full sample set without restarting AR tracking. The marker root is a child of the movable terrain entity, so pins inherit placement, drag, rotation and scale.

Tapping a pin opens a detail card with the raw value and units, source/type, original raster pixel and normalized terrain position. Relative values are explicitly described as relative elevation, not metric height. Tapping the placed terrain itself does not trigger another placement.
