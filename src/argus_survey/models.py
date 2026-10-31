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
