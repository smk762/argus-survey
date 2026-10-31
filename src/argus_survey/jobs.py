"""Job directories: ``<jobs root>/<job_id>/job.json`` plus its products.

Job ids are validated against a strict filename charset so an id can never be a
path (no traversal surface); the HTTP API addresses every product by
``(job_id, kind)`` and never resolves a client-supplied path.

A :class:`Job` is the handle a step works through: :meth:`Job.sink` receives
every :class:`~argus_survey.models.CommandRecord` from the runner and persists
it immediately (so a crashed step still leaves its command log), products are
registered with their SHA256, and :meth:`Job.step` times a step into the sidecar.
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import structlog

from argus_survey import __version__
from argus_survey.models import (
    SIDECAR_NAME,
    CommandRecord,
    JobDigest,
    JobSidecar,
    Product,
    SurveyError,
    utcnow,
)
from argus_survey.tools import sha256_file, versions

log = structlog.get_logger(__name__)

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def is_safe_id(value: str) -> bool:
    return bool(value) and value not in {".", ".."} and _SAFE_ID.match(value) is not None


def require_safe_id(value: str, label: str = "job id") -> str:
    if not is_safe_id(value):
        raise SurveyError(f"invalid {label} {value!r}: use letters, digits, '.', '_' or '-' only")
    return value


def save_sidecar(job_dir: Path, sidecar: JobSidecar) -> Path:
    job_dir.mkdir(parents=True, exist_ok=True)
    path = job_dir / SIDECAR_NAME
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(sidecar.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def load_sidecar(job_dir: Path) -> JobSidecar:
    """Read ``job.json``; :class:`FileNotFoundError` if absent, :class:`SurveyError` if invalid."""
    path = job_dir / SIDECAR_NAME
    if not path.is_file():
        raise FileNotFoundError(f"no {SIDECAR_NAME} in {job_dir}")
    try:
        return JobSidecar.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SurveyError(f"invalid {path}: {exc}") from exc


def digest(sidecar: JobSidecar) -> JobDigest:
    return JobDigest(
        job_id=sidecar.job_id,
        created_at=sidecar.created_at,
        updated_at=sidecar.updated_at,
        crs=sidecar.crs.name if sidecar.crs else None,
        points=sidecar.ingest.output_count if sidecar.ingest else None,
        steps_done=sidecar.steps_done,
        products=sorted(sidecar.products),
        warnings=len(sidecar.warnings),
    )


class Job:
    """One job directory and its sidecar."""

    def __init__(self, job_dir: Path, sidecar: JobSidecar) -> None:
        self.dir = job_dir
        self.sidecar = sidecar
        self._lock = threading.RLock()  # tiles run in parallel and all record through sink()
        # (event, data) callbacks — the server streams them as SSE frames; never part of the sidecar
        self.listeners: list[Callable[[str, dict[str, Any]], None]] = []

    def emit(self, event: str, data: dict[str, Any]) -> None:
        for listener in list(self.listeners):
            try:
                listener(event, data)
            except Exception:  # a broken listener must never break a survey step
                log.warning("job.listener_failed", job=self.id, event=event, exc_info=True)

    @property
    def id(self) -> str:
        return self.sidecar.job_id

    # ── persistence ──
    def save(self) -> None:
        with self._lock:
            self.sidecar.updated_at = utcnow()
            save_sidecar(self.dir, self.sidecar)

    def sink(self, record: CommandRecord) -> None:
        """The runner's command sink: append in run order and persist straight away (thread-safe)."""
        with self._lock:
            self.sidecar.commands.append(record)
            self.save()
            index = len(self.sidecar.commands)
        self.emit(
            "command",
            {
                "index": index,
                "step": record.step,
                "tool": record.tool,
                "seconds": record.seconds,
                "returncode": record.returncode,
            },
        )

    def path(self, name: str) -> Path:
        return self.dir / name

    # ── products ──
    def add_product(self, kind: str, name: str, *, step: str, description: str = "", **meta: Any) -> Product:
        path = self.path(name)
        if not path.is_file():
            raise SurveyError(f"step {step} did not produce {name}")
        product = Product(
            kind=kind,
            path=name,
            sha256=sha256_file(path),
            size=path.stat().st_size,
            step=step,
            description=description,
            meta=meta,
        )
        self.sidecar.products[kind] = product
        self.save()
        return product

    def product_path(self, kind: str, *, needed_by: str | None = None) -> Path:
        product = self.sidecar.products.get(kind)
        if product is None:
            hint = f" ({needed_by} needs it)" if needed_by else ""
            raise SurveyError(f"job {self.id!r} has no {kind!r} product yet{hint}")
        path = self.path(product.path)
        if not path.is_file():
            raise SurveyError(f"job {self.id!r}: product {kind!r} is recorded but {product.path} is missing")
        return path

    # ── diagnostics ──
    def warn(self, message: str) -> None:
        if message not in self.sidecar.warnings:
            self.sidecar.warnings.append(message)
            log.info("job.warning", job=self.id, message=message)
            self.save()
            self.emit("warning", {"message": message})

    @contextmanager
    def step(self, name: str) -> Iterator[None]:
        """Time a step into ``timings`` and mark it done on success."""
        t0 = time.perf_counter()
        log.info("step.start", job=self.id, step=name)
        self.emit("step", {"step": name, "state": "start"})
        yield
        self.sidecar.timings[name] = round(time.perf_counter() - t0, 3)
        if name not in self.sidecar.steps_done:
            self.sidecar.steps_done.append(name)
        self.sidecar.tools = versions()
        self.save()
        log.info("step.done", job=self.id, step=name, seconds=self.sidecar.timings[name])
        self.emit("step", {"step": name, "state": "done", "seconds": self.sidecar.timings[name]})

    def require_crs(self, step: str, *, height: bool) -> None:
        """Refuse *step* without a CRS — and, for height-dependent steps, without a vertical datum (rule 1)."""
        crs = self.sidecar.crs
        if crs is None:
            raise SurveyError(f"{step}: job {self.id!r} has no CRS record — run `argus-survey ingest` first")
        if height and not crs.has_height_datum:
            raise SurveyError(
                f"{step} depends on heights, but job {self.id!r} has no vertical datum "
                f"({crs.name}). Re-ingest with --assume-vertical EPSG:<code> (e.g. EPSG:5711 for AHD) "
                "if you know the heights' datum; the assumption is recorded and flagged in the report"
            )


class JobStore:
    """All jobs under one root; ids only, never paths."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def dir(self, job_id: str) -> Path:
        return self.root / require_safe_id(job_id)

    def exists(self, job_id: str) -> bool:
        return (self.dir(job_id) / SIDECAR_NAME).is_file()

    def open(self, job_id: str, *, create: bool = False) -> Job:
        job_dir = self.dir(job_id)
        if (job_dir / SIDECAR_NAME).is_file():
            return Job(job_dir, load_sidecar(job_dir))
        if not create:
            raise SurveyError(f"no job {job_id!r} under {self.root}")
        job = Job(job_dir, JobSidecar(job_id=job_id, package_version=__version__, tools=versions()))
        job.save()
        return job

    def list(self) -> list[JobDigest]:
        """Digests of every readable job, newest first. Unreadable dirs are skipped."""
        if not self.root.is_dir():
            return []
        rows: list[JobDigest] = []
        for child in self.root.iterdir():
            if not child.is_dir() or not is_safe_id(child.name):
                continue
            try:
                rows.append(digest(load_sidecar(child)))
            except (FileNotFoundError, SurveyError, OSError):
                continue
        rows.sort(key=lambda d: d.created_at, reverse=True)
        return rows


def open_job(job_id: str | None, out: Path | None, *, jobs_root: Path, create: bool = False) -> Job:
    """Resolve the CLI's ``--job <id>`` / ``--out <dir>`` pair to a job (``--out``: the dir name is the id)."""
    if (job_id is None) == (out is None):
        raise SurveyError("give exactly one of --job <id> or --out <dir>")
    if out is not None:
        out = out.expanduser().resolve()
        return JobStore(out.parent).open(require_safe_id(out.name, "--out directory name"), create=create)
    assert job_id is not None
    return JobStore(jobs_root).open(job_id, create=create)
