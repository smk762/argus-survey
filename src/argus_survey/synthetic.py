"""Synthetic survey scenes with analytically known answers.

Used by the test suite (no binary fixtures, ever) and by ``argus-survey synth``
so a reader can try every step without downloading data. A scene is a tilted
ground plane with Gaussian noise, a stockpile cone of known radius and height
(volume πr²h/3 above the plane, whatever the tilt), a flat-roofed building and a
few trees whose canopy hides ground from the "camera" but not from the "laser".

Points are written as CSV and turned into LAS by PDAL (``readers.text`` →
``writers.las``) with the CRS the caller asks for. Truth labels go in
``UserData``: 0 ground, 1 stockpile, 2 building, 3 vegetation, 4 noise (isolated
low multipath points and high "birds"). Every point is
written ``Classification = 1`` (unclassified): classifying is the toolkit's job.

Pure-Python on purpose (``random`` with a fixed seed, no numpy): the geometry
is simple and the outputs must be reproducible byte for byte.
"""

from __future__ import annotations

import csv
import json
import math
import random
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path

from argus_survey.tools import CommandSink, pdal_pipeline

TRUTH_GROUND, TRUTH_STOCKPILE, TRUTH_BUILDING, TRUTH_VEGETATION, TRUTH_NOISE = 0, 1, 2, 3, 4


@dataclass(frozen=True)
class Tree:
    x: float  # offsets from the scene origin, metres
    y: float
    crown_radius: float
    height: float


@dataclass(frozen=True)
class Scene:
    """Geometry in metres. ``x``/``y`` offsets are relative to ``origin`` (E, N)."""

    origin: tuple[float, float] = (391000.0, 6464000.0)  # MGA2020 zone 50, near Perth
    width: float = 80.0
    height: float = 60.0
    spacing: float = 0.3
    noise: float = 0.02  # σ of the vertical noise added to every point
    base_z: float = 20.0
    slope_x: float = 0.03
    slope_y: float = -0.015
    cone_centre: tuple[float, float] = (20.0, 30.0)
    cone_radius: float = 8.0
    cone_height: float = 4.0
    building: tuple[float, float, float, float] = (50.0, 36.0, 60.0, 44.0)  # x0, y0, x1, y1
    building_height: float = 6.0
    trees: tuple[Tree, ...] = field(
        default_factory=lambda: (
            Tree(45.0, 12.0, 2.5, 8.0),
            Tree(52.0, 15.0, 2.0, 7.0),
            Tree(65.0, 10.0, 3.0, 9.0),
            Tree(70.0, 25.0, 2.0, 6.0),
            Tree(8.0, 8.0, 2.5, 7.5),
            Tree(10.0, 52.0, 2.0, 6.5),
        )
    )
    noise_points: int = 30  # isolated outliers: half 10-40 m below ground, half 20-60 m above
    photogrammetry: bool = False  # True: no returns info, RGB, no ground under canopy
    seed: int = 762

    # ── analytic truths ──
    def ground_z(self, x: float, y: float) -> float:
        """Height of the bare plane at offset (x, y)."""
        return self.base_z + self.slope_x * x + self.slope_y * y

    @property
    def cone_volume(self) -> float:
        return math.pi * self.cone_radius**2 * self.cone_height / 3.0

    def cone_z(self, x: float, y: float) -> float | None:
        d = math.hypot(x - self.cone_centre[0], y - self.cone_centre[1])
        if d >= self.cone_radius:
            return None
        return self.ground_z(x, y) + self.cone_height * (1.0 - d / self.cone_radius)

    def in_building(self, x: float, y: float) -> bool:
        x0, y0, x1, y1 = self.building
        return x0 <= x <= x1 and y0 <= y <= y1

    def tree_over(self, x: float, y: float) -> tuple[Tree, float] | None:
        """The tree whose crown covers (x, y), and the crown height above ground there."""
        for t in self.trees:
            d = math.hypot(x - t.x, y - t.y)
            if d < t.crown_radius:
                # a dome: full height at the trunk, 2 m lower at the crown edge
                return t, t.height - 2.0 * (d / t.crown_radius) ** 2
        return None

    def surface_z(self, x: float, y: float) -> float:
        """Bare-earth height including the stockpile (what a perfect DTM shows)."""
        z = self.cone_z(x, y)
        return z if z is not None else self.ground_z(x, y)

    def roof_z(self) -> float:
        x0, y0, x1, y1 = self.building
        return self.ground_z((x0 + x1) / 2, (y0 + y1) / 2) + self.building_height


def epoch2(scene: Scene, *, extra_height: float = 1.0, seed: int | None = None) -> Scene:
    """The same site later: the stockpile grew by *extra_height* m at its apex; fresh noise."""
    return replace(scene, cone_height=scene.cone_height + extra_height, seed=scene.seed + 1 if seed is None else seed)


COLUMNS = ["X", "Y", "Z", "Intensity", "ReturnNumber", "NumberOfReturns", "Classification", "UserData", "GpsTime"]
RGB = ["Red", "Green", "Blue"]


def points(scene: Scene) -> list[list[float | int]]:
    """Every point of *scene* as a row matching ``COLUMNS`` (+ ``RGB`` for photogrammetry)."""
    rnd = random.Random(scene.seed)
    e0, n0 = scene.origin
    rows: list[list[float | int]] = []
    s = scene.spacing
    nx, ny = int(scene.width / s), int(scene.height / s)

    def add(
        x: float, y: float, z: float, intensity: int, rn: int, nr: int, truth: int, rgb: tuple[int, int, int]
    ) -> None:
        row: list[float | int] = [
            round(e0 + x, 3),
            round(n0 + y, 3),
            round(z + rnd.gauss(0.0, scene.noise), 3),
            intensity,
            0 if scene.photogrammetry else rn,
            0 if scene.photogrammetry else nr,
            1,
            truth,
            round(len(rows) * 1e-5, 5),
        ]
        if scene.photogrammetry:
            row += [c * 256 for c in rgb]
        rows.append(row)

    for i in range(nx):
        for j in range(ny):
            x = (i + 0.5) * s + rnd.uniform(-s / 3, s / 3)
            y = (j + 0.5) * s + rnd.uniform(-s / 3, s / 3)
            if scene.in_building(x, y):
                add(x, y, scene.roof_z(), 520, 1, 1, TRUTH_BUILDING, (150, 60, 50))
                continue
            canopy = scene.tree_over(x, y)
            cone = scene.cone_z(x, y)
            if canopy is not None:
                _, crown = canopy
                add(x, y, scene.ground_z(x, y) + crown, 120, 1, 2, TRUTH_VEGETATION, (40, 110, 40))
                if scene.photogrammetry:
                    continue  # the camera never sees the ground under a crown
                add(x, y, scene.surface_z(x, y), 280, 2, 2, TRUTH_GROUND, (0, 0, 0))
            elif cone is not None:
                add(x, y, cone, 340, 1, 1, TRUTH_STOCKPILE, (170, 150, 110))
            else:
                add(x, y, scene.ground_z(x, y), 300, 1, 1, TRUTH_GROUND, (120, 110, 90))

    # building walls: vertical columns of points along the perimeter
    x0, y0, x1, y1 = scene.building
    perimeter = [(x, y0) for x in _steps(x0, x1, s)] + [(x, y1) for x in _steps(x0, x1, s)]
    perimeter += [(x0, y) for y in _steps(y0, y1, s)] + [(x1, y) for y in _steps(y0, y1, s)]
    for x, y in perimeter:
        zg = scene.ground_z(x, y)
        for z in _steps(zg + 0.3, scene.roof_z(), 0.5):
            add(x, y, z, 480, 1, 1, TRUTH_BUILDING, (140, 140, 140))
    # isolated outliers, well clear of every surface
    for k in range(scene.noise_points):
        x, y = rnd.uniform(2, scene.width - 2), rnd.uniform(2, scene.height - 2)
        dz = -rnd.uniform(10, 40) if k % 2 == 0 else rnd.uniform(20, 60)
        add(x, y, scene.surface_z(x, y) + dz, 50, 1, 1, TRUTH_NOISE, (255, 255, 255))
    return rows


def _steps(a: float, b: float, step: float) -> list[float]:
    n = max(int((b - a) / step), 1)
    return [a + k * (b - a) / n for k in range(n + 1)]


def write_scene(
    scene: Scene,
    path: Path,
    *,
    srs: str | None = "EPSG:7850+5711",
    sink: CommandSink | None = None,
) -> Path:
    """Write *scene* to a LAS/LAZ file at *path* with CRS *srs* (``None``: no CRS in the file).

    A ``.e57`` path writes E57 (double precision; E57 has no CRS, so *srs* is ignored, and no return numbering).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = COLUMNS + (RGB if scene.photogrammetry else [])
    with tempfile.TemporaryDirectory(prefix="argus-survey-synth-") as tmp:
        csv_path = Path(tmp) / "points.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(columns)
            writer.writerows(points(scene))
        writer_stage: dict[str, object] = {
            "type": "writers.las",
            "filename": str(path),
            "minor_version": 4,
            "dataformat_id": 7 if scene.photogrammetry else 6,
            "scale_x": 0.001,
            "scale_y": 0.001,
            "scale_z": 0.001,
            "offset_x": scene.origin[0],
            "offset_y": scene.origin[1],
            "offset_z": 0,
            "compression": path.suffix.lower() == ".laz",
        }
        if srs is not None:
            writer_stage["a_srs"] = srs
        if path.suffix.lower() == ".e57":
            # single precision (the writer's default) rounds an MGA northing to ~0.5 m
            writer_stage = {"type": "writers.e57", "filename": str(path), "double_precision": True}
        pdal_pipeline([{"type": "readers.text", "filename": str(csv_path)}, writer_stage], step="synth", sink=sink)
    return path


@dataclass(frozen=True)
class CheckPoint:
    id: str
    x: float
    y: float
    z: float
    description: str  # "open" (NVA) or "vegetated" (VVA)


def check_points(
    scene: Scene, *, n_open: int = 30, n_vegetated: int = 10, seed: int = 99, z_noise: float = 0.0
) -> list[CheckPoint]:
    """Ground check points on the true bare-earth surface, away from the stockpile, building and edges.

    "open" points lie in clear ground (non-vegetated accuracy); "vegetated" ones under tree crowns.
    *z_noise*: σ of Gaussian error added to the check heights (a survey's own error, for testing RMSE).
    """
    rnd = random.Random(seed)
    zn = random.Random(seed + 1)
    e0, n0 = scene.origin
    out: list[CheckPoint] = []
    tries = 0
    while len([c for c in out if c.description == "open"]) < n_open and tries < 100_000:
        tries += 1
        x, y = rnd.uniform(3, scene.width - 3), rnd.uniform(3, scene.height - 3)
        if scene.in_building(x, y) or scene.tree_over(x, y) or _near_cone(scene, x, y) or _near_building(scene, x, y):
            continue
        out.append(
            CheckPoint(
                f"CP{len(out) + 1:03d}",
                round(e0 + x, 3),
                round(n0 + y, 3),
                round(scene.ground_z(x, y) + zn.gauss(0, z_noise), 3),
                "open",
            )
        )
    for k in range(n_vegetated):
        t = scene.trees[k % len(scene.trees)]
        ang = rnd.uniform(0, 2 * math.pi)
        r = rnd.uniform(0, t.crown_radius * 0.6)
        x, y = t.x + r * math.cos(ang), t.y + r * math.sin(ang)
        out.append(
            CheckPoint(
                f"CP{len(out) + 1:03d}",
                round(e0 + x, 3),
                round(n0 + y, 3),
                round(scene.surface_z(x, y) + zn.gauss(0, z_noise), 3),
                "vegetated",
            )
        )
    return out


def _near_cone(scene: Scene, x: float, y: float, margin: float = 2.0) -> bool:
    return math.hypot(x - scene.cone_centre[0], y - scene.cone_centre[1]) < scene.cone_radius + margin


def _near_building(scene: Scene, x: float, y: float, margin: float = 2.0) -> bool:
    x0, y0, x1, y1 = scene.building
    return x0 - margin <= x <= x1 + margin and y0 - margin <= y <= y1 + margin


def write_check_points(points_: list[CheckPoint], path: Path) -> Path:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["id", "x", "y", "z", "description"])
        for c in points_:
            writer.writerow([c.id, f"{c.x:.3f}", f"{c.y:.3f}", f"{c.z:.3f}", c.description])
    return path


# ── demo kit: every input the workflow takes, with known answers ─────────────


def _ring_projected(scene: Scene, pts: list[tuple[float, float]]) -> list[list[float]]:
    e0, n0 = scene.origin
    ring = [[round(e0 + x, 3), round(n0 + y, 3)] for x, y in pts]
    return ring + [ring[0]]


def _polygon_file(scene: Scene, path: Path, name: str, pts: list[tuple[float, float]], horizontal: str) -> Path:
    """A GeoJSON polygon in the scene's projected CRS, declared with the (pre-RFC 7946) ``crs`` member GDAL reads."""
    code = horizontal.split(":", 1)[1]
    doc = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": f"urn:ogc:def:crs:EPSG::{code}"}},
        "features": [
            {
                "type": "Feature",
                "properties": {"name": name},
                "geometry": {"type": "Polygon", "coordinates": [_ring_projected(scene, pts)]},
            }
        ],
    }
    path.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    return path


def write_kit(out: Path, *, srs: str = "EPSG:7850+5711", sink: CommandSink | None = None) -> dict[str, object]:
    """Write a complete demo input set into *out* and return its known answers (also written to ``kit-truth.json``).

    ``site-e1.laz`` / ``site-e2.laz`` (the stockpile grew 1 m at its apex), ``checks.csv`` (30 open + 10 vegetated),
    ``stockpile.geojson`` (a ring 1.5 m outside the cone's toe), ``pad.geojson`` + ``design.tif`` (a flat pad
    benched 0.25 m below the ground at its centre, so cut − fill is exactly 0.25 m × the pad's area).
    """
    from argus_survey.tools import run

    if not srs.upper().startswith("EPSG:"):
        raise ValueError("the demo kit needs an EPSG code (e.g. EPSG:7850+5711)")
    horizontal = srs.split("+", 1)[0]
    out.mkdir(parents=True, exist_ok=True)
    scene = Scene()
    later = epoch2(scene)
    write_scene(scene, out / "site-e1.laz", srs=srs, sink=sink)
    write_scene(later, out / "site-e2.laz", srs=srs, sink=sink)
    write_check_points(check_points(scene), out / "checks.csv")

    cx, cy = scene.cone_centre
    r = scene.cone_radius + 1.5
    ring = [(cx + r * math.cos(2 * math.pi * k / 48), cy + r * math.sin(2 * math.pi * k / 48)) for k in range(48)]
    _polygon_file(scene, out / "stockpile.geojson", "stockpile", ring, horizontal)

    x0, y0, x1, y1 = 28.0, 6.0, 46.0, 26.0  # clear of the cone's toe and the building
    _polygon_file(scene, out / "pad.geojson", "pad", [(x0, y0), (x1, y0), (x1, y1), (x0, y1)], horizontal)
    level = round(scene.ground_z((x0 + x1) / 2, (y0 + y1) / 2) - 0.25, 3)
    res, margin = 0.5, 2.0
    cols, rows = int((x1 - x0 + 2 * margin) / res), int((y1 - y0 + 2 * margin) / res)
    e0, n0 = scene.origin
    with tempfile.TemporaryDirectory(prefix="argus-survey-kit-") as tmp:
        asc = Path(tmp) / "design.asc"
        header = (
            f"ncols {cols}\nnrows {rows}\nxllcorner {e0 + x0 - margin}\nyllcorner {n0 + y0 - margin}\n"
            f"cellsize {res}\nNODATA_value -9999\n"
        )
        line = " ".join([f"{level}"] * cols)
        asc.write_text(header + "\n".join([line] * rows) + "\n", encoding="utf-8")
        tif = out / "design.tif"
        run(["gdal_translate", "-q", "-of", "GTiff", "-ot", "Float32", str(asc), str(tif)], step="synth", sink=sink)
    run(["gdal", "raster", "edit", "--crs", srs, str(tif)], step="synth", sink=sink)

    pad_area = (x1 - x0) * (y1 - y0)
    truth: dict[str, object] = {
        "srs": srs,
        "stockpile_volume_m3": round(scene.cone_volume, 3),
        "stockpile_volume_epoch2_m3": round(later.cone_volume, 3),
        "change_gain_m3": round(later.cone_volume - scene.cone_volume, 3),
        "pad_area_m2": pad_area,
        "pad_design_level_m": level,
        "design_res_m": res,
        "pad_net_cut_m3": round(0.25 * pad_area, 3),
        "notes": [
            "stockpile: a cone on a tilted plane; measure it on the DSM (ground filtering removes a pile from the DTM) "
            "with a plane base",
            "change: on the DSM, the gain before any level of detection; LoD trims the cone's shallow flanks",
            "pad: cut minus fill (design below ground) is exactly 0.25 m times the pad area",
            f"design.tif is on a {res} m grid: build the surfaces at --res {res}, or cut/fill refuses the misaligned "
            "design unless --resample is given",
            "checks: on the true ground; NVA RMSE ≈ the 0.02 m point noise, VVA reads the DTM under canopy",
        ],
    }
    (out / "kit-truth.json").write_text(json.dumps(truth, indent=2) + "\n", encoding="utf-8")
    return truth
