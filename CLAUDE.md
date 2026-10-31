# CLAUDE.md — argus-survey

Guidance for AI agents working in this repo. Human-facing usage lives in [README.md](README.md); this file is the orientation an agent needs to change code safely.

## What this is

A point-cloud toolkit for **land surveyors** (first audience: Western Australian civil surveyors — MGA2020 zones 49–51, PCG2020 and the other WA LSG2020 grids, AHD via AUSGeoid2020). Input is LiDAR or drone photogrammetry points (LAS/LAZ/COPC); output is what a surveyor hands over: QA report, ground classification, DTM/DSM/CHM, contours, stockpile volumes, cut/fill, profiles, change between epochs and an accuracy statement against check points (ASPRS ed. 2, NVA + VVA). Every run is traceable: every datum, operation, pipeline and argv is recorded in `job.json`.

It is judged on **correct datums, honest accuracy numbers and reproducible outputs**, not visuals. Prefer one deliverable done properly over three done loosely. It is a standalone peer in the Argus suite (port **8107**); its rasters are plain GeoTIFFs that `argus-atlas controls --dem` can consume. No code dependency on atlas.

## Toolchain (read before touching Docker/CI)

- **PDAL, GDAL and PROJ come from conda-forge**, pinned by `env/geo-linux-64.lock` (explicit URLs + md5), generated from `env/geo.yml` by `make geo-lock`. PDAL is not packaged by Debian or Ubuntu (24.04 and 26.04 both lack it), so "apt install pdal" is not an option; one conda-forge stack also guarantees PDAL links the same GDAL/PROJ whose CLIs we call. The image is `ubuntu:26.04` + that env at `/opt/geo` + Ubuntu's Python 3.14 in `/opt/venv`.
- **Never pip-install PDAL/GDAL/rasterio/laspy/pyproj.** The package only shells out to `pdal`, `gdal*`, `ogr2ogr`, `projinfo`. The unified `gdal` CLI (≥ 3.11) supplies `raster calc`, `raster zonal-stats`, `raster pixel-info -r bilinear` and `raster fill-nodata`, so no Python GDAL bindings are needed (the spec's `gdal_calc.py`/`gdallocationinfo` are replaced by these on purpose).
- **Datum grids** are fetched at image build from cdn.proj.org and verified against `env/proj-grids.sha256` (AUSGeoid2020, AUSGeoid09, GDA94→GDA2020 conformal+distortion, AGQG/AVWS, GEOID18). At run time `PROJ_NETWORK=OFF`, and `tools.run` forces it off for every child.
- `argus-survey doctor --strict` runs as the last image build step, so an image without a working toolchain cannot be built.

## Layout

`src/argus_survey/`:

- `models.py` — Pydantic v2 wire types. **`JobSidecar` (`job.json`) is the API contract.** `SURVEY_VERSION` is its schema version.
- `config.py` — `SURVEY_*` env knobs, `process_allowed()`, `resolve_under_root()` (the API's path fence, ported from atlas).
- `tools.py` — **the one subprocess runner** (`run`, `run_json`, `pdal_pipeline`), `versions()`, PROJ search paths/grid lookup, `doctor()`.
- `crs.py` — `select_operation()`: explicit coordinate-operation choice (see conventions), CRS record parsing.
- `cli.py` — Typer CLI.

`tests/` — `conftest.py` marks tool-backed tests `tools`; they skip without the tools unless `SURVEY_TEST_REQUIRE_TOOLS=1`. `tests/data/` holds captured *text* tool output for pure-Python parser tests (no binary fixtures, ever).

## Commands

```bash
make install       # uv sync --locked --all-extras; `make lock` after changing dependencies
make lint          # ruff 0.15.16 check + format --check
make test          # host pytest: pure-Python tests run, tool tests SKIP (no PDAL on the host)
make test-docker   # the full suite in the image against this checkout, tools required
make image         # build the runtime image (doctor --strict inside)
make geo-lock      # re-resolve env/geo-linux-64.lock after editing env/geo.yml
```

The dev host has no PDAL/GDAL; `make test-docker` is the real gate. Don't `docker run` the image as root against the checkout (root-owned `__pycache__`); compose runs as `${UID}:${GID}`.

## Conventions (non-negotiable)

1. **No silent datums.** Every job records horizontal CRS, vertical CRS (or `null` + an explicit `vertical_assumed` reason), geoid model, epoch where relevant, and units. Without a vertical CRS, height-dependent steps (volumes, contours, accuracy) refuse unless `--assume-vertical EPSG:xxxx` is given, and the assumption is recorded and shown in red in the report.
2. **Record, don't assume.** Every PDAL pipeline JSON and every GDAL/PROJ argv goes into the sidecar in run order, with tool versions. Every input is pinned by SHA256 and size.
3. **Paths from clients are names under a root.** The API resolves inputs under `SURVEY_DATA_DIR` (`resolve_under_root`); artifacts are addressed by `(job_id, kind)`; job ids match `^[A-Za-z0-9][A-Za-z0-9._-]*$`. No route takes a filesystem path.
4. **Numbers come with their uncertainty.** A volume carries the surface resolution, each surface's void %, and the method. An accuracy statement says how many check points, how many fell outside coverage, and which standard/edition.
5. **Never overwrite inputs.** Outputs go into the job dir; inputs are read-only (compose mounts `/data/in` read-only).
6. **One subprocess runner.** All PDAL/GDAL/PROJ calls go through `tools.run` (logs, times, captures stderr, appends to the sidecar — failed commands too); a non-zero exit raises `ToolError` with the argv and stderr tail.

## Gotchas

- **Never let PROJ pick a transformation at run time.** `crs.select_operation` asks `projinfo --hide-ballpark` for candidates, takes the first whose `+grids=` files are all on disk (or the one `--operation` names), and returns its PROJ string; PDAL then runs that exact string through `filters.projpipeline`. `filters.reprojection` with EPSG codes would let PROJ silently fall back to a ballpark operation when a geoid grid is missing (ellipsoidal heights labelled AHD — ~30 m wrong in Perth). `projinfo --grid-check discard_missing` was tested and does **not** reliably drop such candidates; the on-disk check is ours. A `@grid` (optional) marker is treated as required.
- **GDA94 → GDA2020 has several valid operations.** PROJ ranks the 7-parameter Helmert first; legacy WA cadastral/control-derived data often warrants the ICSM conformal + distortion grid. Record all candidates; let the user pin one (`--operation "GDA94 to GDA2020 (2)"`).
- **Photogrammetry clouds** usually have `ReturnNumber = 0` and no classes: "first returns" selects nothing. Detect and record the source type; use all points for the DSM.
- The conda lock includes an x86-64-v3 (AVX2) microarch marker; very old CPUs can't run the image.
- **Branches:** `dev` is the default branch and takes feature PRs; `main` is protected and only receives `dev` → `main` release PRs. Tags `vX.Y.ZrcN` / `vX.Y.Z`, each with a CHANGELOG heading.
- Versioning is git-tag-derived (`hatch-vcs`); `_version.py` is generated. `structlog`, Pydantic v2, ruff line-length 120. Scaffolded from `argus-pkg-template`; CI is standalone (`.github/workflows/ci.yml`).
- Publishing (GitHub repo, PyPI, GHCR) is the owner's (smk762) job — never do it from here. Ask before adding a pip dependency beyond pydantic/structlog/typer/rich/fastapi/uvicorn/argus-cortex, or any network fetch in the server.
