# Datums

How argus-survey settles and records the datum of every height it touches. For which flags to give
for common WA inputs, see [WA datums in one table](../README.md#wa-datums-in-one-table); for the exact
wording of each refusal and its fix, see [Reading refusals](runbook.md#reading-refusals).

## No silent datums (Rule 1)

Every job records, in `job.json` → `crs`:

- the **horizontal CRS** (name, EPSG code, units);
- the **vertical CRS**, or none;
- `height_reference`: `gravity` (e.g. AHD), `ellipsoidal`, or `none`;
- `vertical_assumed`: why the vertical datum was assumed, when it was not read from the data;
- `geoid`: the geoid grid a transformation applied to reach the job's heights (e.g. AUSGeoid2020);
- `epoch`, for dynamic datums;
- `source`: whether the CRS came from the `file`, the `user` (`--s-srs`, `--t-srs`), or was `transformed`.

A height never changes datum without that change being recorded, and a datum is never filled in by
default. If the data don't say what their heights are, the job says "none" until you do.

### What is refused

| Situation | Refused | Unless |
|---|---|---|
| The input has no CRS at all | `ingest` | `--s-srs EPSG:…` states it (recorded as user-supplied) |
| `--t-srs` has a vertical datum but the input heights have none | `ingest`: heights can't be converted from an unknown datum | `--assume-vertical` states what they are |
| `--t-srs` is 2D and the input heights have a datum | nothing: the job keeps the source heights explicitly (`EPSG:<h>+<v>`, or 3D for ellipsoidal) | — |
| The job has no vertical datum | every height-dependent step: `surfaces`, `contours`, `volume`, `cutfill`, `change` (both jobs), `accuracy`, `profile` | re-ingest with `--assume-vertical` |
| A transformation's every candidate needs a grid that isn't installed | `ingest`, `accuracy` (check points) | the grid is added to the image |
| PROJ offers only a ballpark operation | the same | — (a ballpark ignores datum and geoid differences) |
| `--assume-vertical` on data that already declare a vertical datum | `ingest` | use `--s-srs` to override a wrong file CRS |
| A base or design raster with no CRS, another horizontal CRS, or another vertical datum | `volume --base surface:…`, `cutfill` (rasters are never transformed vertically) | it is in the job CRS. One that declares no vertical datum is accepted when `--base-vertical` / `--design-vertical` states the job's (recorded as a job warning) |
| Two epochs in different CRSs | `change` | both jobs are in the same CRS |

`qa`, `ground` and `export` don't depend on what the heights mean, so they run without a vertical datum.

Around Perth AUSGeoid2020's separation is about 30 m. A pipeline that quietly skipped a missing geoid
grid would label ellipsoidal heights AHD and be ~30 m wrong while looking correct, which is why the
refusals above exist.

### What counts as assumed

A vertical datum is **assumed** when you state it rather than the data declaring it:

- `--assume-vertical EPSG:5711` (or any vertical EPSG code): the heights are taken to be on that datum.
- `--assume-vertical ellipsoidal`: the heights are taken to be ellipsoidal heights on the horizontal CRS's
  datum. The common case is drone output labelled MGA2020 with GNSS heights.
- `--base-vertical` / `--design-vertical` on a raster that declares no vertical datum.

The assumption is written to `crs.vertical_assumed` as a sentence, for example
`--assume-vertical EPSG:5711: the input declares no vertical datum; the user stated its heights are AHD height`.
It carries through any later transformation: heights taken to AHD from an assumed ellipsoidal datum are still
marked assumed. Rasters, volumes and accuracy statements derived from the job inherit it.

A vertical datum read from the file, or reached by a recorded transformation from a declared one, is not
assumed.

### Why red

`report.html` puts a red banner at the top of the report, and red text in its CRS table, in two cases:

- **Vertical datum assumed**: "Vertical datum ASSUMED, not read from the data", followed by the recorded
  reason. Every height in the report depends on something the data didn't say. If the assumption is wrong,
  every elevation, contour and accuracy figure is wrong by the datum difference (tens of metres
  between ellipsoidal and AHD in WA), and volumes taken against a different-datum base are too.
- **No vertical datum**: heights are of unknown datum, so no surfaces, volumes, contours or accuracy were
  produced.

Red marks a fact the reader must check before relying on the deliverable. The viewer shows the same red
banner for an assumed datum.

## Transformations are chosen, never guessed

When the source and target CRS differ, ingest asks `projinfo` for every non-ballpark candidate operation
over the site's extent and takes the first whose grids are all installed, or the one `--operation` names.
PDAL then runs that exact PROJ pipeline. The job records the chosen operation's name, stated accuracy,
grids and PROJ string, and every candidate with any missing grids (`job.json` → `ingest.transform`; the
report lists them). An operation with a stated accuracy of 1 m or worse, such as WGS 84 → GDA2020 through
the ensemble, adds a "not survey grade" warning.

GDA94 → GDA2020 has several valid operations. PROJ ranks the 7-parameter Helmert first; data derived
from WA state control often warrant the ICSM conformal + distortion grid:
`--operation "GDA94 to GDA2020 (2)"`.

At run time `PROJ_NETWORK` is off for every tool call, so no grid is fetched mid-job. The grids in the image
are listed and SHA256-checked by `argus-survey doctor`.
