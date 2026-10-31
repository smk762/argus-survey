# syntax=docker/dockerfile:1
#
# argus-survey: Ubuntu 26.04 + one consistent PDAL/GDAL/PROJ stack.
#
# PDAL is not packaged by Debian or Ubuntu (24.04 and 26.04 both lack it), so the
# geospatial toolchain comes from conda-forge, installed from the explicit
# lockfile env/geo-linux-64.lock (exact URLs + md5) — PDAL links the very
# GDAL/PROJ whose CLIs the package calls. No Python GIS bindings anywhere: the
# package only shells out to `pdal`, `gdal*` and `proj*` (CLAUDE.md, rule 6).
#
# The datum grids surveyors need are fetched once at build time from the PROJ
# CDN and checked against env/proj-grids.sha256; at run time PROJ_NETWORK=OFF,
# so a transform can never silently download (or silently skip) a grid.

# ── Stage 1: the geo toolchain ────────────────────────────────────────────────
FROM ubuntu:26.04 AS geo

ARG MICROMAMBA_VERSION=2.3.3
ENV DEBIAN_FRONTEND=noninteractive MAMBA_ROOT_PREFIX=/opt/mamba

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates bzip2 curl \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL "https://micro.mamba.pm/api/micromamba/linux-64/${MICROMAMBA_VERSION}" \
        | tar -xj -C /usr/local bin/micromamba

COPY env/geo-linux-64.lock /tmp/geo.lock
RUN micromamba create -y -q -p /opt/geo -f /tmp/geo.lock \
    && micromamba clean -y -a -q \
    # Headers, static libs and docs are build-time only; conda-meta stays (doctor reads versions from it).
    && rm -rf /opt/geo/include /opt/geo/share/doc /opt/geo/share/man /opt/geo/man \
    && find /opt/geo/lib -name '*.a' -delete \
    # conda-forge's PDAL ships optional plugins (HDF, icebridge) whose libraries the
    # package doesn't depend on; drop any plugin that can't load so every `pdal` call
    # isn't prefixed by load errors on stderr.
    && for so in /opt/geo/lib/libpdal_plugin_*.so; do \
        if LD_LIBRARY_PATH=/opt/geo/lib ldd "$so" | grep -q 'not found'; then echo "drop $so"; rm "$so"; fi; \
    done

COPY env/proj-grids.sha256 /tmp/proj-grids.sha256
RUN cd /opt/geo/share/proj \
    && cut -d' ' -f3 /tmp/proj-grids.sha256 \
        | xargs -I{} curl -fsSL --retry 3 -o {} "https://cdn.proj.org/{}" \
    && sha256sum -c /tmp/proj-grids.sha256 \
    && cp /tmp/proj-grids.sha256 /opt/geo/share/proj/argus-survey-grids.sha256

# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM ubuntu:26.04

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /uvx /bin/

# hatch-vcs derives the version from git history, so the build context includes
# .git and the image needs git. Ubuntu's ca-certificates lags: distrust emSign
# ECC Root CA - G3 and the Comodo AAA cross-sign root, as the rest of the suite does.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 git ca-certificates \
    && sed -i \
        -e 's|^mozilla/emSign_ECC_Root_CA_-_G3|!mozilla/emSign_ECC_Root_CA_-_G3|' \
        -e 's|^mozilla/Comodo_AAA|!mozilla/Comodo_AAA|' \
        /etc/ca-certificates.conf \
    && update-ca-certificates --fresh >/dev/null \
    && rm -rf /var/lib/apt/lists/*

COPY --from=geo /opt/geo /opt/geo

# The conda env is used without activation, so name its data dirs explicitly.
ENV PROJ_DATA=/opt/geo/share/proj \
    PROJ_NETWORK=OFF \
    GDAL_DATA=/opt/geo/share/gdal \
    PATH="/opt/venv/bin:${PATH}:/opt/geo/bin"

WORKDIR /app
COPY . .

# Extras baked into the image, synced from uv.lock (--locked fails the build if
# the lock is stale). The package lives in its own venv on Ubuntu's Python.
ARG EXTRAS="server,cli"
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
RUN uv sync --locked --no-editable --no-cache --python /usr/bin/python3 \
        $(echo "${EXTRAS}" | tr ',' '\n' | sed 's/^/--extra /') \
    && argus-survey doctor --strict

ENV SURVEY_DATA_DIR=/data/in \
    SURVEY_JOBS_DIR=/data/jobs

EXPOSE 8107
CMD ["argus-survey", "serve", "--host", "0.0.0.0", "--port", "8107"]
