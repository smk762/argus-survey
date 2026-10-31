# TODO

Follow-ups beyond v0.1.0, most-requested first.

- **CAD export: LandXML surfaces + DXF contours/breaklines.** Surveyors will ask for this first.
- **Grid-to-ground scale**: report the combined scale factor (point scale × height factor) at the site
  centroid, and offer ground-scaled areas/volumes alongside grid ones.
- Breaklines (kerbs, toes, crests) honoured in the DTM.
- TIN-based volumes (prism method) alongside the grid method, for comparison with 12d / Civil 3D.
- Vertical transformation of rasters (design surfaces in another height datum), today refused.
- M3C2 change detection (point-to-point with local uncertainty) beyond the DEM of difference.
- Classification beyond ground (buildings, vegetation heights, power lines).
- CHM on a radius rather than strict bins at fine resolutions (sparse canopy leaves binned voids).
- E57 ingest (terrestrial scanners): PDAL's E57 plugin is already in the image.
- Report as PDF directly (today: print the HTML).
- Photogrammetry ingest (images → points).
