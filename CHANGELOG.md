# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/) (tags are the source of truth via hatch-vcs).

## [Unreleased]

### Added
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
