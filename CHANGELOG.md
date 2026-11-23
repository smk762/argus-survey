# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/) (tags are the source of truth via hatch-vcs).

## [Unreleased]

### Changed
- **argus-cortex 0.8 → 0.16**, pinned `>=0.16.1,<0.17` (base dependency and the `server` extra). Nothing survey uses changed between them: `fs.safe_name`, `server.WriteGuard` and `wire.render_schema` behave as before, and the committed wire schema is byte-identical.
- **`GET /health` is the Argus suite's `ServiceHealth`** (argus-cortex's `install_health`; argus-cortex#212). `status`, `service`, `version`, `read_only` and `tools` keep their names, values and meaning, so `HealthInfo`, the wire schema and `job.json` are unchanged. The body gains `service_version`, `started_at`, `wire` (`{"survey_version": "1"}`, the `job.json` schema version), `capabilities` (empty), `checks` (empty), `build` and `peer`. No dependency check is registered, so `status` is still always `ok` and the route always answers `200`. Two things a client could notice: the response carries `Cache-Control: no-store`, and `/health` is no longer listed in `/openapi.json`.

### Added
- The compose `serve` container carries the suite's discovery labels (`argus.peer: survey`, `argus.service: argus-survey`, `argus.health: /health`, `argus.port: "8107"`), which argus-iris's agents read, and `ARGUS_PEER=survey`, so `/health` reports `peer`. Its healthcheck asks `/health?checks=0`.

### Fixed
- **`run` honours `force`:** `POST /jobs/{id}/run` accepted `"force": true` but dropped it, so re-running an ingested job was refused ("already ingested") and Sightline's redo from the capture panel never worked. `argus-survey run` gains `--force` to match `ingest`.

### Fixed
- **`doctor` NAD83(2011) → NAVD88 check** (#69): with no extent, `select_operation` passed no spatial test, and projinfo's default (`contains`, against the CRSs' own areas of use) dropped the CONUS-only GEOID18 operation. It passes `intersects` again when there is no bbox; `contains` still applies to the data's extent.

### Fixed (ai-code-auditor: 20 findings)
- **Safety:** a CRS definition must be spelled like one: an authority code (`EPSG:7850+5711`), an OGC URN, WKT, PROJJSON or a PROJ string. GDAL opens anything else as a file and fetches `http(s)://` URLs, so a path or URL given as a CRS was a local file read or an outbound request; `+init=` may only name `authority:code`. CRS *names* (`"GDA2020 / MGA zone 50"`) are no longer accepted.
- **Datums:** with the data's extent known, operation candidates must be valid over the whole of it (`projinfo --spatial-test contains`, was `intersects`). `--operation` ignores surrounding whitespace. A point `gdaltransform` can't place no longer aborts the extent lookup. The 2D CRS taken from a compound PROJJSON keeps a `BoundCRS`'s datum shift. Scale factors refuse when `proj -V` answers for fewer points than it was given.
- **Accuracy:** check points transformed into a geographic job CRS keep 10 decimals (6 was ~0.1 m of latitude).
- **QA gaps:** when at least half the covered cells are empty the median density is 0 and no gaps were reported; the empty cells are now the gaps, and the rule says so.
- **Profile:** a sample with a void on both sides is drawn as a marker instead of vanishing; ticks stay distinct for very small ranges.
- **Report:** a change's DoD figure is never another surface's raster (the generic `dod_lod` stands in only for the latest comparison).
- **Runner:** a command that could not be started (not executable, missing working directory) is recorded with exit 126 and no longer reported as a tool missing from PATH. A failed tile cancels the tiles not yet started.
- Synthetic vegetated check points obey the same exclusions as open ones. The Dockerfile guard reads continued `RUN` lines and every pip spelling; the shell-quoting test goes through a real shell; the web-view test checks the command log is untouched.

## [0.2.0] - 2026-11-19

### Fixed (ai-code-auditor sweep: 73 findings fixed, 12 closed as not applicable)
- **QA density and gaps:** a covered cell with no points became `-9999 / cell_area` (a large negative "density" at 0.5 m, silently dropped at 1 m), because `gdal raster calc` does not propagate nodata. Empty covered cells now count as zero-density gaps, so gap % can read higher than before, correctly.
- **Accuracy:** blunder screening without a class target (every VVA, and an NVA without `--class-cm`) tests each residual against 3 × the RMSE of the group's *other* residuals (leave-one-out); with the group's own RMSE a blunder could never be flagged in a group of 9 or fewer. Groups under 3 points are not screened, and the report says so. NVA pass/fail is judged at the printed 0.1 cm, so 7.0 cm meets a 7 cm class. Check-point CSV errors name the row; short rows, non-finite coordinates and cct's per-point error lines raise a clear error.
- **Datums:** vertical datums without EPSG codes are compared by name in volume/cut-fill (two code-less datums used to pass unchecked); `--design-vertical`/`--base-vertical` accept `ellipsoidal`. Coordinate epochs (`EPSG:x@epoch`, PROJJSON `CoordinateMetadata`) are recorded instead of refused. Oblique Mercator (Hotine) working CRSs are accepted. `--operation` falls through to a matching candidate whose grids are on disk. Scale factors read ellipsoid axes in their own units (Clarke's feet) and handle spheres.
- **Safety:** CRS definitions starting with `-` or `@` are refused (projinfo read `@file`). Report values are HTML-escaped by default; the command log uses POSIX shell quoting. CLI output no longer parses zone names, CRS fields or doctor details as Rich markup (the height reference was being swallowed).
- **Robustness:** a failed untwine merge or overlay render keeps the previous output, and none of them (export included) leaves a partial file or scratch directory; sidecar writes use a per-call temp file and the job's own sidecar updates (products, warnings, steps) hold the job lock; a failed `--force` re-ingest no longer leaves ingest marked done. Refused, timed-out and missing-tool commands are recorded in the command log (exit 125, 124, 127; a refusal read "exit 0"). Large polygon WKT no longer overflows the CSV reader; vector files with more than one layer are refused, naming the layers.
- Smaller fixes: DoD figure saturation scales with the LoD; web overlays are capped on the longer side and small rasters are not upscaled; `grid.snap` never puts the origin above the minimum; north-up checks refuse flipped rasters; NaN/Inf nodata is read; ENVI byte order is taken from the header; `profile` keeps one sidecar entry matching its files; profile ticks always terminate.

### Changed
- **Wire:** `res`, `interval` and `density_res` have a minimum of 0.01 m (API and CLI); `dsm_method` is `idw|max` and `chm_method` `max|radius`; `index` is ≥ 0. argus-sightline must regenerate its types.

- **Job ids are checked by argus-cortex's `fs.safe_name`** (argus-cortex 0.8, ai-code-auditor#56): the same rule as before (start alphanumeric, then letters, digits, `.`, `_` or `-`), now matched against the whole string and capped at 128 characters. argus-cortex (pydantic-only without extras) is now a base dependency, pinned `>=0.8,<0.9`, as is the `server` extra's.

## [0.1.2] - 2026-11-14

Reference docs, and release fixes: the image reports its tag.

### Added
- Reference docs, so argus-sightline can import them at `dev` HEAD (#42): `docs/datums.md` (Rule 1: what is refused, what counts as assumed, why red), `docs/steps.md` (each step's parameters, outputs and refusals), `docs/methods.md` (NVA/VVA accuracy and the three volume bases), `docs/provenance.md` (`job.json`), `docs/api.md` (routes, bodies, SSE, read-only posture) and a generated `docs/cli.md` (`make docs`, checked by a test). `docs/data-sources.md` covers the `--kit` truths. A test keeps every heading other repos link to.
- GitHub Releases, as in the rest of the suite: after PyPI publishes and the image passes its `/health` check, the release workflow creates a Release titled with the tag, whose notes are that version's CHANGELOG section, with the wheel and sdist attached (`rc`/`a`/`b`/`dev` tags are marked pre-release). A tag without a CHANGELOG section now fails in `build`, before anything publishes.

### Fixed
- The image reports the release's version (#51). The release passes the tag to the build as `VERSION`, which sets `SETUPTOOLS_SCM_PRETEND_VERSION` for building this package only (dependencies are synced before it is set), and the release fails unless the pushed image's `/health` version equals the tag as a PEP 440 version. `.dockerignore` is anchored so git reads it the way Docker does: `git ls-files -ci --exclude-from=.dockerignore` prints nothing.
- `POST /jobs/{id}/contours` accepts `index: 0` for no index contours, as the CLI's `--index 0` does (it was refused).

### Changed
- Release image tags follow PEP 440: `:latest` and `:X.Y` move only on a final release, never on a release candidate. The install check also runs `argus-survey --version`, which would have caught 0.1.0's broken bare install.

## [0.1.1] - 2026-11-13

Packaging fixes for 0.1.0.

### Fixed
- `pip install argus-survey` now gives a working CLI: `typer` and `rich` are base dependencies. In 0.1.0 they were only in the `cli` extra, so the installed `argus-survey` command failed with `No module named 'typer'`. `argus-survey[cli]` still works.
- CLI errors, tool stderr and warnings are printed literally. They went through Rich markup, so bracketed text such as `[server]` in an install hint, or PDAL's `[readers.las]`, was dropped or could raise a markup error.
- The image reports its real version. `.dockerignore` dropped tracked files (`.github/`, `data/in/.gitkeep`), so git in the build saw a dirty tree and the v0.1.0 image called itself `0.1.1.dev0+g087758b45.d20261113`, in `--version`, `/health` and every `job.json`'s `package_version`. A test now fails if `.dockerignore` excludes anything tracked.

### Added
- README: installing from PyPI, and the toolchain it needs.

## [0.1.0] - 2026-11-13

First release, published to PyPI and GHCR.

### Changed
- Accuracy follows the text of ASPRS Positional Accuracy Standards, Edition 2, Version 2 (2024): §7.16.1 statement wording (with the reduced-checkpoint statement below 30 points), min/max/median (§7.16), bias flagged above 25 % of the target RMSE and blunders beyond 3 × it (§7.2), a horizontal-accuracy note for photogrammetry (§7.6) (#3).

### Added
- `export --points las|e57 [--polygon P] [--thin S]` (and `POST /jobs/{id}/export`): the classified cloud as uncompressed LAS 1.4 (CRS in the header) or double-precision E57 for ReCap / Civil 3D, with a notes file stating the CRS, vertical datum, grid-to-ground factor and ASPRS class counts; streaming, count-reconciled; recorded in `job.json` `exports` (#8).
- Grid-to-ground: ingest records `scale_factors` (point scale k and convergence from `proj -V`, k's range over the extent, height factor R/(R+h) from the ellipsoidal height via the explicit geoid operation, combined factor); volume and cut/fill reports add ground-scaled areas and volumes (#12).
- E57 ingest (terrestrial scanners) via `readers.e57`; `--s-srs` required (E57 has no CRS); new source type `terrestrial` (#20).
- `ground --preset stockpile` (SMRF slope 1.0) keeps piles at the angle of repose as ground (#18).
- `surfaces --chm-method radius`: highest point within res·√2, for sparse canopy at fine resolution (#19).
- Wire contract for UIs: `argus-survey schema [--check]` writes `schema/survey-wire.schema.json` (job.json, digests, health, every request body and SSE event) with `argus_cortex.wire`; the API's request bodies moved to `models.py`, and `/health` and the SSE payloads are built from the models. [argus-sightline](https://github.com/smk762/argus-sightline) generates its types from it.
- `volume` and `cutfill` over the API take a polygon drawn on the map (`polygon_geojson`, a WGS 84 GeoJSON geometry, plus an optional zone `name`) instead of a file, the way `profile` takes a drawn line.
- `argus-survey synth DIR --kit` (`make demo-kit`): every demo input with known answers: two epochs, check points, stockpile and pad polygons and a design surface, plus `kit-truth.json`.
- Viewer (`/viewer`): MapLibre map of surfaces, density, change, contours and coverage; the classified cloud streamed from the COPC (copc.js + laz-perf + three.js) coloured by elevation/class/intensity; job CRS and warnings; profile drawing when processing is allowed.
- Docs: README walkthrough on public USGS 3DEP data (`make sample`), WA datum table, runbook, data sources, argus-atlas showcase. `make demo`/`up`/`down`.
- Ingest refuses geographic and Web Mercator working CRSs (Mercator metres are not ground metres).
- API on :8107 (`argus-survey serve`, compose `serve`): health, doctor, inputs, jobs, products by kind with HTTP Range (COPC streams), web map views (Web Mercator PNG + corners, WGS 84 GeoJSON), and `POST /jobs/{id}/{step}` for every step streaming Server-Sent Events. Read-only unless `SURVEY_ALLOW_PROCESS`; inputs are names under `SURVEY_DATA_DIR`.
- Docs: README API route table, step names and body models (pointing at the wire schema); how check points are split into NVA/VVA; the 0.10 m default LoD; `synth DIR --kit` syntax; which commands take `--job`/`--out`. SPEC.md marked as the historical v0 brief, with where it diverges.

## [0.1.0rc1] - 2026-10-31

First release candidate: a usable CLI (milestones 1–5 of the build plan).

### Added
- `report`: one self-contained, printable `report.html` per job (no JavaScript, embedded thumbnails), with a red banner when the vertical datum was assumed, every product's SHA256, figures linked to the commands that produced them, and the full command log. `run` now ends with it.
- Scaffold from argus-pkg-template; Docker image on Ubuntu 26.04 with PDAL 2.10 / GDAL 3.13 / PROJ 9.9 from a pinned conda-forge lockfile, and datum grids (AUSGeoid2020, AUSGeoid09, GDA94→GDA2020 conformal+distortion, AGQG, GEOID18) verified by SHA256.
- `argus-survey doctor`: tool versions, PDAL/GDAL drivers and algorithms, datum grids, and the surveyor transformations (GDA2020/GDA94 → MGA + AHD, NAD83(2011) → NAVD88) resolved without ballpark fallbacks.
- One subprocess runner that records every command, and explicit coordinate-operation selection that refuses rather than falling back to a ballpark transform.
- `info` and `ingest`: CRS records (compound, ellipsoidal, user-supplied, assumed), `--t-srs` through an explicit PROJ operation with axis-order compensation, crop polygons with CRS sanity checks, streaming ingest + untwine COPC for drone-scale clouds, point-count reconciliation, LiDAR/photogrammetry detection.
- `qa`: density (mean/p5/p50/p95), class and return histograms (ASPRS names), outlier count, coverage boundary, density gaps.
- `ground`: SMRF (default) or CSF after statistical outlier flagging, per buffered tile with an exact partition check; every point kept.
- `surfaces`: DTM/DSM/CHM on a snapped grid, void masks and void % of the covered area, optional void fill, hillshade, resolution warning from ground density.
- `contours`: GPKG + GeoJSON with index contours. `run`: the whole chain.
- `volume` (stockpiles: plane / lowest-perimeter / surface base), `cutfill` (design vs surface), `change` (DoD with LoD, optional polygon), `accuracy` (ASPRS ed. 2: NVA/VVA RMSE, check-point accuracy, ≥30 rule, transformed check points), `profile` (CSV + SVG). One volume engine with fractional boundary cells; analytic tests (cone on a tilted plane at three resolutions, wedge, cut/fill signs, voids).
- `synth`: synthetic survey scenes with analytically known answers (used by the tests and for demos).
