# Methods

How the accuracy statement and the volumes are computed, in more detail than
[How the numbers are made](../README.md#how-the-numbers-are-made).

## Vertical accuracy (NVA and VVA)

`argus-survey accuracy` tests a surface (DTM by default, `--surface dsm`) against surveyed check points under
the **ASPRS Positional Accuracy Standards for Digital Geospatial Data, Edition 2, Version 2 (2024)**. The
standard and edition are named in every statement.

**Check points.** A CSV with id, easting, northing and height columns (`--cols`, default `id,x,y,z`) and an
optional `description`. The CSV is pinned by SHA256. Its CRS is stated with `--crs` and the points are
transformed into the job CRS through an explicit operation, chosen as at ingest and recorded (see
[Datums](datums.md#transformations-are-chosen-never-guessed)). The surface is sampled bilinearly at each point.
The residual is **surface − check** (the standard's data minus checkpoint).

**NVA and VVA.** A point is in the **VVA** (vegetated vertical accuracy) group only when its `description`
starts with `veg` or `vva` (case-insensitive). Every other point, including one with an empty or missing
description, is **NVA** (non-vegetated). NVA is tested pass/fail against the target class. VVA is reported
as found, with no pass/fail, as Edition 2 requires.

**Statistics per group**: n; points excluded, with the reason (outside the surface or in a void); mean, sample
standard deviation, min, max, median (§7.16); the RMSE of the residuals (`rmse_data_m`); and the product RMSE.

- **RMSE only.** Edition 1's 95 % confidence figure is gone in Edition 2 and is not reported.
- **Product RMSE** = √(RMSE_data² + RMSE_checkpoints²) (§7.12.4). `--checkpoint-rmse` is the surveyor's 1σ
  vertical RMSE for the check points themselves. Without it the product RMSE equals the data RMSE, and the notes
  say so. Check points should be at least twice as accurate as the class tested (§7.13); a note says when they
  aren't.
- **Accuracy class.** Free-valued in Edition 2: a "#-cm" class means NVA RMSE_V ≤ # cm. Give it with
  `--class-cm`. Without it, the statement reports the RMSE found and makes no pass/fail claim.
- **Minimum count.** At least 30 check points per group (§7.14). Below that, the group is flagged
  (`meets_minimum_count: false`) and the statement uses §7.16.1's reduced-checkpoint wording.
- **Blunders.** Residuals beyond 3 × the target RMSE (§7.2), or 3 × the data RMSE without a class, are listed
  by id to investigate. They stay in the statistics.
- **Bias.** An NVA mean beyond 25 % of the target RMSE (or of the RMSE found, without a class) is flagged as possible systematic bias (datum, geoid or
  control). A datum slip shows up here first, as a large mean with a small spread.

**Outputs**: `accuracy.json` (the statement, groups, notes and every residual) and `accuracy_residuals.csv`.
`change --lod-from-accuracy` reads both epochs' NVA RMSE from these statements.

## Volume bases

`argus-survey volume --polygon P --base …` measures a stockpile as the volume between a top surface
(`--surface dtm|dsm`) and a base, over each polygon. Every report states the base, the cell size, each surface's
void %, the method and the sign convention: *above* is the stockpile, *below* is any hollow beneath the base.
Cells on the polygon edge count by the fraction inside it, and more than 5 % voids under a polygon is refused
unless `--allow-voids` (voids count as zero).

Use **`--surface dsm`** for stockpiles. Ground filtering correctly removes a pile from the DTM, which then
reads low (about 10 % on the demo kit's cone).

| Base | What it is | Use it when |
|---|---|---|
| `plane` (default) | A least-squares plane through the top surface's heights, sampled at cell spacing along the polygon boundary. Its RMS about the plane (`base_fit_rms_m`) and the number of samples are reported. | The pile sits on a planar pad or an even slope, and the polygon is drawn on clean ground just outside the toe. An RMS of centimetres means the plane fits; decimetres mean the toe isn't planar, so use another base. |
| `lowest-perimeter` | A horizontal plane at the lowest boundary height. | A conservative upper bound, or a pile in a hollow. On sloping ground it also counts the wedge of terrain above the lowest point, so it reads high there. |
| `surface:<raster>` | Another raster: a surveyed pre-stockpile surface, a pad design or an earlier DTM. | The true base is known. It must be on the job's grid and in the job CRS, or it is refused. `--resample` warps a misaligned one bilinearly (recorded), and `--base-vertical` states the datum of one that declares none. |

The plane fit and the lowest boundary height use boundary samples that land on data. More than 10 % of
them falling in voids adds a warning; fewer than 3 is refused.

Volumes are in grid units. When ingest could compute the combined scale factor, each zone also gives
ground-scaled area and volumes (grid ÷ combined²). `cutfill` uses the same engine with a design surface as the
base (*fill* = design above the existing surface), and `change` uses it with an earlier epoch.
