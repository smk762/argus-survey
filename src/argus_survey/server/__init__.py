"""FastAPI server for argus-survey (:8107)."""

from argus_survey.server.app import create_app

__all__ = ["create_app"]
