"""FastAPI server for argus-survey (peer of atlas on :8105; this is :8107).

Routes::

    GET  /health                         {status, service, version, read_only, tools}
    GET  /doctor                         the toolchain report
    GET  /inputs                         point clouds, vectors, rasters and CSVs under SURVEY_DATA_DIR (names only)
    GET  /jobs                           {jobs: [JobDigest]}
    GET  /jobs/{id}                      the sidecar (job.json)
    GET  /jobs/{id}/web/{kind}           map view: raster → {png url, lon/lat corners}; vector → WGS 84 GeoJSON
    GET  /jobs/{id}/web/{kind}/image     the map view PNG
    GET  /jobs/{id}/{kind}               a product by kind (copc, dtm, report, …), with HTTP Range (COPC streams)
    POST /jobs/{id}/{step}               run a step; Server-Sent Events: step / command / warning / done | error
    GET  /                               the viewer

Read-only by default: every POST is refused (403) by the shared
:class:`argus_cortex.server.WriteGuard` unless ``SURVEY_ALLOW_PROCESS`` is truthy, and
``/health.read_only == not allow_process``. Clients name inputs relative to
``SURVEY_DATA_DIR`` (resolved by :func:`~argus_survey.config.resolve_under_root`, so
``../`` and symlinks out are refused) and products by ``(job_id, kind)``; no route takes a
filesystem path. A drawn profile line or volume polygon arrives as GeoJSON, not as a file.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
except ImportError as exc:  # pragma: no cover
    raise ImportError("Server requires: pip install argus-survey[server]") from exc

from argus_cortex.server import WriteGuard
from pydantic import BaseModel

from argus_survey import __version__, config
from argus_survey.jobs import Job, JobStore, is_safe_id
from argus_survey.models import (
    AccuracyBody,
    ChangeBody,
    ContoursBody,
    CutfillBody,
    DoneEvent,
    EmptyBody,
    ErrorEvent,
    GroundBody,
    HealthInfo,
    IngestBody,
    ProfileBody,
    QaBody,
    RasterView,
    RunBody,
    StartEvent,
    SurfacesBody,
    SurveyError,
    VolumeBody,
)
from argus_survey.tools import ToolError, versions

PROCESS_REFUSAL = "processing is disabled on this host (set SURVEY_ALLOW_PROCESS=1 to enable POST /jobs/{id}/{step})"
STATIC = Path(__file__).parent / "static"
INPUT_KINDS = {
    "points": (".las", ".laz"),
    "vectors": (".geojson", ".json", ".gpkg", ".shp", ".fgb"),
    "rasters": (".tif", ".tiff", ".vrt"),
    "tables": (".csv", ".txt"),
}
MEDIA = {
    ".laz": "application/vnd.laszip",
    ".las": "application/octet-stream",
    ".tif": "image/tiff",
    ".geojson": "application/geo+json",
    ".gpkg": "application/geopackage+sqlite3",
    ".json": "application/json",
    ".html": "text/html; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}


def _refuse(allow_process: bool):  # noqa: ANN202 - argus_cortex Refuse
    def refuse(scope, headers) -> str | None:  # noqa: ANN001 - starlette Scope / Headers
        return None if allow_process else PROCESS_REFUSAL

    return refuse


def create_app(
    *,
    cors: bool = False,
    cors_origins: list[str] | None = None,
    data_dir: str | Path | None = None,
    jobs_dir: str | Path | None = None,
    allow_process: bool | None = None,
) -> FastAPI:
    """Build the app. Omitted arguments fall back to the ``SURVEY_*`` environment."""
    allow = config.process_allowed(allow_process)
    data_root = config.data_dir(data_dir)
    store = JobStore(config.jobs_dir(jobs_dir))
    busy: set[str] = set()
    busy_lock = threading.Lock()

    app = FastAPI(
        title="Argus Survey",
        description="Point-cloud deliverables for land surveyors, with every datum and command recorded.",
        version=__version__,
    )
    if not allow:
        app.add_middleware(WriteGuard, refuse=_refuse(allow))
    if cors:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.cors_origins(cors_origins),
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["Content-Range", "Accept-Ranges", "Content-Length"],
        )

    # ── helpers ──
    def job_or_404(job_id: str) -> Job:
        if not is_safe_id(job_id):
            raise HTTPException(400, f"invalid job id {job_id!r}")
        try:
            return store.open(job_id)
        except SurveyError as exc:
            raise HTTPException(404, str(exc)) from exc

    def data_path(name: str, label: str) -> Path:
        try:
            path = config.resolve_under_root(data_root, name, label)
        except SurveyError as exc:
            raise HTTPException(400, str(exc)) from exc
        if not path.exists():
            raise HTTPException(404, f"{label} {name!r} not found under the data root")
        return path

    def opt_path(name: str | None, label: str) -> Path | None:
        return data_path(name, label) if name else None

    # ── reads ──
    @app.get("/health")
    def health() -> dict[str, Any]:
        return HealthInfo(version=__version__, read_only=not allow, tools=versions()).model_dump(mode="json")

    @app.get("/doctor")
    def doctor() -> dict[str, Any]:
        from argus_survey.tools import doctor as run_doctor

        report = run_doctor(verify_grid_digests=False)
        return {"ok": report.ok, **report.model_dump()}

    @app.get("/inputs")
    def inputs() -> dict[str, list[str]]:
        out: dict[str, list[str]] = {k: [] for k in INPUT_KINDS}
        if data_root.is_dir():
            root = data_root.resolve()
            for p in sorted(root.rglob("*")):
                if not p.is_file() or any(part.startswith(".") for part in p.relative_to(root).parts):
                    continue
                for kind, suffixes in INPUT_KINDS.items():
                    if p.suffix.lower() in suffixes:
                        out[kind].append(str(p.relative_to(root)))
        return out

    @app.get("/jobs")
    def jobs() -> dict[str, Any]:
        return {"jobs": [d.model_dump(mode="json") for d in store.list()]}

    @app.get("/jobs/{job_id}")
    def job(job_id: str) -> dict[str, Any]:
        return job_or_404(job_id).sidecar.model_dump(mode="json")

    @app.get("/jobs/{job_id}/web/{kind}", response_model=None)
    def web(job_id: str, kind: str) -> dict[str, Any] | FileResponse:
        from argus_survey import overlays

        j = job_or_404(job_id)
        try:
            if kind in overlays.VECTOR_KINDS:
                return FileResponse(overlays.vector_wgs84(j, kind), media_type="application/geo+json")
            meta = overlays.raster_overlay(j, kind)
        except SurveyError as exc:
            raise HTTPException(404, str(exc)) from exc
        return RasterView.model_validate({**meta, "url": f"/jobs/{job_id}/web/{kind}/image"}).model_dump(mode="json")

    @app.get("/jobs/{job_id}/web/{kind}/image")
    def web_image(job_id: str, kind: str) -> FileResponse:
        from argus_survey import overlays

        j = job_or_404(job_id)
        try:
            meta = overlays.raster_overlay(j, kind)
        except SurveyError as exc:
            raise HTTPException(404, str(exc)) from exc
        return FileResponse(j.path(".web") / meta["png"], media_type="image/png")

    @app.get("/jobs/{job_id}/{kind}")
    def product(job_id: str, kind: str) -> FileResponse:
        """A product by kind. Starlette's FileResponse honours ``Range`` (206), so COPC readers stream it."""
        j = job_or_404(job_id)
        p = j.sidecar.products.get(kind)
        if p is None:
            raise HTTPException(404, f"job {job_id!r} has no {kind!r} (has: {', '.join(sorted(j.sidecar.products))})")
        path = j.path(p.path)
        if not path.is_file():
            raise HTTPException(410, f"{p.path} is recorded but missing on disk")
        media = MEDIA.get(path.suffix.lower(), "application/octet-stream")
        disposition = (
            "inline" if path.suffix.lower() in {".html", ".svg", ".json", ".geojson", ".csv"} else "attachment"
        )
        return FileResponse(path, media_type=media, filename=path.name, content_disposition_type=disposition)

    # ── processing (SSE) ──
    def steps() -> dict[str, tuple[type[BaseModel], Callable[[Job, Any], Any], bool]]:
        """step → (body model, runner, creates the job)."""
        from argus_survey.accuracy import accuracy
        from argus_survey.change import change
        from argus_survey.contours import contours
        from argus_survey.ground import ground
        from argus_survey.ingest import ingest
        from argus_survey.pipeline import run_all
        from argus_survey.profile import profile
        from argus_survey.qa import qa
        from argus_survey.report import report
        from argus_survey.surfaces import surfaces
        from argus_survey.volume import cutfill, volume

        def base_arg(b: VolumeBody) -> str:
            kind, _, name = b.base.partition(":")
            return f"surface:{data_path(name, 'base surface')}" if kind == "surface" else b.base

        def zone(j: Job, b: VolumeBody | CutfillBody, *, required: bool) -> tuple[Path | None, str | None]:
            """The polygon file and its CRS: a named file under the data root, or a drawn WGS 84 geometry."""
            if b.polygon is not None and b.polygon_geojson is not None:
                raise SurveyError("give polygon (a file) or polygon_geojson (a drawn geometry), not both")
            if b.polygon_geojson is None:
                if required and b.polygon is None:
                    raise SurveyError("give polygon (a file under the data root) or polygon_geojson (drawn, WGS 84)")
                return opt_path(b.polygon, "polygon"), b.polygon_srs
            geom = b.polygon_geojson
            if geom.get("type") not in {"Polygon", "MultiPolygon"} or not geom.get("coordinates"):
                raise SurveyError("polygon_geojson must be a GeoJSON Polygon or MultiPolygon geometry")
            path = j.path(".drawn-polygon.geojson")
            feature = {"type": "Feature", "properties": {"name": b.name or "drawn polygon"}, "geometry": geom}
            path.write_text(json.dumps({"type": "FeatureCollection", "features": [feature]}))
            return path, "EPSG:4326"

        def volume_run(j: Job, b: VolumeBody) -> Any:
            poly, srs = zone(j, b, required=True)
            return volume(
                j,
                poly,  # type: ignore[arg-type]
                base=base_arg(b),
                surface=b.surface,
                polygon_srs=srs,
                allow_voids=b.allow_voids,
                resample=b.resample,
                base_vertical=b.base_vertical,
            )

        def cutfill_run(j: Job, b: CutfillBody) -> Any:
            poly, srs = zone(j, b, required=False)
            return cutfill(
                j,
                data_path(b.design, "design"),
                polygon=poly,
                polygon_srs=srs,
                surface=b.surface,
                design_vertical=b.design_vertical,
                resample=b.resample,
                allow_voids=b.allow_voids,
            )

        def profile_run(j: Job, b: ProfileBody) -> Any:
            if (b.line is None) == (b.line_file is None):
                raise SurveyError("give exactly one of line (GeoJSON geometry, WGS 84) or line_file")
            if b.line is not None:
                if b.line.get("type") != "LineString" or len(b.line.get("coordinates") or []) < 2:
                    raise SurveyError("line must be a GeoJSON LineString with at least two positions")
                path = j.path(".drawn-line.geojson")
                path.write_text(
                    json.dumps(
                        {
                            "type": "FeatureCollection",
                            "features": [{"type": "Feature", "properties": {"name": "drawn line"}, "geometry": b.line}],
                        }
                    )
                )
                return profile(j, path, step=b.step, line_srs="EPSG:4326")
            return profile(j, data_path(b.line_file, "line_file"), step=b.step, line_srs=b.line_srs)  # type: ignore[arg-type]

        return {
            "ingest": (
                IngestBody,
                lambda j, b: ingest(
                    j,
                    [data_path(n, "input") for n in b.inputs],
                    t_srs=b.t_srs,
                    s_srs=b.s_srs,
                    assume_vertical=b.assume_vertical,
                    operation=b.operation,
                    crop=opt_path(b.crop, "crop"),
                    crop_srs=b.crop_srs,
                    force=b.force,
                ),
                True,
            ),
            "qa": (QaBody, lambda j, b: qa(j, density_res=b.density_res, min_density=b.min_density), False),
            "ground": (
                GroundBody,
                lambda j, b: ground(j, method=b.method, params=b.params, keep_classes=b.keep_classes),
                False,
            ),
            "surfaces": (
                SurfacesBody,
                lambda j, b: surfaces(
                    j, res=b.res, dtm=b.dtm, dsm=b.dsm, chm=b.chm, fill_voids=b.fill_voids, dsm_method=b.dsm_method
                ),
                False,
            ),
            "contours": (ContoursBody, lambda j, b: contours(j, interval=b.interval, index=b.index), False),
            "volume": (VolumeBody, volume_run, False),
            "cutfill": (CutfillBody, cutfill_run, False),
            "change": (
                ChangeBody,
                lambda j, b: change(
                    j,
                    job_or_404(b.against),
                    surface=b.surface,
                    lod=b.lod,
                    lod_from_accuracy=b.lod_from_accuracy,
                    polygon=opt_path(b.polygon, "polygon"),
                    polygon_srs=b.polygon_srs,
                ),
                False,
            ),
            "accuracy": (
                AccuracyBody,
                lambda j, b: accuracy(
                    j,
                    data_path(b.checks, "checks"),
                    checks_crs=b.crs,
                    cols=tuple(b.cols),
                    checkpoint_rmse=b.checkpoint_rmse,
                    surface=b.surface,
                    target_class_cm=b.class_cm,
                ),
                False,
            ),
            "profile": (ProfileBody, profile_run, False),
            "report": (EmptyBody, lambda j, _b: report(j), False),
            "run": (
                RunBody,
                lambda j, b: run_all(
                    j,
                    [data_path(n, "input") for n in b.inputs],
                    res=b.res,
                    interval=b.interval,
                    t_srs=b.t_srs,
                    s_srs=b.s_srs,
                    assume_vertical=b.assume_vertical,
                    operation=b.operation,
                    crop=opt_path(b.crop, "crop"),
                    crop_srs=b.crop_srs,
                    method=b.method,
                    fill_voids=b.fill_voids,
                    echo=lambda _m: None,
                ),
                True,
            ),
        }

    @app.post("/jobs/{job_id}/{step}")
    async def process(job_id: str, step: str, body: dict[str, Any] | None = None) -> StreamingResponse:
        table = steps()
        if step not in table:
            raise HTTPException(404, f"unknown step {step!r} (one of {', '.join(table)})")
        model, runner, creates = table[step]
        try:
            parsed = model.model_validate(body or {})
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if not is_safe_id(job_id):
            raise HTTPException(400, f"invalid job id {job_id!r}")
        try:
            j = store.open(job_id, create=creates)
        except SurveyError as exc:
            raise HTTPException(404, str(exc)) from exc
        with busy_lock:
            if job_id in busy:
                raise HTTPException(409, f"job {job_id!r} is already processing")
            busy.add(job_id)

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()

        def put(event: str, data: dict[str, Any]) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, (event, data))

        def work() -> None:
            j.listeners.append(put)
            try:
                put("start", StartEvent(job=job_id, step=step).model_dump())
                result = runner(j, parsed)
                payload = result.model_dump(mode="json") if isinstance(result, BaseModel) else {"result": str(result)}
                done = DoneEvent(
                    job=job_id,
                    step=step,
                    result=payload,
                    products=sorted(j.sidecar.products),
                    warnings=j.sidecar.warnings,
                )
                put("done", done.model_dump(mode="json"))
            except ToolError as exc:
                put("error", ErrorEvent(message=str(exc), stderr=exc.stderr_tail[-2000:]).model_dump())
            except (SurveyError, HTTPException) as exc:
                msg = exc.detail if isinstance(exc, HTTPException) else str(exc)
                put("error", ErrorEvent(message=msg).model_dump(exclude_none=True))
            except Exception as exc:  # noqa: BLE001 - surfaced to the client, logged by structlog upstream
                msg = f"internal error: {exc.__class__.__name__}: {exc}"
                put("error", ErrorEvent(message=msg).model_dump(exclude_none=True))
            finally:
                j.listeners.remove(put)
                with busy_lock:
                    busy.discard(job_id)
                loop.call_soon_threadsafe(queue.put_nowait, None)

        threading.Thread(target=work, name=f"survey-{job_id}-{step}", daemon=True).start()

        async def stream() -> AsyncIterator[str]:
            while (item := await queue.get()) is not None:
                event, data = item
                yield f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        )

    # ── viewer ──
    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/viewer")

    @app.get("/viewer", include_in_schema=False)
    def viewer() -> HTMLResponse:
        page = STATIC / "viewer.html"
        if not page.is_file():
            raise HTTPException(404, "viewer not packaged")
        return HTMLResponse(page.read_text(encoding="utf-8"))

    return app
