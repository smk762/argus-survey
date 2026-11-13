# Public point-cloud data

Where to get LiDAR to try argus-survey on, and on what terms. Check each dataset's own metadata for its
licence and its datums before relying on it — and record what you find with `--s-srs`/`--assume-vertical`
when the files don't say.

| Source | Coverage | Access | Licence / attribution |
|---|---|---|---|
| [USGS 3DEP](https://registry.opendata.aws/usgs-lidar/) (Entwine Point Tiles on AWS) | USA | scriptable: PDAL `readers.ept` over HTTPS ([dataset browser](https://usgs.entwine.io/)) | US public domain; credit "U.S. Geological Survey, 3D Elevation Program" |
| [ELVIS — Elevation Information System](https://elevation.fsdf.org.au/) (Geoscience Australia / ICSM) | Australia | select an area, order, download link by email | per dataset; many are CC BY 4.0, some state data carry their own licence |
| [Landgate](https://www.landgate.wa.gov.au/) / [data.wa.gov.au LiDAR](https://catalogue.data.wa.gov.au/dataset/lidar) | Western Australia | order from Landgate (quote) | Landgate personal-use or commercial licence |
| [DataWA](https://catalogue.data.wa.gov.au/) community UAV captures (e.g. Cambridge Coastcare, Floreat) | Perth coast | direct LAS download (CKAN API) | CC BY 4.0 |
| [TERN Dronescape](https://data.tern.org.au/uas/dronescape_lidar/collection.json) | Australia-wide plots, 100+ WA captures | COPC over HTTPS, free API key | CC BY 4.0 |
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

What each WA source actually delivered when argus-survey was validated on it (issue #2, November 2026).
Always check a delivery's header with `argus-survey info`, and state what it doesn't declare.

### ELVIS (Geoscience Australia)

Free, with no request limit, but there's no public API: draw an area in the [portal](https://elevation.fsdf.org.au/)
and a download link arrives by email. **Point-cloud coverage in WA is patchy.** Over Floreat–Wembley (Perth) it
offered only DEMs (1-second and 5 m DEMs, a 1-second DSM, DEA intertidal) and no LAZ. National and other-state
deliveries are LAZ tiles; older tiles often declare only the horizontal CRS, and then the heights' datum must be
stated (`--assume-vertical EPSG:5711`) from the delivery's metadata.

### Landgate Perth Metro LiDAR (2021–22)

Raw classified LAS in 1 km tiles over built-up Perth. It is not downloadable: order it with Landgate's
[request form](https://www.landgate.wa.gov.au/siteassets/documents/location-data-and-services/licensing/location-information-transaction.pdf)
(CustomerExperience@Landgate.wa.gov.au). Raw LiDAR is $154.10 per km² for orders up to 10 km² (2026 price list), under
the Landgate Personal Use Licence. Landgate's forum says the tiles were delivered in **GDA94**, so ingest picks a
GDA94 → GDA2020 operation; ask for the CRS and vertical datum when ordering.

### Landgate geodetic marks: check points

DataWA's [geodetic-marks](https://catalogue.data.wa.gov.au/dataset/geodetic-marks) KMZ is open and covers every WA SSM,
BM and CORS (about 65,000, current and destroyed). Each placemark's description carries the GDA2020 latitude and
longitude as DMS text, the **AHD71 reduced level**, its status (Final or Provisional) and, where known, its accuracy.
About 59,000 have a Final level. Use the DMS text, not the KML point, for the coordinates. Marks sit in kerbs,
footpaths and road reserves, so an aerial capture over suburbs sees enough of them for an ASPRS test (≥ 30): around
Rossmoyne there are 67 current marks with a Final level in 2 km²; along the Floreat dunes, none. The file downloads of the
same layers (LGATE-076/199) need a DataWA login and fall under the Personal Use Licence.

### Cambridge Coastcare UAV captures (DataWA, CC BY 4.0)

Floreat Beach dune captures from 2018 to 2022 (areas f1–f4), downloadable from DataWA as LAS: a "lidar" product and
a Pix4D photogrammetry cloud ("sparse_point_cloud", though tens of millions of points) per capture. Points are
in MGA2020 zone 50 with AHD heights (the beach is at about 0 m), but **only some files declare it**: the f1 2021
lidar has the full compound CRS (`GDA2020 / MGA zone 50 + AHD height - AUSGeoid2020`); the 2022 files and the
photogrammetry declare none (`--s-srs EPSG:7850 --assume-vertical EPSG:5711`). The 2021 and 2022 f1 captures
cover the same dunes: their DTM DoD has a mean of −0.003 m (σ 0.22 m), so the assumed and declared AHD agree.
The **2022 lidar is ground-only** (every point class 2; the 2021 one is unclassified), so its DSM is really a
DTM: compare these epochs' DTMs, not DSMs (QA warns about such deliveries).
No GCP report is published.

### TERN Dronescape (CC BY 4.0)

Drone LiDAR over TERN monitoring plots (about 300 × 300 m each), with over 100 WA captures listed, as COPC with a
[STAC catalogue](https://data.tern.org.au/uas/dronescape_lidar/collection.json). Plots run to 100–700 million points
(WAACOO0001: 102 million, 686 MB). Files need a free [TERN account](https://account.tern.org.au) and API key
(`curl --user "$TERN_API_KEY:"`). The header declares GDA2020 / MGA (zone 50 to 52) but **no vertical datum**,
and TERN doesn't publish one: whether heights are ellipsoidal or AHD has to be established before any height-dependent use.
The points are unclassified, with RGB.

> NOAA's public bucket lists "WA DNR" COPC files — that is **Washington State's** Department of Natural
> Resources, not Western Australia.

## Synthetic data

`argus-survey synth site.laz --checks checks.csv` writes a scene whose answers are known: a 60 × 80 m tilted
plane with 2 cm noise, a cone stockpile of 268.08 m³, a flat-roofed building, trees, injected outliers, and
check points (`--epoch 2` grows the stockpile by 67.02 m³). It is what the test suite runs on.
