#!/usr/bin/env bash
# Fetch a ~200 m x 200 m public-domain LiDAR sample from USGS 3DEP (Entwine Point Tiles on AWS Open Data)
# into ./data/in/usgs-sample.laz, using PDAL inside the argus-survey image (nothing to install on the host).
#
# Dataset: USGS_LPC_CA_SoCal_Wildfires_B1_2018 (public domain, USGS). The EPT is in EPSG:3857 (Web Mercator)
# with heights in metres and no vertical CRS declared, so ingest needs --assume-vertical (recorded, flagged).
set -euo pipefail
cd "$(dirname "$0")/.."
DATASET="${DATASET:-USGS_LPC_CA_SoCal_Wildfires_B1_2018_LAS_2019}"
BOUNDS="${BOUNDS:-([-13061500, -13061300], [4013900, 4014100])}"
OUT="${OUT:-usgs-sample.laz}"
mkdir -p data/in
docker compose build -q survey
docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD/data/in:/out" "${SURVEY_IMAGE:-argus-survey:local}" \
  pdal translate "https://s3-us-west-2.amazonaws.com/usgs-lidar-public/${DATASET}/ept.json" "/out/${OUT}" \
  --readers.ept.bounds="${BOUNDS}"
echo "wrote data/in/${OUT}"
