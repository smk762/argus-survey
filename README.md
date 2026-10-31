# argus-survey

Point-cloud toolkit for land surveyors: QA, ground, DTM/DSM, contours, volumes, change and accuracy with recorded datums

Part of the [Argus suite](https://github.com/smk762?tab=repositories&q=argus).

## Install

```bash
uv pip install argus-survey
```

## Develop

```bash
make install   # venv + editable install with the "dev" extras
make test
make lint
```

## CI / Release

- **CI** runs via the shared [`argus-ci`](https://github.com/smk762/argus-ci) reusable workflow.
- **Release** publishes to PyPI (OIDC trusted publishing) and GHCR on `v*` tags.
- Versioning is derived from git tags via `hatch-vcs` — tag `vX.Y.Z` to cut a release.

This repo was scaffolded from [`argus-pkg-template`](https://github.com/smk762/argus-pkg-template).
Run `copier update` to pull template changes (CI, release, tooling).
