"""Pydantic v2 wire types.

:class:`JobSidecar` (``job.json``) is the API contract and the provenance record
of a job: every input pinned by SHA256, the CRS record, every PDAL pipeline and
GDAL/PROJ argv in run order with the tool versions, every product with its
digest, and the reports (QA, volumes, accuracy, …) the steps wrote.
``SURVEY_VERSION`` is the sidecar's schema version; bump it on any breaking
change to these models.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SURVEY_VERSION = 1
SIDECAR_NAME = "job.json"


class SurveyError(Exception):
    """A refusal or failure the user can act on (bad input, missing datum, …).

    The CLI prints the message and exits non-zero; the API maps it to 4xx.
    """


def utcnow() -> datetime:
    return datetime.now(UTC)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ── Tools ──────────────────────────────────────────────────────────────────────


class ToolVersions(_Model):
    """Versions of the external stack a job ran against (``doctor`` and every sidecar)."""

    pdal: str | None = None
    gdal: str | None = None
    proj: str | None = None


class CommandRecord(_Model):
    """One external command, as run. ``argv`` is exact; ``pipeline`` is the PDAL JSON fed on stdin."""

    step: str
    tool: str
    argv: list[str]
    pipeline: list[dict[str, Any]] | None = None
    started_at: datetime
    seconds: float
    returncode: int


class DoctorCheck(_Model):
    name: str
    ok: bool
    detail: str = ""
    required: bool = True


class DoctorReport(_Model):
    versions: ToolVersions
    checks: list[DoctorCheck] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks if c.required)


# ── Files ──────────────────────────────────────────────────────────────────────


class FileRef(_Model):
    """A file pinned by digest. ``path`` is relative to the job dir for products, absolute for inputs."""

    path: str
    sha256: str
    size: int


Units = Literal["metre", "US survey foot", "foot"]


# ── CRS ────────────────────────────────────────────────────────────────────────


class OperationCandidate(_Model):
    """One candidate PROJ offered for a transformation, and why it was or wasn't used."""

    name: str
    accuracy_m: float | None
    grids: list[str] = Field(default_factory=list)
    missing_grids: list[str] = Field(default_factory=list)


class TransformOp(_Model):
    """The exact coordinate operation a job used (fed verbatim to PDAL's filters.projpipeline)."""

    source: str
    target: str
    name: str
    accuracy_m: float | None
    proj: str
    grids: list[str] = Field(default_factory=list)
    chosen_by: Literal["proj-default", "user"] = "proj-default"
    candidates: list[OperationCandidate] = Field(default_factory=list)


HeightReference = Literal["gravity", "ellipsoidal", "none"]


class CrsRef(_Model):
    """One CRS (or one component of a compound CRS), as identified."""

    name: str
    epsg: int | None = None
    kind: str  # PROJJSON type: ProjectedCRS, GeographicCRS, VerticalCRS, …
    units: str = "metre"
    unit_factor: float = 1.0  # metres per unit
    north_first: bool = False  # authority axis order is (north, east) / (lat, lon)


class CrsRecord(_Model):
    """Everything a surveyor needs to know about the coordinates of a dataset (CLAUDE.md rule 1)."""

    definition: str  # what PDAL/PROJ are given: "EPSG:7850+5711", or WKT2 when there is no code
    name: str
    horizontal: CrsRef
    vertical: CrsRef | None = None
    height_reference: HeightReference = "none"
    vertical_assumed: str | None = None  # why the vertical datum was assumed, if it was
    geoid: str | None = None  # the geoid grid applied to reach this CRS, if any
    epoch: float | None = None  # coordinate epoch (dynamic datums only)
    source: Literal["file", "user", "transformed"] = "file"

    @property
    def has_height_datum(self) -> bool:
        return self.height_reference != "none"


# ── Point clouds ───────────────────────────────────────────────────────────────


class Bounds3D(_Model):
    minx: float
    miny: float
    minz: float
    maxx: float
    maxy: float
    maxz: float

    @property
    def area(self) -> float:
        return max(self.maxx - self.minx, 0.0) * max(self.maxy - self.miny, 0.0)


SourceType = Literal["lidar", "photogrammetry", "unknown"]


class CloudSummary(_Model):
    """Header-level facts about a point cloud (``pdal info --summary``)."""

    count: int
    bounds: Bounds3D
    dimensions: list[str] = Field(default_factory=list)
    las_version: str | None = None
    point_format: int | None = None
    copc: bool = False
    scale: tuple[float, float, float] | None = None


class InputRecord(FileRef):
    """An input file, pinned. ``path`` is absolute."""

    role: str = "points"
    summary: CloudSummary | None = None


class DimStats(_Model):
    name: str
    count: int
    minimum: float
    maximum: float
    average: float
    stddev: float
    counts: dict[str, int] = Field(default_factory=dict)  # value -> count, for enumerated dims


class Product(FileRef):
    """A job output. ``path`` is relative to the job dir."""

    kind: str
    step: str
    description: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)


class IngestRecord(_Model):
    inputs_count: int
    output_count: int
    cropped: bool = False
    crop: str | None = None  # description of the crop applied
    scale: float
    source_crs: CrsRecord
    transform: TransformOp | None = None
    source_type: SourceType = "unknown"
    stats: list[DimStats] = Field(default_factory=list)


class JobSidecar(_Model):
    """``job.json`` — the API contract and the provenance record of a job."""

    schema_version: int = SURVEY_VERSION
    job_id: str
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    package_version: str
    tools: ToolVersions = Field(default_factory=ToolVersions)
    crs: CrsRecord | None = None
    inputs: list[InputRecord] = Field(default_factory=list)
    ingest: IngestRecord | None = None
    qa: QaReport | None = None
    ground: GroundReport | None = None
    surfaces: SurfacesReport | None = None
    contours: ContoursReport | None = None
    volumes: list[VolumeReport] = Field(default_factory=list)
    change: ChangeReport | None = None
    accuracy: AccuracyReport | None = None
    profiles: list[ProfileReport] = Field(default_factory=list)
    commands: list[CommandRecord] = Field(default_factory=list)
    products: dict[str, Product] = Field(default_factory=dict)
    timings: dict[str, float] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    steps_done: list[str] = Field(default_factory=list)


class JobDigest(_Model):
    """One row of ``GET /jobs``."""

    job_id: str
    created_at: datetime
    updated_at: datetime
    crs: str | None
    points: int | None
    steps_done: list[str]
    products: list[str]
    warnings: int


# ── Grids and rasters ──────────────────────────────────────────────────────────


class GridSpec(_Model):
    """A raster grid snapped to multiples of its resolution, so two epochs at the same res share cells.

    ``origin_x``/``origin_y`` are the lower-left corner (PDAL writers.gdal's convention); the
    GeoTIFF's top-left is ``(origin_x, origin_y + height * res)``.
    """

    origin_x: float
    origin_y: float
    res: float
    width: int
    height: int

    @property
    def extent(self) -> tuple[float, float, float, float]:
        return (
            self.origin_x,
            self.origin_y,
            self.origin_x + self.width * self.res,
            self.origin_y + self.height * self.res,
        )

    @property
    def cell_area(self) -> float:
        return self.res * self.res


class RasterStats(_Model):
    valid_cells: int
    total_cells: int
    minimum: float | None = None
    maximum: float | None = None
    mean: float | None = None
    stddev: float | None = None

    @property
    def valid_fraction(self) -> float:
        return self.valid_cells / self.total_cells if self.total_cells else 0.0


# ── QA ─────────────────────────────────────────────────────────────────────────


class ClassCount(_Model):
    code: int
    name: str
    count: int
    percent: float


class ValueCount(_Model):
    value: int
    count: int
    percent: float


class DensityStats(_Model):
    res: float
    mean: float
    p5: float
    p50: float
    p95: float
    covered_cells: int


class GapStats(_Model):
    threshold: float  # pts/m²
    threshold_rule: str
    cells: int
    area_m2: float
    percent_of_coverage: float


class OutlierStats(_Model):
    method: str
    params: dict[str, Any]
    count: int
    percent: float


class QaReport(_Model):
    points: int
    bounds: Bounds3D
    coverage_area_m2: float
    coverage_hex_edge: float
    source_type: SourceType
    density: DensityStats
    classes: list[ClassCount]
    returns: list[ValueCount]
    outliers: OutlierStats
    gaps: GapStats
    tiles: int = 1


# ── Ground ─────────────────────────────────────────────────────────────────────


class GroundReport(_Model):
    method: Literal["smrf", "csf"]
    preset: str | None = None  # named parameter set (e.g. "stockpile"), already merged into params
    params: dict[str, Any]
    outlier_params: dict[str, Any]
    points: int
    ground_points: int
    ground_percent: float
    noise_points: int
    ground_density: float  # ground pts per m² of coverage
    ground_spacing: float  # 1/sqrt(density), metres
    reset_classes: bool
    normalised_returns: bool
    tiles: int
    tile_size: float
    tile_buffer: float


# ── Surfaces ───────────────────────────────────────────────────────────────────


class SurfaceRecord(_Model):
    kind: Literal["dtm", "dsm", "chm"]
    method: str  # e.g. "idw r=0.707 m over class 2"
    output_type: str
    radius: float | None
    window_size: int
    points_used: str  # the selection, in words
    void_percent: float  # of coverage, before any fill
    void_percent_after_fill: float | None = None
    filled_max_distance: int | None = None
    stats: RasterStats


class SurfacesReport(_Model):
    grid: GridSpec
    surfaces: dict[str, SurfaceRecord] = Field(default_factory=dict)
    resolution_warning: str | None = None


class ContoursReport(_Model):
    interval: float
    index_every: int | None
    source: str
    features: int
    min_elev: float | None
    max_elev: float | None


# ── Volumes, change, accuracy, profiles ────────────────────────────────────────


class ZoneVolume(_Model):
    """Volumes between an upper and a lower surface over one polygon (all positive numbers)."""

    name: str
    area_m2: float  # polygon area as covered by the grid (fractional cells)
    above_m3: float  # upper above lower: stockpile volume / fill needed / gain
    below_m3: float  # upper below lower: hollow under base / cut needed / loss
    net_m3: float  # above - below
    area_above_m2: float
    area_below_m2: float
    void_percent: float  # of the polygon's area with no value in either surface
    base: str  # description of the lower surface
    base_fit_rms_m: float | None = None  # plane bases: RMS of boundary heights about the fitted plane
    boundary_samples: int | None = None


class VolumeReport(_Model):
    kind: Literal["stockpile", "cutfill", "change"]
    surface: str  # what "upper"/"lower" are, in words
    sign_convention: str
    res: float
    surface_void_percent: dict[str, float]  # per input surface, of the whole surface's coverage
    method: str
    zones: list[ZoneVolume]
    resampled: str | None = None  # if an input was resampled onto the grid, how
    allow_voids: bool = False


class ChangeReport(_Model):
    against_job: str
    against_sidecar_sha256: str
    this_sidecar_sha256: str
    surface: str
    lod_m: float
    lod_rule: str
    gain_m3: float
    loss_m3: float
    net_m3: float
    gain_area_m2: float
    loss_area_m2: float
    no_change_area_m2: float
    compared_area_m2: float
    void_percent: float
    res: float
    area: str = "whole overlap of the two epochs"


class CheckResidual(_Model):
    id: str
    x: float
    y: float
    z_check: float
    z_surface: float | None
    residual: float | None  # surface - check (ASPRS: data minus checkpoint)
    group: Literal["NVA", "VVA"]
    description: str = ""
    excluded: str | None = None  # why it was not used


class AccuracyGroup(_Model):
    group: Literal["NVA", "VVA"]
    n: int
    excluded: int
    mean_m: float | None
    std_m: float | None  # sample standard deviation (n-1)
    rmse_data_m: float | None  # RMSE of the residuals alone
    rmse_m: float | None  # product RMSE incl. check point survey accuracy, ASPRS ed. 2
    max_abs_m: float | None
    meets_minimum_count: bool
    pass_fail_applies: bool  # NVA: yes; VVA: reported as found (ed. 2)


class AccuracyReport(_Model):
    standard: str
    surface: str
    sampling: str
    checks_file: FileRef
    checks_crs: str
    transform: TransformOp | None
    checkpoint_rmse_m: float | None
    groups: list[AccuracyGroup]
    residuals: list[CheckResidual]
    target_class_cm: float | None = None
    statement: str
    notes: list[str] = Field(default_factory=list)


class ProfileReport(_Model):
    line: str
    length_m: float
    step_m: float
    samples: int
    surfaces: list[str]


# ── API request bodies (inputs are names under SURVEY_DATA_DIR) ────────────────


class IngestBody(_Model):
    inputs: list[str] = Field(min_length=1)
    t_srs: str | None = None
    s_srs: str | None = None
    assume_vertical: str | None = None
    operation: str | None = None
    crop: str | None = None
    crop_srs: str | None = None
    force: bool = False


class QaBody(_Model):
    density_res: float = Field(default=1.0, gt=0)
    min_density: float | None = Field(default=None, ge=0)


class GroundBody(_Model):
    method: str = "smrf"
    preset: str | None = None  # e.g. "stockpile" (SMRF slope 1.0); params override it
    params: dict[str, float | int | bool] = Field(default_factory=dict)
    keep_classes: bool = False


class SurfacesBody(_Model):
    res: float = Field(default=0.5, gt=0)
    dtm: bool = True
    dsm: bool = True
    chm: bool = True
    fill_voids: int | None = Field(default=None, ge=1)
    dsm_method: str = "idw"
    chm_method: str = "max"  # max (binned) or radius (highest point within res·√2; fewer voids, wider crowns)


class ContoursBody(_Model):
    interval: float = Field(default=0.5, gt=0)
    index: int | None = 5


class VolumeBody(_Model):
    polygon: str | None = None  # a polygon file under the data root
    polygon_geojson: dict[str, Any] | None = None  # or a GeoJSON (Multi)Polygon geometry in WGS 84, drawn on the map
    name: str | None = None  # zone name for a drawn polygon
    base: str = "plane"  # plane | lowest-perimeter | surface:<name under the data root>
    surface: str = "dtm"
    polygon_srs: str | None = None
    allow_voids: bool = False
    resample: bool = False
    base_vertical: str | None = None


class CutfillBody(_Model):
    design: str
    polygon: str | None = None
    polygon_geojson: dict[str, Any] | None = None  # a drawn GeoJSON (Multi)Polygon in WGS 84, instead of polygon
    name: str | None = None
    polygon_srs: str | None = None
    surface: str = "dtm"
    design_vertical: str | None = None
    resample: bool = False
    allow_voids: bool = False


class ChangeBody(_Model):
    against: str
    lod: float | None = Field(default=None, ge=0)
    lod_from_accuracy: bool = False
    surface: str = "dtm"
    polygon: str | None = None
    polygon_srs: str | None = None


class AccuracyBody(_Model):
    checks: str
    crs: str
    cols: list[str] = Field(default_factory=lambda: ["id", "x", "y", "z"], min_length=4, max_length=4)
    checkpoint_rmse: float | None = Field(default=None, ge=0)
    class_cm: float | None = Field(default=None, gt=0)
    surface: str = "dtm"


class ProfileBody(_Model):
    line: dict[str, Any] | None = None  # a GeoJSON LineString geometry in WGS 84 (lon, lat), e.g. drawn on the map
    line_file: str | None = None  # or a line file under the data root
    line_srs: str | None = None
    step: float = Field(default=0.5, gt=0)


class RunBody(IngestBody):
    res: float = Field(default=0.5, gt=0)
    interval: float = Field(default=0.5, gt=0)
    method: str = "smrf"
    preset: str | None = None
    fill_voids: int | None = Field(default=None, ge=1)


class EmptyBody(_Model):
    pass


# ── API responses and Server-Sent Events ──────────────────────────────────────


class HealthInfo(_Model):
    """``GET /health``. ``read_only`` is true unless ``SURVEY_ALLOW_PROCESS`` is set: clients disable writes on it."""

    status: Literal["ok"] = "ok"
    service: Literal["argus-survey"] = "argus-survey"
    version: str
    read_only: bool
    tools: ToolVersions


class RasterView(_Model):
    """``GET /jobs/{id}/web/{kind}`` for a raster: a Web Mercator PNG placed by its lon/lat corners (TL, TR, BR, BL).

    ``ramp`` names the fixed colour ramp (``terrain``: blue → red over ``range``; ``diverging``: blue (−) → transparent (0)
    → red (+) for a DoD; ``hillshade``: grey, no ``range``). A derived view, never a deliverable.
    """

    kind: str
    png: str
    url: str
    coordinates: list[list[float]]
    range: list[float] | None = None
    ramp: Literal["terrain", "diverging", "hillshade"] | None = None


class StartEvent(_Model):
    """SSE ``start``: the step was accepted and is running."""

    job: str
    step: str


class StepEvent(_Model):
    """SSE ``step``: a (sub-)step started or finished (``run`` reports each of its steps)."""

    step: str
    state: Literal["start", "done"]
    seconds: float | None = None


class CommandEvent(_Model):
    """SSE ``command``: one tool invocation finished; ``index`` is its 1-based place in ``job.json`` ``commands``."""

    index: int
    step: str
    tool: str
    seconds: float
    returncode: int


class WarningEvent(_Model):
    """SSE ``warning``: also appended to ``job.json`` ``warnings``."""

    message: str


class DoneEvent(_Model):
    """SSE ``done``: the step's own report in ``result``; the job's product kinds and warnings after it."""

    job: str
    step: str
    result: dict[str, Any]
    products: list[str]
    warnings: list[str]


class ErrorEvent(_Model):
    """SSE ``error``: a refusal or tool failure; ``stderr`` is the tool's stderr tail when a command failed."""

    message: str
    stderr: str | None = None


WIRE_MODELS: tuple[type[BaseModel], ...] = (
    JobSidecar,
    JobDigest,
    DoctorReport,
    HealthInfo,
    RasterView,
    IngestBody,
    QaBody,
    GroundBody,
    SurfacesBody,
    ContoursBody,
    VolumeBody,
    CutfillBody,
    ChangeBody,
    AccuracyBody,
    ProfileBody,
    RunBody,
    EmptyBody,
    StartEvent,
    StepEvent,
    CommandEvent,
    WarningEvent,
    DoneEvent,
    ErrorEvent,
)
WIRE_TITLE = "argus-survey wire contract"


def wire_schema_text() -> str:
    """The committed ``schema/survey-wire.schema.json``: every wire model, sorted and newline-terminated."""
    from argus_cortex.wire import render_schema

    return render_schema(WIRE_MODELS, title=WIRE_TITLE)


JobSidecar.model_rebuild()
