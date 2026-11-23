# HTTP API

`argus-survey serve` (or `make up`) starts the API and viewer on **:8107**. It runs the same steps as the CLI
and writes the same `job.json`. The request bodies, `HealthInfo`, the SSE payloads and `job.json` are all
defined in one JSON Schema,
[`schema/survey-wire.schema.json`](https://github.com/smk762/argus-survey/blob/dev/schema/survey-wire.schema.json)
(regenerate with `argus-survey schema`). Generate client types from it rather than from these tables.

## Read-only by default

Every `POST` is refused with `403` and
`{"detail": "processing is disabled on this host (set SURVEY_ALLOW_PROCESS=1 …)"}` unless `SURVEY_ALLOW_PROCESS`
is `1`, `true`, `yes` or `on`. `GET /health` reports the posture as `read_only` (`not SURVEY_ALLOW_PROCESS`), and
clients disable their write controls on it. A read never changes a job: map views are cached under
`<job>/.web/` and are not recorded in `job.json`.

Clients never send filesystem paths:

- **Inputs** are names relative to `SURVEY_DATA_DIR` (`/data/in` in the image), resolved under that root. A name
  that escapes it is refused (`400`), and one that doesn't exist is `404`. `GET /inputs` lists what is there.
- **Jobs** are ids matching `^[A-Za-z0-9][A-Za-z0-9._-]*$`, under `SURVEY_JOBS_DIR`.
- **Products** are addressed by `(job id, kind)`.

`serve --cors` allows the origins in `SURVEY_CORS_ORIGINS` (default `http://localhost:3000`) and exposes the
`Range` headers that COPC streaming needs.

## Routes

| Route | Returns |
|---|---|
| `GET /health` | `HealthInfo`: `{status, service, version, read_only, tools}`, inside the Argus suite's `ServiceHealth` body (argus-cortex), which adds `service_version`, `started_at`, `wire` (`survey_version`, the `job.json` schema version), `capabilities`, `checks`, `build` and `peer`. `status` is always `ok` and the route always answers `200`: no dependency check is registered, and a missing tool shows as `null` in `tools` and in full at `GET /doctor`. Not listed in `/openapi.json` |
| `GET /doctor` | the toolchain report, as `argus-survey doctor`, plus `ok` |
| `GET /inputs` | `{points, vectors, rasters, tables}`: names under `SURVEY_DATA_DIR`, by file type |
| `GET /jobs` | `{jobs: [JobDigest]}` |
| `GET /jobs/{id}` | the sidecar (`job.json`); see [Provenance](provenance.md) |
| `GET /jobs/{id}/{kind}` | a product by kind (`copc`, `dtm`, `report`, …), with HTTP Range (`206`), so COPC readers stream it. `404` if the job has no such product, `410` if it is recorded but missing on disk |
| `GET /jobs/{id}/web/{kind}` | a map view: a raster gives `RasterView` (`{kind, png, url, coordinates, range, ramp}`, lon/lat corners), a vector gives WGS 84 GeoJSON |
| `GET /jobs/{id}/web/{kind}/image` | the raster map view's EPSG:3857 PNG |
| `POST /jobs/{id}/{step}` | run a step, streamed as Server-Sent Events |
| `GET /viewer` (`/` redirects) | the viewer |

## Request bodies

`POST /jobs/{id}/{step}` takes a JSON body whose fields mirror the CLI options of that step (see the
[CLI reference](cli.md) and [Workflow steps](steps.md)). Unknown fields are rejected with `422`.

| `{step}` | Body | Notes |
|---|---|---|
| `ingest` | `IngestBody` | `inputs` (names), `t_srs`, `s_srs`, `assume_vertical`, `operation`, `crop`, `crop_srs`, `force`. Creates the job. |
| `qa` | `QaBody` | `density_res`, `min_density` |
| `ground` | `GroundBody` | `method`, `preset`, `params`, `keep_classes` |
| `surfaces` | `SurfacesBody` | `res`, `dtm`, `dsm`, `chm`, `fill_voids`, `dsm_method`, `chm_method` |
| `contours` | `ContoursBody` | `interval`, `index` (`0` or `null`: no index contours) |
| `volume` | `VolumeBody` | `polygon` (a file name) **or** `polygon_geojson` (a WGS 84 Polygon/MultiPolygon geometry drawn on the map) with an optional `name`; `base`, `surface`, `polygon_srs`, `allow_voids`, `resample`, `base_vertical` |
| `cutfill` | `CutfillBody` | `design`, plus a polygon as for `volume`; `surface`, `design_vertical`, `resample`, `allow_voids` |
| `change` | `ChangeBody` | `against` (a job id), `lod` or `lod_from_accuracy`, `surface`, `polygon`, `polygon_srs` |
| `accuracy` | `AccuracyBody` | `checks`, `crs`, `cols`, `checkpoint_rmse`, `class_cm`, `surface` |
| `profile` | `ProfileBody` | `line` (a WGS 84 LineString drawn on the map) **or** `line_file`; `line_srs`, `step` |
| `export` | `ExportBody` | `points` (`las` or `e57`), `polygon`, `polygon_srs`, `thin` |
| `report` | `EmptyBody` | `{}` |
| `run` | `RunBody` | `IngestBody` plus `res`, `interval`, `method`, `preset`, `fill_voids`. Creates the job. |

Drawn geometry is saved in the job directory under a content hash, so the recorded argv keeps pointing at
the exact geometry used. Steps other than `ingest` and `run` need the job to exist (`404`). Only one step
runs per job at a time: a second `POST` while one is running gets `409`. An unknown step is `404`.

## Server-Sent Events

The response to a `POST` is a `text/event-stream`. Each event's `data` is JSON:

| Event | Payload | When |
|---|---|---|
| `start` | `StartEvent` `{job, step}` | the step was accepted and is running |
| `step` | `StepEvent` `{step, state: start\|done, seconds}` | a step started or finished (`run` reports each of its steps) |
| `command` | `CommandEvent` `{index, step, tool, seconds, returncode}` | one tool call finished; `index` is its 1-based place in `job.json` → `commands` |
| `warning` | `WarningEvent` `{message}` | a warning, also appended to `job.json` → `warnings` |
| `done` | `DoneEvent` `{job, step, result, products, warnings}` | success: the step's own report, then the job's product kinds and warnings |
| `error` | `ErrorEvent` `{message, stderr}` | a refusal or tool failure; `stderr` is the failed tool's stderr tail |

The stream ends after `done` or `error`. A refusal (for example a missing vertical datum, see
[Datums](datums.md#what-is-refused)) arrives as `error` with the same sentence the CLI prints.
