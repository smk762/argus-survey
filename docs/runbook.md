# Runbook

Operating argus-survey day to day: where things live, how to read its refusals, and how to handle large
drone surveys.

## Layout on disk

```
data/in/            inputs (mounted read-only at /data/in) — point clouds, polygons, lines, check points, designs
data/jobs/<id>/     one directory per job (mounted at /data/jobs)
  job.json          the record: inputs + SHA256, CRS, operation, every command, every product + SHA256, reports
  points.copc.laz   the ingested cloud in the job CRS
  ground.copc.laz   the same points, ground-classified
  dtm.tif dsm.tif chm.tif hillshade.tif   surfaces on one snapped grid
  *_voids.tif       1 void, 0 data, 255 outside coverage
  qa.json density.tif boundary.geojson gaps.geojson coverage.tif
  contours.gpkg contours.geojson
  volume.json cutfill.json change.json dod.tif dod_lod.tif accuracy.json accuracy_residuals.csv
  profile.csv profile.svg report.html
  .web/             cached map views for the viewer (not deliverables; safe to delete)
```

Inputs are never written to. Re-running a step overwrites its own products and appends its commands to the
log; `ingest --force` starts a job over.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `SURVEY_DATA_DIR` | `/data/in` in the image | the only root the API resolves input names under |
| `SURVEY_JOBS_DIR` | `/data/jobs` in the image | job directories |
| `SURVEY_ALLOW_PROCESS` | off | `1/true/yes/on` enables `POST /jobs/{id}/{step}`; `/health.read_only` reports it |
| `SURVEY_CORS_ORIGINS` | `http://localhost:3000` | origins for `serve --cors` |
| `SURVEY_WORKERS` | min(4, CPUs) | parallel tiles for outlier/ground/height-above-ground |
| `PROJ_NETWORK` | `OFF` | forced off for every tool call: no grid is ever fetched mid-job |

Compose reads host paths and UID/GID from `.env` (see `.env.example`).

## Typical WA drone job

DJI/Pix4D/Agisoft outputs often carry GDA2020 (or WGS 84) coordinates with **ellipsoidal** heights, sometimes
labelled with a 2D CRS that says nothing about heights.

```bash
docker compose run --rm survey info /data/in/flight1/cloud.laz
docker compose run --rm survey run /data/in/flight1/ --job site-2026-10-31 \
    --assume-vertical ellipsoidal --t-srs EPSG:7850+5711 --res 0.25 --interval 0.5
```

`--assume-vertical ellipsoidal` states that the heights are ellipsoidal on the horizontal CRS's datum; the
target (MGA2020 zone 50 + AHD) then makes PROJ apply AUSGeoid2020, and the report records the operation,
the grid and the assumption. Check one control mark against the DTM before trusting the result — `accuracy`
with even a handful of marks will show a datum slip immediately as a large mean error.

If the drone software exported WGS 84, the WGS 84 → GDA2020 step is an ensemble transformation good to about
2 m; the job warns that it is not survey grade. Re-export in GDA2020 from the processing software when the
base station was on GDA2020 control.

## Reading refusals

Every refusal is a sentence telling you what to do. The common ones:

| Message starts | Meaning | Fix |
|---|---|---|
| `… have no CRS. Pass --s-srs` | the file has no CRS at all | state it: `--s-srs EPSG:7850` |
| `… heights cannot be converted from an unknown datum` | `--t-srs` has a vertical datum but the data has none | `--assume-vertical …` if you know it |
| `… depends on heights, but job … has no vertical datum` | a height step on a job ingested without one | re-ingest with `--assume-vertical` |
| `every operation … needs a datum grid that is not installed` | the geoid/datum grid is missing | add it to `env/proj-grids.sha256` and rebuild |
| `… Mercator 'metres' are not ground metres` | working CRS is Web Mercator | `--t-srs` with MGA/PCG/UTM |
| `… has units of 'foot'` | the data are in feet | `--t-srs` with a metric CRS: the conversion is explicit and recorded |
| `… declares geographic coordinates … extent is clearly projected` | GeoJSON without a `crs` member holding MGA coordinates | `--polygon-srs EPSG:7850` (or `--crop-srs`, `--line-srs`) |
| `… % of the polygon has no surface value` | > 5 % voids under a stockpile | `surfaces --fill-voids N`, or `--allow-voids` |
| `… grid … does not align` | a base/design raster on another lattice | snap it, or `--resample` (bilinear, recorded) |

## Stockpiles

- Use **`--surface dsm`** for stockpiles: SMRF's default slope (0.15) treats the crest of a steep pile as
  non-ground, so the DTM has a void (or a flattened top) there. Or tune ground with `--slope 0.6 --window 30`
  and check the DTM hillshade.
- Draw the polygon on clean ground just outside the toe. The `plane` base's RMS tells you how well a plane
  fits the toe: centimetres is good; decimetres means the pad isn't planar — use `lowest-perimeter` or a
  surveyed base surface (`--base surface:pad.tif`) instead.

## Large surveys

Ingest and surface rasterisation stream (bounded memory). Outlier flagging, SMRF/CSF and height-above-ground
run on tiles of about 8 million points with a buffer (25 m or 1.5 × the SMRF window for ground), in
`SURVEY_WORKERS` parallel processes. Budget about 1.5 GB RAM per worker. Disk: the COPC is about the size of
the input LAZ; ground classification writes a second copy.

## Check the toolchain

```bash
docker compose run --rm survey doctor          # versions, drivers, grids (SHA256), key operations
```

The image runs `doctor --strict` when it is built, so an image without a working toolchain can't be built.
