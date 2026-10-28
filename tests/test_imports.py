"""Every module in the package imports. A module no other test touches can still take production down: on 2026-10-01
a SyntaxError in albatross/alerts/monitor.py (imported only by __main__) crash-looped the ingest container for two
minutes while the rest of the suite passed."""

import importlib
import pkgutil

import pytest

import albatross

MODULES = sorted(m.name for m in pkgutil.walk_packages(albatross.__path__, "albatross."))


@pytest.mark.parametrize("name", MODULES)
def test_the_module_imports(name):
    importlib.import_module(name)


def test_the_entry_point_imports_with_every_job():
    main = importlib.import_module("albatross.__main__")
    assert "deep_fomo_verdict" in main.jobs and "oracle_watch" in main.jobs
