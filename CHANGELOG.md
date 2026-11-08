# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/) (tags are the source of truth via hatch-vcs).

## [Unreleased]

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
