UV ?= uv

.PHONY: help install lock geo-lock lint format test test-docker image doctor build clean

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

build:  ## Build sdist + wheel
	$(UV) build

clean:  ## Remove build + cache artifacts
	rm -rf dist build .venv *.egg-info .pytest_cache .ruff_cache src/argus_survey/_version.py
