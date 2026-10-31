"""Shared fixtures.

Tests marked ``tools`` shell out to PDAL/GDAL/PROJ. They SKIP when the tools are
absent, unless ``SURVEY_TEST_REQUIRE_TOOLS=1`` (CI and the compose ``test``
profile set it), which turns a missing toolchain into a hard error so a runner
without PDAL can never pass silently.
"""

from __future__ import annotations

import os

import pytest

from argus_survey.tools import tools_available

REQUIRE_TOOLS = os.environ.get("SURVEY_TEST_REQUIRE_TOOLS", "").strip() == "1"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "tools: needs pdal/gdal/projinfo on PATH")
    if REQUIRE_TOOLS and not tools_available():
        raise pytest.UsageError("SURVEY_TEST_REQUIRE_TOOLS=1 but pdal/gdal/projinfo are not on PATH")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if tools_available():
        return
    skip = pytest.mark.skip(reason="pdal/gdal/projinfo not on PATH (set SURVEY_TEST_REQUIRE_TOOLS=1 to fail instead)")
    for item in items:
        if "tools" in item.keywords:
            item.add_marker(skip)
