"""Environment configuration — the ``SURVEY_*`` knobs and the path roots.

Every caller-supplied path that reaches the HTTP API is resolved under
``SURVEY_DATA_DIR`` (:func:`resolve_under_root`, ported from argus-atlas): a
relative path is joined under the root, an absolute path must already lie
inside it, and anything that escapes — ``..``, a symlink out — is refused.
"""

from __future__ import annotations

import os
from pathlib import Path

from argus_survey.models import SurveyError

ENV_DATA_DIR = "SURVEY_DATA_DIR"
ENV_JOBS_DIR = "SURVEY_JOBS_DIR"
ENV_ALLOW_PROCESS = "SURVEY_ALLOW_PROCESS"
ENV_CORS_ORIGINS = "SURVEY_CORS_ORIGINS"

DEFAULT_DATA_DIR = "./data/in"
DEFAULT_JOBS_DIR = "./data/jobs"
DEFAULT_CORS_ORIGINS = "http://localhost:3000"
DEFAULT_PORT = 8107

# Same spellings argus_cortex.server.env_flag accepts. Duplicated rather than imported
# because cortex's server module needs starlette, which a CLI-only install does not have.
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _env_path(name: str, default: str, override: str | os.PathLike[str] | None = None) -> Path:
    if override is not None:
        return Path(override).expanduser()
    return Path(os.environ.get(name) or default).expanduser()


def data_dir(override: str | os.PathLike[str] | None = None) -> Path:
    """The only root under which the API may name an input (point cloud, polygon, check points, design)."""
    return _env_path(ENV_DATA_DIR, DEFAULT_DATA_DIR, override)


def jobs_dir(override: str | os.PathLike[str] | None = None) -> Path:
    """Where ``<job_id>/job.json`` and every product live."""
    return _env_path(ENV_JOBS_DIR, DEFAULT_JOBS_DIR, override)


def cors_origins(override: list[str] | None = None) -> list[str]:
    if override is not None:
        return override
    raw = os.environ.get(ENV_CORS_ORIGINS) or DEFAULT_CORS_ORIGINS
    return [o.strip() for o in raw.split(",") if o.strip()]


def process_allowed(override: bool | None = None) -> bool:
    """Processing routes are OFF unless ``SURVEY_ALLOW_PROCESS`` is truthy (``1``/``true``/``yes``/``on``).
    A set-but-unrecognised value counts as off. The CLI is not gated."""
    if override is not None:
        return override
    return os.environ.get(ENV_ALLOW_PROCESS, "").strip().lower() in _TRUTHY


def resolve_under_root(root: Path, candidate: str, label: str = "path") -> Path:
    """Resolve *candidate* under *root*, refusing anything that escapes it.

    Relative paths are joined under the root; absolute paths are accepted only
    when they already lie inside it. Symlinks are resolved before the check, so
    a link out of the root is refused too.
    """
    if not candidate or "\x00" in candidate:
        raise SurveyError(f"invalid {label}")
    root_resolved = root.expanduser().resolve()
    given = Path(candidate).expanduser()
    full = (given if given.is_absolute() else root_resolved / given).resolve()
    if full != root_resolved and root_resolved not in full.parents:
        raise SurveyError(f"{label} {candidate!r} escapes the configured root {root}")
    return full
