# CLAUDE.md — argus-survey

Guidance for AI agents working in this repo. Human-facing usage lives in [README.md](README.md); this file is the orientation an agent needs to change code safely.

## What this is

A point-cloud toolkit for **land surveyors** (first audience: Western Australian civil surveyors — MGA2020 zones 49–51, PCG2020 and the other WA LSG2020 grids, AHD via AUSGeoid2020). Input is LiDAR, drone photogrammetry or terrestrial-scanner points (LAS/LAZ/COPC, E57); output is what a surveyor hands over: QA report, ground classification, DTM/DSM/CHM, contours, stockpile volumes, cut/fill, profiles, change between epochs and an accuracy statement against check points (ASPRS ed. 2, NVA + VVA). Every run is traceable: every datum, operation, pipeline and argv is recorded in `job.json`.

It is judged on **correct datums, honest accuracy numbers and reproducible outputs**, not visuals. Prefer one deliverable done properly over three done loosely. It is a standalone peer in the Argus suite (port **8107**); its rasters are plain GeoTIFFs that `argus-atlas controls --dem` can consume. No code dependency on atlas.

## Argus suite
<!-- argus-suite: keep this block; argus-ci checks for it -->
This repo is part of the Argus suite. The suite map below lists every sibling and what it owns; check it before
building something a sibling may already do. **On every release, update this repo's row and capabilities in
argus-cortex's SUITE.md** (a PR to argus-cortex in the same release).

@../argus-core/SUITE.md

Not checked out? Read https://github.com/smk762/argus-cortex/blob/dev/SUITE.md

## Toolchain (read before touching Docker/CI)

- **PDAL, GDAL and PROJ come from conda-forge**, pinned by `env/geo-linux-64.lock` (explicit URLs + md5), generated from `env/geo.yml` by `make geo-lock`. PDAL is not packaged by Debian or Ubuntu (24.04 and 26.04 both lack it), so "apt install pdal" is not an option; one conda-forge stack also guarantees PDAL links the same GDAL/PROJ whose CLIs we call. The image is `ubuntu:26.04` + that env at `/opt/geo` + Ubuntu's Python 3.14 in `/opt/venv`.
- **No Python GIS bindings** (see the table): the package only shells out to `pdal`, `gdal*`, `ogr2ogr`, `projinfo`. The unified `gdal` CLI (≥ 3.11) supplies `raster calc`, `raster zonal-stats`, `raster pixel-info -r bilinear` and `raster fill-nodata`, so no Python GDAL bindings are needed (the spec's `gdal_calc.py`/`gdallocationinfo` are replaced by these on purpose).
- **Datum grids** are fetched at image build from cdn.proj.org and verified against `env/proj-grids.sha256` (AUSGeoid2020, AUSGeoid09, GDA94→GDA2020 conformal+distortion, AGQG/AVWS, GEOID18). At run time `PROJ_NETWORK=OFF`, and `tools.run` forces it off for every child.
- `argus-survey doctor --strict` runs as the last image build step, so an image without a working toolchain cannot be built.

## Must never happen

Each line names what enforces it. An **UNENFORCED** line is a known gap: rely on review, and close it with a check
rather than more prose. When something slips through, add the missing check here and in code. Don't add a rule to
remember. Tests marked `tools` run only in the image (`make test-docker`, CI `image-test`), and
`test_rules::test_must_never_table_names_real_tests` fails when a test cited here no longer exists.

| Never | Enforced by |
|---|---|
| A height step (surfaces, contours, volume, cutfill, change, accuracy, profile) without a vertical datum, or an assumed datum that isn't recorded and shown red | `test_jobs::test_height_steps_refuse_without_vertical`, `test_report::test_assumed_vertical_gets_a_red_banner` |
| PROJ picking the transformation at run time: `filters.reprojection`, or a ballpark operation when a grid is missing | `test_crs_operations::test_select_refuses_when_only_grid_operations_lack_grids`, `::test_parse_optional_grid_marker_is_treated_as_required`, `test_rules::test_no_banned_pdal_stages` |
| A grid fetched mid-job (`PROJ_NETWORK` on for a child process) | `test_tools::test_run_forces_proj_network_off` |
| A PDAL run that exits 0 after points failed to reproject, accepted as success | `test_tools::test_fatal_stderr_turns_exit_zero_into_failure` (ingest passes `TRANSFORM_FATAL`) |
| An external command not in `job.json` (failed, missing or timed-out ones included), or run outside `tools.run` | `test_tools::test_run_failure_is_recorded_and_raised_with_stderr_tail`, `::test_run_missing_tool_is_recorded`, `::test_run_timeout_is_recorded_with_stderr`; ruff `TID251` bans `subprocess`, `os.system`, `os.popen` and asyncio subprocesses outside `tools.py` |
| A route that takes a filesystem path, an input outside `SURVEY_DATA_DIR`, or a job id outside `^[A-Za-z0-9][A-Za-z0-9._-]*$` | `test_server::test_path_fence`, `::test_ids_and_steps_validated`, `test_config::test_resolve_under_root_refuses_escape`, `::test_resolve_under_root_refuses_symlink_out`, `test_jobs::test_unsafe_ids` |
| A POST accepted without `SURVEY_ALLOW_PROCESS` | `test_server::test_process_routes_403_when_read_only` |
| A GET that changes `job.json` (map views are cached in `<job>/.web/`, never recorded) | `test_server::test_reads_never_change_the_sidecar` |
| Two steps running on one job at once | `test_server::test_one_step_per_job_at_a_time` (409) |
| An input written to | `compose.yaml` mounts `/data/in` `read_only: true`; `test_server::test_run_over_sse_then_read_products` (`tools`: a full run leaves the input's bytes and mtime unchanged) |
| Tiles merged when their parts don't add up to the source's point count | `test_tiling_units::test_parts_that_miss_points_are_refused_before_the_merge`, `::test_cores_partition_the_padded_extent` |
| Voids silently counted as zero volume | `test_volumes_analytic::test_voids_refused_unless_allowed` (`tools`) |
| An accuracy statement with a 95 % figure (ASPRS ed. 2 is RMSE only) | `test_report::test_report_covers_every_step_and_product` (`tools`) |
| JavaScript in `report.html` | `test_report::test_report_is_self_contained` (`tools`) |
| PDAL, GDAL, rasterio, laspy, pyproj or fiona as Python packages | `test_rules::test_no_python_gis_bindings_in_the_lock`, `::test_no_pip_install_of_gis_bindings_in_the_image` |
| `writers.copc` (it buffers the whole cloud; untwine builds the COPC) | `test_rules::test_no_banned_pdal_stages` |
| A binary file committed under `tests/` | `test_rules::test_no_binary_files_under_tests` |
| Private IPs, home or server paths, or non-noreply emails in `README.md` or `docs/` | CI `docs-check` (argus-ci `docs-check.yml`, hygiene job) |
| A stale `docs/cli.md`, or a lost heading that argus-sightline links to | `test_docs::test_cli_reference_is_current`, `::test_linked_headings_exist`; CI `docs-check` |
| A stale `schema/survey-wire.schema.json` | `test_wire::test_committed_schema_is_current` |
| A tracked file left out of the Docker build context, or an image that doesn't report its tag | `test_dockerignore::*`; `release.yml` "The pushed image reports the tag at /health" |
| A release tag without its `## [X.Y.Z]` CHANGELOG section | `release.yml` build job, before anything publishes |
| Work on `main` that `dev` lacks, or a PR into `main` other than the release PR from `dev` | CI `branches` job |

## Layout

Module docstrings say what each file does. What they don't:

- **`models.py` is the wire contract.** `JobSidecar` (`job.json`) is the API contract and `SURVEY_VERSION` its schema version. A wire change means: `argus-survey schema`, commit, then `npm run wire:local ../argus-survey` (or `wire:pull` after a tag) in [argus-sightline](../argus-sightline), which generates its TypeScript types from the schema.
- **`synthetic.py`** is both the test fixtures and the demo data (`synth DIR --kit` → `kit-truth.json`). Stockpiles and change are measured on the **DSM**: ground filtering correctly removes a pile from the DTM, which then reads ~10 % low on the kit's cone.
- **`docs/`** is published: argus-sightline imports `README.md` and `docs/*.md` at `dev` HEAD and deep-links to headings (`src/lib/surveyDocs.ts`). Rename a heading only with a Sightline change. `docs/cli.md` is generated (`make docs`).
- **`tests/`**: `conftest.py` marks tool-backed tests `tools`; they skip without the tools unless `SURVEY_TEST_REQUIRE_TOOLS=1`. `tests/data/` holds captured *text* tool output for the pure-Python parser tests.

`make help` lists the targets. The dev host has no PDAL/GDAL, so `make test` skips the tool tests and `make test-docker` is the real gate. Don't `docker run` the image as root against the checkout (root-owned `__pycache__`); compose runs as `${UID}:${GID}`.

## Conventions (non-negotiable)

1. **No silent datums.** Every job records horizontal CRS, vertical CRS (or `null` + an explicit `vertical_assumed` reason), geoid model, epoch where relevant, and units. Without a vertical CRS, height-dependent steps refuse unless `--assume-vertical EPSG:xxxx` is given, and the assumption is recorded and shown in red in the report.
2. **Record, don't assume.** Every PDAL pipeline JSON and every GDAL/PROJ argv goes into the sidecar in run order, with tool versions. Every input is pinned by SHA256 and size.
3. **Paths from clients are names under a root.** The API resolves inputs under `SURVEY_DATA_DIR` (`resolve_under_root`); artifacts are addressed by `(job_id, kind)`.
4. **Numbers come with their uncertainty.** A volume carries the surface resolution, each surface's void %, and the method. An accuracy statement says how many check points, how many fell outside coverage, and which standard/edition.
5. **Never overwrite inputs.** Outputs go into the job dir.
6. **One subprocess runner.** All PDAL/GDAL/PROJ calls go through `tools.run` (logs, times, captures stderr, appends to the sidecar); a non-zero exit raises `ToolError` with the argv and stderr tail.

## Gotchas

- **Never let PROJ pick a transformation at run time.** `crs.select_operation` asks `projinfo --hide-ballpark` for candidates, takes the first whose `+grids=` files are all on disk (or the one `--operation` names), and returns its PROJ string; PDAL then runs that exact string through `filters.projpipeline`. `filters.reprojection` with EPSG codes would let PROJ silently fall back to a ballpark operation when a geoid grid is missing (ellipsoidal heights labelled AHD — ~30 m wrong in Perth). `projinfo --grid-check discard_missing` was tested and does **not** reliably drop such candidates; the on-disk check is ours. A `@grid` (optional) marker is treated as required.
- **GDA94 → GDA2020 has several valid operations.** PROJ ranks the 7-parameter Helmert first; legacy WA cadastral/control-derived data often warrants the ICSM conformal + distortion grid. Record all candidates; let the user pin one (`--operation "GDA94 to GDA2020 (2)"`).
- **PDAL exits 0 when every point fails to reproject** (it logs "falls outside grid" per point and writes garbage). Ingest passes `fatal_stderr=TRANSFORM_FATAL` to the runner and reconciles counts and bounds afterwards. Keep both.
- `pdal info --summary` on a `.copc.laz` (readers.copc) has no LAS header block; COPC-ness comes from `reader`.
- Drone scale: every ingest stage is streamable (`pdal pipeline --stream`); `writers.copc` is **not** used because it buffers the whole cloud — untwine builds the COPC out of core.
- **writers.gdal's default radius mode smears** each point into neighbouring cells (radius res·√2): fine for IDW, wrong for max/count. DSM, density and CHM use `binmode: true`.
- `gdal_rasterize -a_srs` drops the vertical part of a compound CRS, and `gdal raster calc` then refuses to mix it with a PDAL raster ("Input spatial reference systems are inconsistent"). Rasters carry the job's full compound CRS; stamp it with `rasters.set_crs` after rasterizing.
- **`isnodata()` in `gdal raster calc` is always false when the output has no nodata (`--nodata none`)** — voids then count as zero volume, silently. The volume engine writes `--nodata nan` (no band can produce NaN). With an output nodata, nodata *inputs* arrive as NaN in arithmetic, so guard with `isnodata(...) ? … : …` first. And an expression naming no input (`"1"`) is rejected: write `isnodata(U) ? 1 : 1`.
- Volumes use `--pixels fractional`: the default cell-centre rule got a 5.5 × 4.25 m test polygon's area 28 % wrong at 1 m cells.
- The DSM defaults to IDW. A binned max reads high on slopes by ~slope × res/2 (≈ +6 % on a 1:2 stockpile at 0.25 m); `--dsm-method max` exists for roofs/canopy.
- `gdal raster pixel-info` can't always write GeoJSON to `/vsistdout/`; write a temp file.
- PDAL's automatic hexbin edge can be several times too coarse (coverage area doubled on the test scene); `qa.hexbin_edge` sizes it from density.
- **Viewer libraries**: MapLibre **5.24.0** UMD, not 6.x — 6 is ESM-only and its separate module worker can't start from a cross-origin CDN (GeoJSON sources silently never render; a blob-import wrapper didn't help). copc 0.0.9 + laz-perf 0.0.7 (web build via jsDelivr `+esm`, `locateFile` for the wasm) + three 0.186.1. Check the viewer in headless Chrome after any bump (`zenika/alpine-chrome` with `--use-angle=swiftshader`, `?points=1` auto-loads the cloud).
- The `serve` container runs the package installed in the image: `docker compose up -d --build serve` after changing the viewer or server.
- **Photogrammetry clouds** usually have `ReturnNumber = 0` and no classes: "first returns" selects nothing. Detect and record the source type; use all points for the DSM.
- The conda lock includes an x86-64-v3 (AVX2) microarch marker; very old CPUs can't run the image.
- **Branches:** `dev` is the default branch and the base of every PR; never push to `main`. A release fast-forwards `main` to a CI-passed `dev` commit and tags it, or, once `main` carries a release merge commit (it does), is a PR from `dev` into `main` whose merge commit is the one thing `main` may hold that `dev` lacks (identical tree; don't merge it back). Tags `vX.Y.ZrcN` / `vX.Y.Z`, each with a `## [X.Y.Z]` CHANGELOG section, which becomes the GitHub Release notes.
- Versioning is git-tag-derived (`hatch-vcs`); `_version.py` is generated. `structlog`, Pydantic v2, ruff line-length 120. Scaffolded from `argus-pkg-template`; CI is standalone (`.github/workflows/ci.yml`).
- Publishing (GitHub repo, PyPI, GHCR) is the owner's (smk762) job — never do it from here. Ask before adding a pip dependency beyond pydantic/structlog/typer/rich/fastapi/uvicorn/argus-cortex, or any network fetch in the server.
