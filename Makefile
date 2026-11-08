UV ?= uv
# Where `make demo-kit` writes (the compose input mount is read-only, so the kit gets its own).
KIT_HOST ?= $(CURDIR)/data/in

.PHONY: help install lock geo-lock lint format test test-docker image doctor sample demo demo-kit schema up down build clean

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:  ## Sync the venv to uv.lock with all extras (editable)
	$(UV) sync --locked --all-extras

lock:  ## Re-resolve uv.lock after a dependency change in pyproject.toml
	$(UV) lock

geo-lock:  ## Re-resolve env/geo-linux-64.lock (PDAL/GDAL/PROJ) after editing env/geo.yml
	./env/lock.sh

lint:  ## Ruff check + format check
	$(UV)x ruff@0.15.16 check src/ tests/
	$(UV)x ruff@0.15.16 format --check src/ tests/

format:  ## Ruff autoformat + autofix
	$(UV)x ruff@0.15.16 format src/ tests/
	$(UV)x ruff@0.15.16 check --fix src/ tests/

test:  ## Run the suite here (tool-backed tests SKIP without pdal/gdal on PATH)
	$(UV) run --no-sync pytest --tb=short -q

test-docker:  ## Run the full suite in the image against this checkout (tools required)
	docker compose --profile test build test
	docker compose --profile test run --rm test

image:  ## Build the runtime image (runs `doctor --strict` as its last step)
	docker compose build serve

doctor:  ## Toolchain report from the image
	docker compose run --rm survey doctor

sample:  ## Fetch a small public USGS 3DEP LiDAR sample into data/in/usgs-sample.laz
	./scripts/fetch-usgs-sample.sh

demo:  ## Synthetic demo job (known answers) in data/jobs/demo — no download
	docker compose run --rm --entrypoint sh survey -c 'cd /tmp && argus-survey synth site.laz --checks checks.csv && argus-survey run site.laz --job demo --res 0.5 --interval 0.5 && argus-survey accuracy --job demo --checks checks.csv --crs EPSG:7850+5711'

demo-kit:  ## Every demo input with known answers (two epochs, checks, polygons, design) in data/in/kit
	docker compose run --rm --entrypoint argus-survey -v "$(KIT_HOST):/kit" survey synth /kit/kit --kit

schema:  ## Regenerate schema/survey-wire.schema.json (the wire contract UIs codegen against)
	$(UV) run --no-sync argus-survey schema

up:  ## Start the API + viewer on http://127.0.0.1:8107/viewer
	docker compose up -d --build serve

down:  ## Stop the API
	docker compose down

build:  ## Build sdist + wheel
	$(UV) build

clean:  ## Remove build + cache artifacts
	rm -rf dist build .venv *.egg-info .pytest_cache .ruff_cache src/argus_survey/_version.py
