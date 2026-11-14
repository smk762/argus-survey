# Provenance

Every job directory holds a `job.json`: the record of what went in, which datum and transformation were
used, every command that ran, and every product that came out. It is enough to check a deliverable or
re-run it. It is also the API contract: `GET /jobs/{id}` returns it, and its JSON Schema (`JobSidecar`) is in
[`schema/survey-wire.schema.json`](https://github.com/smk762/argus-survey/blob/dev/schema/survey-wire.schema.json).

## What job.json records

| Field | What it holds |
|---|---|
| `schema_version` | The sidecar's schema version. It changes only on a breaking change to the record. |
| `job_id`, `created_at`, `updated_at` | Identity and timestamps (UTC). |
| `package_version` | The argus-survey version that created the job (`ingest --force` starts it over). |
| `tools` | PDAL, GDAL and PROJ versions. |
| `inputs` | Every input file as given to `ingest`: path, **SHA256** and size, with its point count, bounds, dimensions, LAS version and point format. |
| `crs` | The job's CRS record: horizontal and vertical CRS, `height_reference`, `vertical_assumed`, `geoid`, `epoch`, `source`. See [Datums](datums.md#no-silent-datums-rule-1). |
| `ingest` | The source CRS as read; the **transformation** (name, stated accuracy, grids, the exact PROJ pipeline, `chosen_by` proj-default or user, every candidate considered with any missing grids); any crop; counts in and out; the detected source type (LiDAR, photogrammetry, terrestrial); per-dimension statistics; and the grid-to-ground **scale factors** at the site. |
| `qa`, `ground`, `surfaces`, `contours` | Each step's report: parameters as applied (including preset and tiling), counts, density and gaps, the snapped grid, each surface's method and void %. |
| `volumes` | One report per volume or cut/fill run: base, method, sign convention, cell size, void % per surface, per-zone figures (grid and ground-scaled), and any resampling. |
| `change`, `changes` | The latest comparison, and the latest per surface: the other job, **both sidecars' SHA256s**, LoD and its rule, gain, loss and areas. |
| `accuracy` | The statement, standard and edition, the check-point file (SHA256) and its CRS and transformation, the groups, every residual, and the notes. See [Methods](methods.md#vertical-accuracy-nva-and-vva). |
| `profiles`, `exports` | Each profile line and spacing; each point export with its CRS, classes and thinning. |
| `commands` | **Every external command, in run order**, failed ones included: step, tool, exact argv, the PDAL pipeline JSON fed on stdin, start time, duration and exit code. |
| `products` | Every output by kind (`copc`, `dtm`, `dsm`, `volume`, `report`, …): path in the job dir, **SHA256**, size, producing step and description. |
| `timings`, `steps_done` | Seconds per step; the steps that finished. |
| `warnings` | Everything a step warned about (an approximate transformation, voids under a polygon, few check points, …). The report shows them at the top. |

## Reading it

- **Was the data altered?** Compare an input's SHA256 with the file you have. Inputs are never written to.
- **Which geoid?** `crs.geoid` and `ingest.transform.grids`. A job with no transformation kept the file's heights.
- **How was this raster made?** Find its product, then the `commands` entries with the same `step`: the PDAL
  pipeline and GDAL argv are verbatim. The report numbers its figures to these entries (`#cmd-N`).
- **Is this the same deliverable?** Compare product SHA256s. Re-running a step replaces its products and appends
  its commands, so the log keeps the history of the job.

The report's closing section is this command log, and its CRS table is `crs` and `ingest.transform`, so the
printed `report.html` and `job.json` tell the same story.

Map views cached for the viewer in `<job>/.web/` are not deliverables and are not recorded: reading a job over
the API never changes `job.json`.
