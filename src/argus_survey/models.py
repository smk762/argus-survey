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
