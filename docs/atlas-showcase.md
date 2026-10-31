# Survey → fantasy map (argus-atlas showcase)

argus-survey's DTM is a plain GeoTIFF, so [argus-atlas](https://github.com/smk762/argus-atlas) can bake a
georeferenced fantasy-map overlay straight from a survey. Neither package imports the other.

```bash
# 1. a surveyed DTM
argus-survey run site.laz --job site1 --res 1.0 --interval 1

# 2. its extent in lon/lat for atlas's --bbox (west,south,east,north)
gdalinfo -json data/jobs/site1/dtm.tif | python3 -c \
  'import json,sys; c=json.load(sys.stdin)["wgs84Extent"]["coordinates"][0]; xs=[p[0] for p in c]; ys=[p[1] for p in c]; print(f"{min(xs)},{min(ys)},{max(xs)},{max(ys)}")'

# 3. atlas controls from the survey DTM
argus-atlas controls --dem data/jobs/site1/dtm.tif --bbox <w,s,e,n> --out jobs/site1-map
```

Then follow atlas's bake runbook (`../argus-atlas/docs/first-bake.md`) for the bake and tiles.

## What carries over, and what doesn't

- **CRS**: atlas plans its own Web Mercator canvas and warps the DEM onto it, so the survey CRS (MGA2020,
  PCG2020, …) doesn't matter; atlas records its own grid and every GDAL command, as survey does.
- **Resolution**: atlas's canvas is at most 1536 px on its long side, and its tile pyramid stops at zoom 14
  (about 9.5 m per pixel at the equator, ~8 m at Perth's latitude). A sub-metre DTM of a small site
  therefore **loses its detail**: a 500 m site is a canvas of 1024 px at ~0.5 m/px for the bake, but only
  ~60 px across in the zoom-14 tiles. Survey sites read well in atlas from a few kilometres across; for a
  single pad, use the bake's PNG rather than the tiles, or coarsen the DTM (`--res 2`) before baking.
- **Voids**: atlas warps nodata as nodata; fill small DTM voids first (`surfaces --fill-voids 10`) if a clean
  canvas matters more than showing where the survey had no ground.
