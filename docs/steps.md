# Workflow steps

What each step does, the parameters that matter, what it writes and when it refuses. Every option of every
command is in the [CLI reference](cli.md), generated from the CLI itself. Over HTTP, each step is
`POST /jobs/{id}/{step}` with a body whose fields mirror these options ([HTTP API](api.md#request-bodies)).

All job steps take `--job ID` (a directory under `SURVEY_JOBS_DIR`) or `--out DIR`, write their products into
the job directory, and append every command they run to `job.json` ([Provenance](provenance.md)). The usual
order is ingest → qa → ground → surfaces → contours, then any of volume, cutfill, change, accuracy and
profile, then report. `run` does the first five plus the report.

## ingest

Reads LAS/LAZ/COPC/E57 files (or directories of them), settles the job's CRS, transforms the points into it
and writes one cloud.

| Parameter | Meaning |
|---|---|
| `INPUTS…` | the point clouds; all must share one CRS |
| `--t-srs` | the CRS to work in, e.g. `EPSG:7850+5711` (MGA2020 zone 50 + AHD). Omitted: the input's CRS. |
| `--s-srs` | the input's CRS, when the files have none or a wrong one (E57 always needs it) |
| `--assume-vertical` | the heights' datum when the input declares none: `EPSG:<code>` or `ellipsoidal` (recorded, shown in red) |
| `--operation` | pin one transformation by (part of) its name, e.g. `"GDA94 to GDA2020 (2)"` |
| `--crop`, `--crop-srs` | keep only points inside a polygon |
| `--force` | start an ingested job over (deletes its products) |

**Writes** `points.copc.laz`. **Records** every input's SHA256, the source and job CRS, the selected
operation and all candidates, point counts in and out (reconciled), the source type (LiDAR, photogrammetry,
terrestrial) and the combined grid-to-ground scale factor at the site.
**Refuses** data with no CRS, a target vertical datum the input can't reach, a missing datum grid, a
non-metric or Web Mercator working CRS, and a transformation that leaves points outside its grid.
See [Datums](datums.md).

## qa

Point count, density, class and return histograms, outliers, coverage boundary and gaps.

| Parameter | Meaning |
|---|---|
| `--density-res` | density grid cell size, m (default 1.0) |
| `--min-density` | gap threshold, pts/m² (default 25 % of the median density) |

**Writes** `qa.json`, `density.tif`, `coverage.tif`, `boundary.geojson` (hexbin coverage, edge sized from the
density) and `gaps.geojson`. Outliers are counted per tile at 3σ, and only counted: `ground` flags them.

## ground

Classifies ground on buffered tiles. Every point is kept, reclassified.

| Parameter | Meaning |
|---|---|
| `--method` | `smrf` (default) or `csf` |
| `--preset stockpile` | SMRF slope 1.0, so steep piles stay ground |
| `--slope`, `--window`, `--threshold`, `--scalar`, `--cell` | SMRF parameters (they override a preset) |
| `--resolution`, `--rigidness`, `--threshold` | CSF parameters |
| `--keep-classes` | don't reset existing classes first |

**Writes** `ground.copc.laz`. Each tile resets classes, flags outliers as class 7 and runs the filter
ignoring them. Photogrammetry clouds' missing return numbers are normalised to 1-of-1 (recorded). The report
gives ground density and spacing. Runs without a vertical datum.

## surfaces

DTM, DSM and CHM on one snapped grid (origin ⌊min / res⌋ · res), so two epochs at the same cell size share
cells.

| Parameter | Meaning |
|---|---|
| `--res` | cell size, m (default 0.5) |
| `--no-dtm`, `--no-dsm`, `--no-chm` | skip a surface |
| `--fill-voids N` | fill DTM voids up to N cells across, re-masked to the coverage (recorded) |
| `--dsm-method` | `idw` (default, unbiased for volumes) or `max` (roof edges, canopy: reads ≈ slope × res/2 high on slopes) |
| `--chm-method` | `max` (binned) or `radius` (fewer voids at fine `--res`, wider crowns) |

**Writes** `dtm.tif` (IDW of ground within res·√2), `dsm.tif` (first returns for LiDAR, all points for
photogrammetry), `chm.tif`, `*_voids.tif` (1 void, 0 data, 255 outside coverage) and `hillshade.tif`. Each
surface's void % is measured inside the coverage. **Refuses** without a vertical datum.

## contours

| Parameter | Meaning |
|---|---|
| `--interval` | contour interval, m (default 0.5) |
| `--index N` | every Nth contour is an index contour (default 5; 0 for none) |

**Writes** `contours.gpkg` and `contours.geojson` from the DTM, each line with its elevation and an `idx` flag.
**Refuses** without a vertical datum.

## volume

Stockpile volumes over one or more polygons, against a base.

| Parameter | Meaning |
|---|---|
| `--polygon` | the stockpile polygon(s); each feature is a zone |
| `--base` | `plane` (default), `lowest-perimeter` or `surface:<raster>`; see [Volume bases](methods.md#volume-bases) |
| `--surface` | `dtm` (default) or `dsm`. Use `dsm` for stockpiles: ground filtering removes the pile from the DTM. |
| `--allow-voids` | accept more than 5 % voids under a polygon (counted as zero) |
| `--resample`, `--base-vertical` | for a `surface:` base: warp a misaligned one (recorded); state the datum of one that declares none |

**Writes** `volume.json`: per zone the area, volume above and below the base, net, void %, base description
and fit RMS, in grid units and ground-scaled. **Refuses** without a vertical datum, over 5 % voids, and a base
raster in another CRS or on another grid.

## cutfill

Cut and fill between the existing surface and a design surface.

| Parameter | Meaning |
|---|---|
| `--design` | design GeoTIFF in the job CRS |
| `--polygon` | limit to polygon(s); default the design's extent |
| `--surface` | existing surface, `dtm` (default) or `dsm` |
| `--design-vertical` | the design's height datum if the file declares none |
| `--resample`, `--allow-voids` | as for `volume` |

**Writes** `cutfill.json`: *fill* = design above existing (material to bring in), *cut* = existing above design.
**Refuses** a design in another CRS or on another grid unless `--resample` is given. The demo kit's
`design.tif` is on a 0.5 m grid, so build the surfaces at `--res 0.5`.

## change

DEM of difference, this job (later) minus an earlier job, on the overlap of the two.

| Parameter | Meaning |
|---|---|
| `--against` | the earlier epoch's job id |
| `--surface` | `dtm` (default) or `dsm` |
| `--lod` | level of detection, m (default 0.10, recorded as `default 0.1 m`) |
| `--lod-from-accuracy` | LoD = 1.96·√(σ₁² + σ₂²) from both epochs' NVA RMSE (both need `accuracy`) |
| `--polygon` | limit to polygon(s) |

**Writes** `change_<surface>.json`, `dod_<surface>.tif` and `dod_<surface>_lod.tif` (|Δ| below the LoD set to 0).
The product kinds `change`, `dod` and `dod_lod` point at the latest run. Gain, loss and net volumes, areas,
and both sidecars' SHA256s are recorded. **Refuses** epochs in different CRSs or on unaligned grids, and jobs
without a vertical datum.

## accuracy

Vertical accuracy of a surface against check points, ASPRS Edition 2.

| Parameter | Meaning |
|---|---|
| `--checks` | CSV: `id,x,y,z[,description]`; a description starting `veg` or `vva` makes a point VVA |
| `--cols` | column names for id, x, y, z (default `id,x,y,z`) |
| `--crs` | the check points' compound CRS, e.g. `EPSG:7850+5711` |
| `--checkpoint-rmse` | the check survey's own 1σ vertical RMSE, m |
| `--class-cm` | the #-cm class NVA is tested against |
| `--surface` | `dtm` (default) or `dsm` |

**Writes** `accuracy.json` and `accuracy_residuals.csv`. The method is in
[Vertical accuracy](methods.md#vertical-accuracy-nva-and-vva). **Refuses** without a vertical datum.

## profile

| Parameter | Meaning |
|---|---|
| `--line`, `--line-srs` | the line file, and its CRS if it has none |
| `--step` | sample spacing along the line, m (default 0.5) |

**Writes** `profile.csv` and `profile.svg`: bilinear samples of the DTM and DSM along the densified line. Voids
break the line rather than being bridged. **Refuses** without a vertical datum.

## export

Point cloud for ReCap or Civil 3D.

| Parameter | Meaning |
|---|---|
| `--points` | `las` (LAS 1.4, uncompressed, CRS in the file) or `e57` (no CRS field) |
| `--polygon`, `--polygon-srs` | keep only points inside polygon(s) |
| `--thin` | keep the first point per voxel of this size, m |

**Writes** `points.las` or `points.e57` and a notes file with the CRS and class legend. It uses the classified
cloud when `ground` has run.

## report

**Writes** `report.html`: a self-contained, printable page with no scripts. It has the datum banners (red for an
assumed or missing vertical datum, see [Why red](datums.md#why-red)), warnings, the CRS and transformation
with every candidate, each step's figures and thumbnails numbered to the commands that made them, and the
full command log.

## run

`ingest → qa → ground → surfaces → contours → report` in one job. It takes ingest's options plus `--res`,
`--interval`, `--method`, `--preset` and `--fill-voids`. The steps record exactly as they would one by one.
