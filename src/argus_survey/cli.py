"""``argus-survey`` — the Typer CLI.

Every command that processes data writes or extends ``<job>/job.json``. Errors a
user can act on (:class:`SurveyError`) print one line and exit 2; tool failures
also print the stderr tail.
"""

from __future__ import annotations

import logging
import sys
from typing import Annotated

import structlog
import typer
from rich.console import Console
from rich.table import Table

from argus_survey import __version__
from argus_survey.models import SurveyError
from argus_survey.tools import ToolError

app = typer.Typer(
    name="argus-survey",
    help="Point-cloud toolkit for land surveyors: QA, ground, DTM/DSM, contours, volumes, change and accuracy, "
    "with every datum, transform and command recorded.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    add_completion=False,
)
console = Console()
err = Console(stderr=True)

EXIT_REFUSED = 2


def _configure_logging(verbose: bool) -> None:
    level = logging.INFO if verbose else logging.WARNING
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
        ],
    )


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"argus-survey {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Log every tool call")] = False,
    version: Annotated[
        bool, typer.Option("--version", callback=_version_callback, is_eager=True, help="Print the version")
    ] = False,
) -> None:
    _configure_logging(verbose)


def fail(exc: SurveyError) -> typer.Exit:
    err.print(f"[bold red]error:[/] {exc}")
    if isinstance(exc, ToolError) and exc.stderr_tail:
        err.print(f"[dim]{exc.stderr_tail}[/]")
    return typer.Exit(EXIT_REFUSED)


@app.command()
def doctor(
    as_json: Annotated[bool, typer.Option("--json", help="Print the report as JSON")] = False,
    strict: Annotated[bool, typer.Option("--strict", help="Exit 1 if any required check fails")] = False,
    no_digests: Annotated[bool, typer.Option("--no-digests", help="Skip re-hashing the datum grids")] = False,
) -> None:
    """Check PDAL/GDAL/PROJ: versions, drivers, datum grids, and the transformations surveyors rely on."""
    from argus_survey.tools import doctor as run_doctor

    report = run_doctor(verify_grid_digests=not no_digests)
    if as_json:
        print(report.model_dump_json(indent=2))
    else:
        v = report.versions
        console.print(f"argus-survey {__version__} · PDAL {v.pdal} · GDAL {v.gdal} · PROJ {v.proj}")
        table = Table(show_header=True, header_style="bold")
        table.add_column("check")
        table.add_column("")
        table.add_column("detail", overflow="fold")
        for c in report.checks:
            mark = "[green]ok[/]" if c.ok else ("[red]FAIL[/]" if c.required else "[yellow]warn[/]")
            table.add_row(c.name, mark, c.detail)
        console.print(table)
        console.print("[green]toolchain ok[/]" if report.ok else "[red]toolchain incomplete[/]")
    if strict and not report.ok:
        raise typer.Exit(1)


if __name__ == "__main__":  # pragma: no cover
    app()
