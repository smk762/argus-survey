# argus-survey — v0 spec and build brief

> **Historical.** This is the brief the v0 build started from, kept for the record. It is **not** a description
> of the shipped package: where it and the code disagree, the code, [README.md](README.md) and
> [CHANGELOG.md](CHANGELOG.md) are right. Known divergences:
>
> - **§2 Image / CI / raster engine**: the image is `ubuntu:26.04` with PDAL, GDAL and PROJ from a pinned
>   conda-forge lockfile (`env/geo-linux-64.lock`), not apt packages (neither Ubuntu 24.04 nor 26.04 packages PDAL).
>   `gdal_calc.py` and `gdallocationinfo` are replaced by the unified `gdal raster calc` / `pixel-info` CLI.
> - **§4 and §6.7 Accuracy**: ASPRS Edition 2 reports RMSE only, with no 95 % figure (that was Edition 1);
>   at least 30 check points are required (not "flag if n < 20").
> - **§4 and §7 Viewer**: served by the API from `src/argus_survey/server/static/viewer.html` at `/viewer`;
>   `examples/viewer.html` only links to it.

You are building a new package in this folder from scratch. Read this whole file before you start. It is the spec, the build order and the definition of done. Where it says *verify*, check the fact against the installed tool before you rely on it; don't trust this file over `pdal --drivers` or `projinfo`.

## 1. What this is

A point-cloud toolkit for **land surveyors**: take LiDAR or photogrammetry points (LAS/LAZ/COPC), and produce the deliverables a surveyor actually hands over: QA report, ground classification, DTM/DSM, contours, stockpile volumes, cut/fill, profiles, change between epochs, and an accuracy statement against check points. Each run is traceable: every datum, transform and pipeline is recorded.

It is a showcase of capability within the Argus suite (github.com/smk762, `argus-*`), aimed at surveyors who will judge it on **correct datums, honest accuracy numbers and reproducible outputs**, not on visuals. Prefer one deliverable done properly over three done loosely.

Relationship to the suite: it is a standalone peer. Its rasters are plain GeoTIFFs, so `argus-atlas controls --dem <dtm.tif>` can consume a survey DTM directly (see §9). No code dependency on atlas in either direction.

### Out of scope for v0

- No ML classification (ground filtering is PDAL's SMRF/CSF; nothing trained).
- No photogrammetry (images → points). Input is already a point cloud.
- No meshing / TIN deliverables beyond what PDAL's `filters.delaunay` needs internally.
- No editing of points in the browser; the viewer only views.
- No cloud storage or multi-user auth. Local files under one data root.
- No CAD export (DXF/LandXML) in v0 — list it in TODO.md as the first follow-up; surveyors will ask.

## 2. Settled decisions

| Decision | Choice |
|---|---|
| Name / import / CLI | `argus-survey` / `argus_survey` / `argus-survey` |
| Scaffold | `uvx copier copy gh:smk762/argus-pkg-template .` (or `../argus-pkg-template` locally): `has_cli=true`, `has_server=true`, `python_min=3.11`, `ruff_pin=0.15.16` |
| Licence | MIT; versions from git tags via `hatch-vcs` (`_version.py` generated, never hand-edited) |
| Point engine | **PDAL CLI** (`pdal pipeline`, `pdal info`) as a subprocess, pipelines as JSON. PDAL is a **system package, never pip** (no `pdal` Python bindings, no `laspy`) |
| Raster engine | **GDAL CLI** (`gdalinfo -json`, `gdal_calc.py`, `gdal_contour`, `gdaldem`, `gdalwarp`, `gdallocationinfo`) as a subprocess. System package, never pip. **No rasterio** |
| CRS engine | PROJ (`projinfo`, `cs2cs` for checks), via the PDAL/GDAL stack's own PROJ |
| Python deps | `pydantic>=2.7`, `structlog`; extras `cli` (typer, rich), `server` (fastapi, uvicorn, `argus-cortex[server]`), `dev` (pytest, ruff, httpx). `numpy` only if a step genuinely can't be expressed with the GDAL/PDAL CLIs — say why in a comment |
| Server | FastAPI on **:8107** (suite ports: lens 8100, curator 8101, quarry 8102, forge 8103, proof 8104, atlas 8105, safelight/darkroom 8106) |
| Write posture | read-only by default; processing routes opt-in with `SURVEY_ALLOW_PROCESS=1` via `argus_cortex.server.WriteGuard` (copy the pattern from `../argus-atlas/src/argus_atlas/server/app.py`). `/health.read_only == not allow_process` |
| Image | `ubuntu:24.04` + apt `pdal gdal-bin python3-gdal proj-data` (one consistent GDAL/PROJ that PDAL links against). Do **not** layer apt PDAL on `ghcr.io/osgeo/gdal` — two GDALs. Verify the PDAL version (Ubuntu 24.04 ships 2.6.x) has `readers.copc`, `writers.copc`, `filters.smrf`, `filters.csf`, `filters.hag_nn` |
| CI | standalone `.github/workflows/ci.yml` that apt-installs the same packages (the shared argus-ci workflow has no system-package hook; atlas does the same — copy its ci.yml as the starting point) |
| Branches | `dev` default (feature PRs); `main` never ahead of `dev`, moved by fast-forward to a CI-passed `dev` commit (or, if protected, a release PR followed at once by a back-merge into `dev`). Tags `vX.Y.ZrcN` / `vX.Y.Z`, each with a CHANGELOG heading |
| Units | metres internally. If input is in US survey feet or international feet, convert explicitly and record it; never guess |

## 3. Core rules (put these in the repo's CLAUDE.md)

1. **No silent datums.** Every job records horizontal CRS, vertical CRS (or `null` + an explicit `vertical_assumed` reason), geoid model, epoch where relevant, and units. If the input has no vertical CRS, refuse height-dependent steps (volumes, contours, accuracy) unless `--assume-vertical EPSG:xxxx` is given; record that it was assumed.
2. **Record, don't assume.** Every PDAL pipeline JSON and every GDAL argv array goes into the job sidecar, in run order, with tool versions (`pdal --version`, `gdalinfo --version`, PROJ version). Every input file is pinned by SHA256 and size.
3. **Paths from clients are names under a root.** The API resolves inputs under `SURVEY_DATA_DIR` (port atlas's `resolve_under_root`); artifacts are addressed by `(job_id, kind)`; job ids match `^[A-Za-z0-9][A-Za-z0-9._-]*$`. No route takes a filesystem path.
4. **Numbers come with their uncertainty.** A volume is reported with the surface resolution, the void percentage of each surface, and the method (see §6.5). An accuracy statement says how many check points, how many fell outside coverage, and which standard.
5. **Never overwrite inputs.** Outputs go into the job dir; the input point cloud is read-only.
6. **One subprocess runner.** All PDAL/GDAL calls go through one function that logs (structlog), times, captures stderr, and appends to the sidecar; a non-zero exit raises a typed error carrying the argv and stderr tail.

## 4. Layout

```
src/argus_survey/
  models.py      Pydantic v2 wire types. JobSidecar (job.json) is the API contract: inputs + SHA256,
                 CRS record, commands[], products{kind: path, sha256, …}, timings, reports. SURVEY_VERSION = schema version.
  config.py      SURVEY_* env knobs, process_allowed(), resolve_under_root().
  tools.py       subprocess runner; pdal_pipeline(json) and gdal argv builders; versions(); doctor checks.
  crs.py         read CRS from `pdal info --metadata` (horizontal + vertical, WKT2/PROJJSON), resolve EPSG,
                 compound CRS construction, geoid grid presence check (projinfo / PROJ_DATA), unit detection.
  jobs.py        JobStore (create, load, save sidecar, artifact paths), strict id charset.
  ingest.py      info + reproject + to COPC (writers.copc), optional crop to a polygon/bbox.
  qa.py          stats, class histogram, return-number histogram, density raster (writers.gdal count),
                 coverage boundary (filters.hexbin), gap detection, outlier count; qa.json + density.tif.
  ground.py      outlier removal (filters.outlier) + SMRF or CSF; writes classified COPC.
  surfaces.py    DTM (class 2, idw/mean), DSM (first returns, max), nDSM/CHM (filters.hag_nn → max),
                 hillshade (gdaldem), void mask; resolution and window radius recorded.
  contours.py    gdal_contour from DTM at an interval, optional index-contour every N, GeoJSON + GPKG.
  volume.py      stockpile (surface vs base plane / base polygon / lowest-perimeter) and cut/fill (surface vs surface).
  profile.py     sample DTM/DSM along a polyline at a spacing → CSV + SVG.
  change.py      DEM of difference between two epochs (same grid enforced), with a level-of-detection threshold.
  accuracy.py    check points CSV vs DTM → residuals, RMSEz, mean, std, NVA (95%) per ASPRS edition 2 (2023); verify the formulas against the standard text.
  report.py      one HTML report per job (stdlib string templating, no JS framework): QA, CRS, products, numbers, commands.
  server/app.py  FastAPI :8107.
  cli.py         Typer: doctor, info, ingest, qa, ground, surfaces, contours, volume, profile, change, accuracy, report, run, serve.
examples/viewer.html   points + rasters in the browser (§7).
docs/              runbook, data sources, atlas showcase.
tests/
```

Match the code style of `../argus-atlas` (read its CLAUDE.md and `gdal_ops.py`, `jobs.py`, `config.py`, `server/app.py` before writing the equivalents here). Ruff, line-length 120.

## 5. CLI

All commands take `--job <id>` (created on first use) or `--out <dir>`; all write/extend `job.json`.

```
argus-survey doctor                                     # pdal/gdal/proj versions, required drivers, geoid grids present
argus-survey info IN.laz                                # pdal info summary, CRS record, no job
argus-survey ingest IN.laz --job site1 [--t-srs EPSG:7855] [--assume-vertical EPSG:5711] [--crop poly.geojson]
argus-survey qa --job site1 [--density-res 1.0]
argus-survey ground --job site1 [--method smrf|csf] [--slope … --window … --threshold … --scalar …]
argus-survey surfaces --job site1 --res 0.5 [--dtm --dsm --chm]
argus-survey contours --job site1 --interval 0.5 [--index 5]
argus-survey volume --job site1 --polygon pile.geojson --base plane|lowest-perimeter|surface:other.tif
argus-survey cutfill --job site1 --design design.tif
argus-survey profile --job site1 --line line.geojson --step 0.5
argus-survey change --job epoch2 --against epoch1 [--lod 0.10]
argus-survey accuracy --job site1 --checks checks.csv [--cols id,x,y,z] [--crs EPSG:…]
argus-survey report --job site1                         # report.html
argus-survey run IN.laz --job site1 --res 0.5 --interval 0.5   # ingest → qa → ground → surfaces → contours → report
argus-survey serve
```

## 6. Behaviour details

### 6.1 Ingest and CRS
- Read `pdal info --metadata --stats` first; build the CRS record from the SRS WKT (horizontal, vertical, units).
- `--t-srs` reprojects horizontally with `filters.reprojection`. Vertical transforms (ellipsoidal ↔ AHD/NAVD88 etc.) use a compound target CRS and need the geoid grid. `doctor` and `ingest` check the grid is available (`projinfo -s A -t B --spatial-test intersects -o PROJ` shows the operation; refuse if it falls back to a ballpark transform; record the operation string).
- Australian defaults to support and test by name: GDA2020 (EPSG:7844), MGA2020 zones (EPSG:7846–7859), AHD height (EPSG:5711), AUSGeoid2020 grid. Verify every code with `projinfo` rather than trusting this line. US: NAD83(2011) + NAVD88 + GEOID18. Don't hard-code a region; these are documented examples.
- Output: `points.copc.laz` + the CRS record in the sidecar.

### 6.2 QA (`qa.json`, `density.tif`, `boundary.geojson`)
Point count, bounds, density (mean, p5, p50, p95 pts/m² from the density raster), class histogram (ASPRS class names), return histogram, z range, outliers flagged by `filters.outlier` (counted, not removed here), cells below a density threshold as a gap layer. Each figure in the report links to the command that produced it.

### 6.3 Ground
SMRF default, CSF option; parameters recorded. Ground classification output keeps every point (reclassifies, doesn't drop). Report ground %, and the ground density (it bounds DTM resolution — warn when `--res` is finer than ground spacing supports, e.g. fewer than ~1 ground point per cell on average).

### 6.4 Surfaces
`writers.gdal` with explicit `resolution`, `output_type`, `window_size`, `nodata`, and origin snapped to a multiple of the resolution (so two epochs at the same res share a grid). DTM = ground, `idw`; DSM = first returns, `max`; CHM = `filters.hag_nn` then `max`. Fill small voids only if `--fill-voids N` (gdal_fillnodata, max distance N cells); always write a `voids.tif` mask and void % per surface.

### 6.5 Volumes
- Computation over a polygon on a raster grid: per-cell `(surface − base) × cell area`, summed separately for positive (fill / above) and negative (cut / below). Implement with `gdal_calc.py` (clip with `gdalwarp -cutline -crop_to_cutline`), sum via `gdalinfo -stats -json` (mean × valid count × cell area) or a documented equivalent. Record void cells inside the polygon; refuse if voids > 5% unless `--allow-voids`.
- Bases: `plane` (least-squares plane through the polygon's boundary heights), `lowest-perimeter` (horizontal plane at the minimum boundary height), `surface:<tif>` (another grid, must match the grid exactly — refuse rather than resample silently; offer `--resample` that records it).
- Output: `volume.json` with cut, fill, net (m³), area (m²), cell size, base description, void %.
- **Test with an analytic shape** (§8): a synthetic cone or box on a plane, known volume, assert within a tolerance that depends on resolution.

### 6.6 Change
Same grid required (§6.4 snapping makes this normal). DoD = later − earlier; cells with |Δ| < LoD set to 0 and shown as "no detectable change"; report gain/loss volumes and areas above LoD. Record both jobs' sidecar digests.

### 6.7 Accuracy
Check points CSV (id, x, y, z, optional description) in a stated CRS, transformed to the job CRS (recorded). Sample DTM with `gdallocationinfo -valonly` (bilinear if available, else nearest; record which). Report n, n outside coverage / in voids (excluded and listed), mean error, std, RMSEz, NVA at 95% (1.96 × RMSEz for non-vegetated, per ASPRS ed. 2 — verify), max |residual|, and a residual table. Flag the run if n < 20 (ASPRS guidance on minimum checkpoints — verify the number for the edition).

### 6.8 Report
Single self-contained `report.html`: job summary, CRS record (with a red banner when vertical was assumed), QA tables, product list with SHA256, volume/accuracy results, thumbnails (hillshade, density) as embedded PNGs from `gdal_translate`, and the full command log. Must be readable printed to PDF.

## 7. Server and viewer

- Routes: `GET /health`, `GET /inputs` (point clouds and vectors under the data root), `GET /jobs`, `GET /jobs/{id}`, `GET /jobs/{id}/{kind}` (artifacts by kind: `copc`, `dtm`, `dsm`, `chm`, `hillshade`, `contours`, `report`, …, with HTTP range support so COPC streams), and `POST /jobs/{id}/{step}` for each processing step (guarded by `SURVEY_ALLOW_PROCESS`). Long steps run in the background and stream progress frames (SSE), the way atlas streams bake frames.
- `examples/viewer.html`: MapLibre GL for the raster products and contours; a points panel that streams the job's COPC with a browser COPC reader and renders it (e.g. `copc` npm package + three.js; pin exact versions from cdn.jsdelivr.net, verify they work together). Colour by elevation / class / intensity. A profile line drawn on the map calls `/profile` when processing is allowed. Viewer is static and talks only to the API.

## 8. Tests

- **No binary fixtures.** `conftest.py` generates point clouds with PDAL from text: write a CSV of synthetic points (a tilted ground plane with noise, a few "trees" as tall sparse columns, a "building" box, a stockpile cone of known radius/height), then `readers.text` → `writers.las` with an assigned compound CRS. Generate a second epoch with a known change.
- Tests that need PDAL/GDAL **skip** when the tools are absent; `SURVEY_TEST_REQUIRE_TOOLS=1` makes them fail instead and CI sets it (same pattern as atlas's `ATLAS_TEST_REQUIRE_GDAL`).
- Must-have assertions: CRS record parsed from the fixture; refusal without a vertical CRS; SMRF puts the trees/building in non-ground; DTM grid origin snapped; cone volume within tolerance (scale tolerance with res); cut/fill sign convention; change volume matches the injected change; accuracy RMSEz for check points sampled from the known plane ≈ the injected noise; every step appends commands to the sidecar; API path fence rejects `../`; process routes 403 when not allowed.
- Pure-Python units (CRS record parsing, plane fit, stats maths, sidecar models) run without tools.
- A docker-compose `test` profile that runs the suite in the image against the mounted checkout as your uid (copy atlas's `compose.yaml` pattern).

## 9. Docs

- `README.md` — what it is for surveyors, install (apt + uv), a 5-minute walkthrough on a public sample.
- `docs/data-sources.md` — public test data: USGS 3DEP (EPT on AWS, `s3://usgs-lidar-public`, readable with `readers.ept` over HTTPS), Geoscience Australia ELVIS (elevation.fsdf.org.au, LAZ by order), AHN (Netherlands), OpenTopography. Licence/attribution for each. Verify URLs.
- `docs/atlas-showcase.md` — survey → fantasy map: `argus-survey run …` then `argus-atlas controls --dem <job>/dtm.tif --bbox …` and the atlas bake runbook (`../argus-atlas/docs/first-bake.md`). Note atlas plans its own Web Mercator grid, so the DTM's CRS doesn't matter, but a sub-metre DTM over a small site will hit atlas's native-zoom cap (14) — say what that means for the result.
- `CLAUDE.md` — orientation for agents, in the style of `../argus-atlas/CLAUDE.md`, with §3 as its conventions.
- `CHANGELOG.md` (Keep a Changelog), `TODO.md` (follow-ups: LandXML/DXF export, breaklines, TIN volumes, M3C2 change, classification beyond ground, photogrammetry ingest).

## 10. Build order

Work in milestones; each one ends with green `make lint` and `make test` (with tools) and a commit on `dev`. Don't start the next until the previous is done.

1. **Scaffold.** Copier template, `git init`, branch `dev`, Dockerfile (§2), compose with a `test` profile, standalone CI, CLAUDE.md skeleton. `doctor` reports versions/drivers/geoid grids.
2. **Foundation.** `models.py`, `config.py`, `tools.py` runner, `jobs.py`, `crs.py`; fixtures generator; `info` and `ingest`.
3. **QA + ground + surfaces + contours**, and `run`.
4. **Volumes, cut/fill, change, accuracy, profile** — the numbers surveyors will check. Analytic tests first.
5. **Report.**
6. **Server** (read routes, then guarded process routes with SSE).
7. **Viewer + docs** (README walkthrough on a real public dataset, data sources, atlas showcase).

Tag `v0.1.0rc1` once 1–5 are done; that's already a usable CLI to show people.

## 11. Definition of done (v0.1.0)

- `argus-survey run sample.laz --job demo --res 0.5 --interval 0.5` on a public dataset produces COPC, QA, DTM/DSM/CHM, hillshade, contours and `report.html`, with a complete command log and CRS record.
- `volume`, `cutfill`, `change`, `accuracy` work and are covered by analytic tests.
- Missing vertical datum is refused or loudly recorded as assumed — never silent.
- The image builds; CI is green with the tools required; `/health` reflects the write posture.
- README walkthrough reproduces from a clean machine with only apt + uv.

## 12. Ask before

- Adding any pip dependency beyond §2, or any network fetch in the server.
- Choosing a different point engine than PDAL, or adding Python GIS bindings.
- Publishing anything (GitHub repo creation, PyPI, GHCR) — the owner (smk762) does that.
