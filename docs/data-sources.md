# Public point-cloud data

Where to get LiDAR to try argus-survey on, and on what terms. Check each dataset's own metadata for its
licence and its datums before relying on it — and record what you find with `--s-srs`/`--assume-vertical`
when the files don't say.

| Source | Coverage | Access | Licence / attribution |
|---|---|---|---|
| [USGS 3DEP](https://registry.opendata.aws/usgs-lidar/) (Entwine Point Tiles on AWS) | USA | scriptable: PDAL `readers.ept` over HTTPS ([dataset browser](https://usgs.entwine.io/)) | US public domain; credit "U.S. Geological Survey, 3D Elevation Program" |
| [ELVIS — Elevation Information System](https://elevation.fsdf.org.au/) (Geoscience Australia / ICSM) | Australia | select an area, order, download link by email | per dataset; many are CC BY 4.0, some state data carry their own licence |
| [Landgate](https://www.landgate.wa.gov.au/) / [data.wa.gov.au LiDAR](https://catalogue.data.wa.gov.au/dataset/lidar) | Western Australia | through Landgate | mostly licensed (Landgate personal-use or commercial licence); a few DataWA sets are open |
| [AHN](https://www.ahn.nl/) | Netherlands | per tile ([PDOK feed](https://service.pdok.nl/rws/ahn/atom/index.xml)) | open (CC0) |
| [OpenTopography](https://www.opentopography.org/) | worldwide collection | portal (free account for some) | per dataset |

## USGS 3DEP — the walkthrough sample

`make sample` runs `scripts/fetch-usgs-sample.sh`, which reads a 200 × 200 (Web Mercator) metre window of
`USGS_LPC_CA_SoCal_Wildfires_B1_2018_LAS_2019` with PDAL in the image. Change `DATASET`/`BOUNDS` to fetch
elsewhere; bounds are in EPSG:3857.

The EPT service reprojects every dataset to **EPSG:3857** and declares **no vertical CRS**; heights are the
source project's orthometric heights (NAVD88 for most 3DEP projects — confirm in the project's metadata).
That is why the walkthrough ingests with `--assume-vertical EPSG:5703` and a survey projection as `--t-srs`.

## Western Australia

There is no openly scriptable bulk WA point cloud: order through ELVIS (national programmes and some state
captures) or license from Landgate. ELVIS deliveries are usually LAZ tiles in MGA2020 (or MGA94 for older
captures) with AHD heights; check the header with `argus-survey info` — older tiles often declare only the
horizontal CRS, and then the heights' datum must be stated (`--assume-vertical EPSG:5711`) from the
delivery's metadata.

> NOAA's public bucket lists "WA DNR" COPC files — that is **Washington State's** Department of Natural
> Resources, not Western Australia.

## Synthetic data

`argus-survey synth site.laz --checks checks.csv` writes a scene whose answers are known: a 60 × 80 m tilted
plane with 2 cm noise, a cone stockpile of 268.08 m³, a flat-roofed building, trees, injected outliers, and
check points (`--epoch 2` grows the stockpile by 67.02 m³). It is what the test suite runs on.
