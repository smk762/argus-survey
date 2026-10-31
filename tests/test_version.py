from __future__ import annotations

import argus_survey


def test_version_is_exposed() -> None:
    assert isinstance(argus_survey.__version__, str)
    assert argus_survey.__version__
