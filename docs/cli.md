# `argus-survey`

Point-cloud toolkit for land surveyors: QA, ground, DTM/DSM, contours, volumes, change and accuracy, with every datum, transform and command recorded.

**Usage**:

```console
$ argus-survey [OPTIONS] COMMAND [ARGS]...
```

**Options**:

* `-v, --verbose`: Log every tool call
* `--version`: Print the version
* `--help`: Show this message and exit.

**Commands**:

* `doctor`: Check PDAL/GDAL/PROJ: versions, drivers,...
* `info`: Header summary and CRS record of a point...
* `ingest`: Read point clouds, settle and record the...
* `synth`: Write a synthetic survey scene with known...
* `qa`: Point count, density, class/return...
* `ground`: Classify ground (SMRF default, CSF option)...
* `surfaces`: DTM (ground IDW), DSM (top-surface IDW or...
* `contours`: Contours from the DTM (contours.gpkg +...
* `run`: ingest → qa → ground → surfaces → contours...
* `volume`: Stockpile volumes over polygons against a...
* `cutfill`: Cut and fill between the existing surface...
* `change`: DEM of difference (this job − --against)...
* `accuracy`: Vertical accuracy against check points...
* `profile`: Sample DTM/DSM along a polyline →...
* `export`: Point cloud for ReCap / Civil 3D...
* `report`: Write the self-contained, printable...
* `schema`: Write the wire-contract JSON Schema...
* `serve`: Start the API and viewer on :8107...

## `argus-survey doctor`

Check PDAL/GDAL/PROJ: versions, drivers, datum grids, and the transformations surveyors rely on.

**Usage**:

```console
$ argus-survey doctor [OPTIONS]
```

**Options**:

* `--json`: Print the report as JSON
* `--strict`: Exit 1 if any required check fails
* `--no-digests`: Skip re-hashing the datum grids
* `--help`: Show this message and exit.

## `argus-survey info`

Header summary and CRS record of a point cloud (no job is created).

**Usage**:

```console
$ argus-survey info [OPTIONS] {path}
```

**Arguments**:

* `path <file>`: LAS/LAZ/COPC/E57 file  [required]

**Options**:

* `--json`: Print JSON
* `--help`: Show this message and exit.

## `argus-survey ingest`

Read point clouds, settle and record the CRS, and write one COPC (points.copc.laz).

**Usage**:

```console
$ argus-survey ingest [OPTIONS] {inputs}...
```

**Arguments**:

* `inputs... <list[Path]>`: LAS/LAZ/COPC/E57 files or directories of them  [required]

**Options**:

* `-j, --job <str>`: Job id under SURVEY_JOBS_DIR (created on first use)
* `--out <path>`: Job directory (its name is the job id)
* `--t-srs <str>`: Target CRS, e.g. EPSG:7850+5711 (MGA2020 z50 + AHD)
* `--s-srs <str>`: Source CRS when the files have none (or a wrong one)
* `--assume-vertical <str>`: Heights&#x27; datum when the input has none: EPSG:&lt;code&gt; or &#x27;ellipsoidal&#x27;
* `--operation <str>`: Pin a transformation by (part of) its name, e.g. &#x27;GDA94 to GDA2020 (2)&#x27;
* `--crop <path>`: Polygon file (GeoJSON/GPKG/SHP) to keep points inside
* `--crop-srs <str>`: CRS of the crop polygon if it has none
* `--force`: Redo an ingested job (deletes its products)
* `--help`: Show this message and exit.

## `argus-survey synth`

Write a synthetic survey scene with known answers (cone stockpile, building, trees) — for demos and tests.

**Usage**:

```console
$ argus-survey synth [OPTIONS] {out}
```

**Arguments**:

* `out <path>`: Output .las/.laz (with --kit: the directory to fill)  [required]

**Options**:

* `--epoch <int range>`: 2: the stockpile has grown 1 m at its apex  [default: 1; 1&lt;=x&lt;=2]
* `--photogrammetry`: No returns info, RGB, no ground under trees
* `--srs <str>`: CRS written to the file, or &#x27;none&#x27;  [default: EPSG:7850+5711]
* `--checks <path>`: Also write check points CSV here
* `--kit`: Write every demo input into OUT: two epochs, checks, polygons, design surface
* `--help`: Show this message and exit.

## `argus-survey qa`

Point count, density, class/return histograms, outliers, coverage boundary and gaps (qa.json).

**Usage**:

```console
$ argus-survey qa [OPTIONS]
```

**Options**:

* `-j, --job <str>`: Job id under SURVEY_JOBS_DIR (created on first use)
* `--out <path>`: Job directory (its name is the job id)
* `--density-res <float range>`: Density grid cell size (m)  [default: 1.0; x&gt;=0.01]
* `--min-density <float>`: Gap threshold in pts/m² (default: 25% of the median)
* `--workers <int range>`: Parallel tiles (default: SURVEY_WORKERS, else ≤4 as CPUs and ~4 GiB each allow)  [x&gt;=1]
* `--help`: Show this message and exit.

## `argus-survey ground`

Classify ground (SMRF default, CSF option) per buffered tile; every point is kept (ground.copc.laz).

**Usage**:

```console
$ argus-survey ground [OPTIONS]
```

**Options**:

* `-j, --job <str>`: Job id under SURVEY_JOBS_DIR (created on first use)
* `--out <path>`: Job directory (its name is the job id)
* `--method <str>`: smrf (default) or csf  [default: smrf]
* `--preset <str>`: stockpile: SMRF slope 1.0 so steep piles stay ground
* `--slope <float>`: SMRF slope (rise/run)
* `--window <float>`: SMRF max window (m)
* `--threshold <float>`: SMRF/CSF elevation threshold (m)
* `--scalar <float>`: SMRF elevation scalar
* `--cell <float>`: SMRF cell size (m)
* `--resolution <float>`: CSF cloth resolution (m)
* `--rigidness <int>`: CSF rigidness (1-3)
* `--keep-classes`: Don&#x27;t reset existing classes first
* `--workers <int range>`: Parallel tiles (default: SURVEY_WORKERS, else ≤4 as CPUs and ~4 GiB each allow)  [x&gt;=1]
* `--help`: Show this message and exit.

## `argus-survey surfaces`

DTM (ground IDW), DSM (top-surface IDW or max), CHM (height above ground) on a snapped grid.

**Usage**:

```console
$ argus-survey surfaces [OPTIONS]
```

**Options**:

* `-j, --job <str>`: Job id under SURVEY_JOBS_DIR (created on first use)
* `--out <path>`: Job directory (its name is the job id)
* `--res <float range>`: Cell size (m)  [default: 0.5; x&gt;=0.01]
* `--dtm / --no-dtm`: [default: dtm]
* `--dsm / --no-dsm`: [default: dsm]
* `--chm / --no-chm`: [default: chm]
* `--fill-voids <int range>`: Fill DTM voids up to N cells across (recorded)  [x&gt;=1]
* `--dsm-method <str>`: idw (unbiased, for volumes) or max (roof edges/canopy)  [default: idw]
* `--chm-method <str>`: max (binned) or radius (fewer voids at fine --res, wider crowns)  [default: max]
* `--workers <int range>`: Parallel tiles (default: SURVEY_WORKERS, else ≤4 as CPUs and ~4 GiB each allow)  [x&gt;=1]
* `--help`: Show this message and exit.

## `argus-survey contours`

Contours from the DTM (contours.gpkg + contours.geojson, index contours flagged).

**Usage**:

```console
$ argus-survey contours [OPTIONS]
```

**Options**:

* `-j, --job <str>`: Job id under SURVEY_JOBS_DIR (created on first use)
* `--out <path>`: Job directory (its name is the job id)
* `--interval <float range>`: Contour interval (m)  [default: 0.5; x&gt;=0.01]
* `--index <int>`: Every N-th contour is an index contour (0: none)  [default: 5]
* `--help`: Show this message and exit.

## `argus-survey run`

ingest → qa → ground → surfaces → contours → report, in one job.

**Usage**:

```console
$ argus-survey run [OPTIONS] {inputs}...
```

**Arguments**:

* `inputs... <list[Path]>`: LAS/LAZ/COPC/E57 files or directories  [required]

**Options**:

* `-j, --job <str>`: Job id under SURVEY_JOBS_DIR (created on first use)
* `--out <path>`: Job directory (its name is the job id)
* `--res <float range>`: Surface cell size (m)  [default: 0.5; x&gt;=0.01]
* `--interval <float range>`: Contour interval (m)  [default: 0.5; x&gt;=0.01]
* `--t-srs <str>`: Target CRS, e.g. EPSG:7850+5711
* `--s-srs <str>`: Source CRS when the files have none
* `--assume-vertical <str>`: EPSG:&lt;code&gt; or &#x27;ellipsoidal&#x27;
* `--operation <str>`: Pin a transformation by name
* `--crop <path>`: Polygon to keep points inside
* `--crop-srs <str>`: CRS of the crop polygon if it has none
* `--method <str>`: Ground filter: smrf or csf  [default: smrf]
* `--preset <str>`: Ground preset: stockpile
* `--fill-voids <int range>`: Fill DTM voids up to N cells  [x&gt;=1]
* `--workers <int range>`: Parallel tiles (default: SURVEY_WORKERS, else ≤4 as CPUs and ~4 GiB each allow)  [x&gt;=1]
* `--help`: Show this message and exit.

## `argus-survey volume`

Stockpile volumes over polygons against a plane, lowest-perimeter or surface base (volume.json).

**Usage**:

```console
$ argus-survey volume [OPTIONS]
```

**Options**:

* `-j, --job <str>`: Job id under SURVEY_JOBS_DIR (created on first use)
* `--out <path>`: Job directory (its name is the job id)
* `--polygon <path>`: Stockpile polygon(s) (GeoJSON/GPKG/SHP)  [required]
* `--base <str>`: plane | lowest-perimeter | surface:&lt;raster on the job grid&gt;  [default: plane]
* `--surface <str>`: dtm or dsm (the top of the pile)  [default: dtm]
* `--polygon-srs <str>`: CRS of the polygon if it has none
* `--allow-voids`: Accept &gt;5% voids (counted as zero)
* `--resample`: Warp a misaligned base surface (recorded)
* `--base-vertical <str>`: Vertical datum of a base surface that declares none
* `--help`: Show this message and exit.

## `argus-survey cutfill`

Cut and fill between the existing surface and a design surface (cutfill.json).

**Usage**:

```console
$ argus-survey cutfill [OPTIONS]
```

**Options**:

* `-j, --job <str>`: Job id under SURVEY_JOBS_DIR (created on first use)
* `--out <path>`: Job directory (its name is the job id)
* `--design <path>`: Design surface GeoTIFF in the job CRS  [required]
* `--polygon <path>`: Limit to polygon(s); default the design extent
* `--polygon-srs <str>`
* `--surface <str>`: Existing surface: dtm or dsm  [default: dtm]
* `--design-vertical <str>`: Design heights&#x27; datum if the file declares none
* `--resample`: Warp a misaligned design (bilinear, recorded)
* `--allow-voids`
* `--help`: Show this message and exit.

## `argus-survey change`

DEM of difference (this job − --against) with a level of detection (change_&lt;surface&gt;.json, dod_&lt;surface&gt;.tif).

**Usage**:

```console
$ argus-survey change [OPTIONS]
```

**Options**:

* `-j, --job <str>`: Job id under SURVEY_JOBS_DIR (created on first use)
* `--out <path>`: Job directory (its name is the job id)
* `--against <str>`: Earlier epoch&#x27;s job id (same jobs dir)  [required]
* `--lod <float>`: Level of detection (m); default 0.10
* `--lod-from-accuracy`: LoD95 = 1.96·√(σ₁²+σ₂²) from both epochs&#x27; NVA RMSE
* `--surface <str>`: dtm or dsm  [default: dtm]
* `--polygon <path>`: Only within these polygon(s)
* `--polygon-srs <str>`
* `--help`: Show this message and exit.

## `argus-survey accuracy`

Vertical accuracy against check points (ASPRS ed. 2: NVA pass/fail, VVA reported) → accuracy.json.

**Usage**:

```console
$ argus-survey accuracy [OPTIONS]
```

**Options**:

* `-j, --job <str>`: Job id under SURVEY_JOBS_DIR (created on first use)
* `--out <path>`: Job directory (its name is the job id)
* `--checks <path>`: Check points CSV: id,x,y,z[,description]  [required]
* `--cols <str>`: Column names for id,x,y,z  [default: id,x,y,z]
* `--crs <str>`: Check points&#x27; compound CRS, e.g. EPSG:7850+5711
* `--checkpoint-rmse <float>`: The check point survey&#x27;s own 1σ vertical RMSE (m), as the surveyor reports it (ASPRS RMSE_V2)
* `--class-cm <float>`: ASPRS vertical accuracy class to test NVA against: the #-cm class, RMSEV ≤ # cm
* `--surface <str>`: Surface to test (dtm)  [default: dtm]
* `--help`: Show this message and exit.

## `argus-survey profile`

Sample DTM/DSM along a polyline → profile.csv + profile.svg.

**Usage**:

```console
$ argus-survey profile [OPTIONS]
```

**Options**:

* `-j, --job <str>`: Job id under SURVEY_JOBS_DIR (created on first use)
* `--out <path>`: Job directory (its name is the job id)
* `--line <path>`: Line file (GeoJSON/GPKG/SHP)  [required]
* `--step <float>`: Sample spacing along the line (m)  [default: 0.5]
* `--line-srs <str>`: CRS of the line if it has none
* `--help`: Show this message and exit.

## `argus-survey export`

Point cloud for ReCap / Civil 3D (points.las or points.e57) with export notes: CRS, class legend.

**Usage**:

```console
$ argus-survey export [OPTIONS]
```

**Options**:

* `-j, --job <str>`: Job id under SURVEY_JOBS_DIR (created on first use)
* `--out <path>`: Job directory (its name is the job id)
* `--points <str>`: las (LAS 1.4, uncompressed) or e57, for ReCap  [default: las]
* `--polygon <path>`: Keep only points inside polygon(s)
* `--polygon-srs <str>`: CRS of the polygon if it has none
* `--thin <float>`: Keep the first point per voxel of this size (m)
* `--help`: Show this message and exit.

## `argus-survey report`

Write the self-contained, printable report.html for a job.

**Usage**:

```console
$ argus-survey report [OPTIONS]
```

**Options**:

* `-j, --job <str>`: Job id under SURVEY_JOBS_DIR (created on first use)
* `--out <path>`: Job directory (its name is the job id)
* `--help`: Show this message and exit.

## `argus-survey schema`

Write the wire-contract JSON Schema (job.json, request bodies, SSE events) that UIs generate types from.

**Usage**:

```console
$ argus-survey schema [OPTIONS]
```

**Options**:

* `-o, --output <path>`: Where to write the JSON Schema  [default: schema/survey-wire.schema.json]
* `--check`: Exit non-zero if the committed schema is stale (CI)
* `--help`: Show this message and exit.

## `argus-survey serve`

Start the API and viewer on :8107 (read-only unless processing is allowed).

**Usage**:

```console
$ argus-survey serve [OPTIONS]
```

**Options**:

* `--host <str>`: [default: 127.0.0.1]
* `-p, --port <int>`: [default: 8107]
* `--cors`: Enable CORS for $SURVEY_CORS_ORIGINS
* `--allow-process / --read-only`: Enable POST processing routes (default: $SURVEY_ALLOW_PROCESS)
* `--help`: Show this message and exit.
